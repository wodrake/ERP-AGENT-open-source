"""Opt-in metadata-only Langfuse tracing. Never forwards callback payloads."""
import asyncio
import functools
import hashlib
import hmac
import os
import re
import threading
import time
from contextvars import ContextVar
from langchain_core.callbacks import BaseCallbackHandler

_current = ContextVar('erp_trace', default=None)
_client = None
_client_lock = threading.Lock()


def client():
    global _client
    if os.getenv('ERP_LANGFUSE_ENABLED', 'false').lower() != 'true': return None
    if not all(os.getenv(k) for k in ('LANGFUSE_SECRET_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_BASE_URL', 'ERP_TRACE_HASH_KEY')): return None
    with _client_lock:
        if _client is None:
            try:
                from langfuse import Langfuse
                if os.getenv('ERP_LANGFUSE_DIRECT','false').lower()=='true':
                    from urllib.parse import urlparse
                    host=urlparse(os.environ['LANGFUSE_BASE_URL']).hostname
                    if host: os.environ['NO_PROXY']=','.join(filter(None,[os.getenv('NO_PROXY',''),host]))
                _client = Langfuse(timeout=10, flush_at=20, flush_interval=5,
                    environment=os.getenv('ERP_TRACE_ENVIRONMENT','development'),
                    release=os.getenv('ERP_TRACE_RELEASE','unversioned'))
            except Exception: return None
    return _client


def pseudonym(value):
    return hmac.new(os.environ['ERP_TRACE_HASH_KEY'].encode(), str(value).encode(), hashlib.sha256).hexdigest()[:32]


def _propagate_attributes(identity):
    """langfuse 未安装时不中断追踪逻辑；调用方仍然可以挂假客户端做测试。"""
    try:
        from langfuse import propagate_attributes
    except Exception:
        from contextlib import nullcontext
        return nullcontext()
    return propagate_attributes(**identity)


class Trace:
    def __init__(self, user, thread, resume=False):
        self.client = client()
        self.root = None
        self.started = time.perf_counter()
        self.first_token = False
        if self.client:
            try:
                identity={'user_id':pseudonym(user),'session_id':pseudonym(thread)}
                with _propagate_attributes(identity):
                    self.root = self.client.start_observation(name='erp.request', as_type='span')
                self.root.update(metadata={**identity,'resume': bool(resume), 'content_policy': 'metadata_only'})
            except Exception: self.root = None
        self.callback = MetadataCallback(self)

    def start(self, name, parent=None, generation=False):
        if not self.root: return None
        try:
            return self.client.start_observation(name=name, as_type='generation' if generation else 'span',
                trace_context={'trace_id': self.root.trace_id, 'parent_span_id': (parent or self.root).id})
        except Exception: return None

    def event(self, name, **numbers):
        span = self.start(name)
        if span:
            try:
                span.update(metadata={k:v for k,v in numbers.items() if isinstance(v,(int,float,bool))})
                span.end()
            except Exception: pass

    def finish(self, status):
        self.callback.close()
        if self.root:
            try:
                self.root.update(metadata={'status': status, 'duration_ms': (time.perf_counter()-self.started)*1000})
                self.root.end()
            except Exception: pass


def event(name, **numbers):
    trace = _current.get()
    if trace: trace.event(name, **numbers)


def measured(name):
    """Instrument internal operations without recording arguments or exceptions."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            start=time.perf_counter()
            try:
                result=fn(*args, **kwargs)
            except BaseException:
                event(name, success=False, duration_ms=(time.perf_counter()-start)*1000)
                raise
            event(name, success=True, duration_ms=(time.perf_counter()-start)*1000)
            return result
        return wrapped
    return decorate


class MetadataCallback(BaseCallbackHandler):
    """Custom callback deliberately does not pass messages, arguments or raw errors."""
    run_inline = True
    def __init__(self, trace): self.trace=trace; self.spans={}; self.lock=threading.RLock()
    def begin(self, run_id, parent_run_id, name, generation=False):
        with self.lock:
            self.spans[run_id]=self.trace.start(name, self.spans.get(parent_run_id), generation)
    def end(self, run_id, failed=False, usage=None, model=None):
        with self.lock: span=self.spans.pop(run_id,None)
        if span:
            try:
                values={'metadata':{'failed':failed}}
                if usage:
                    values['usage_details']={dest:int(usage[k]) for k,dest in [('input_tokens','input'),('output_tokens','output'),('total_tokens','total')] if isinstance(usage.get(k),int)}
                if model and re.fullmatch(r'[a-zA-Z0-9._:/-]{1,80}',model): values['model']=model
                span.update(**values);span.end()
            except Exception: pass
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
        self.begin(run_id,parent_run_id,'agent.step')
    def on_chain_end(self, outputs, *, run_id, **kw): self.end(run_id)
    def on_chain_error(self, error, *, run_id, **kw): self.end(run_id,True)
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, **kw):
        self.begin(run_id,parent_run_id,'model.router' if 'review_router' in (tags or []) else 'model.call',True)
    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw): self.begin(run_id,parent_run_id,'model.call',True)
    def on_llm_end(self, response, *, run_id, **kw):
        usage=None;model=None
        for row in response.generations:
            for g in row:
                usage=getattr(getattr(g,'message',None),'usage_metadata',None) or usage
                model=(getattr(getattr(g,'message',None),'response_metadata',{}) or {}).get('model_name') or model
        self.end(run_id,usage=usage,model=model)
    def on_llm_error(self, error, *, run_id, **kw): self.end(run_id,True)
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw):
        # Names are code-owned in this project; no arbitrary plugin names exported.
        name=(serialized or {}).get('name','')
        allowed={'task','read_memory','remember','list_skills','read_skill','read_skill_resource','request_order_info'}
        if name not in allowed and not (name.startswith(('part_','supplier_','inventory_','order_')) and name.replace('_','').isascii() and name.replace('_','').isalnum()): name='other'
        self.begin(run_id,parent_run_id,'tool.'+name)
    def on_tool_end(self, output, *, run_id, **kw):
        import json
        failed=getattr(output,'status',None)=='error'
        try:
            data=json.loads(getattr(output,'content',output))
            if isinstance(data,dict) and isinstance(data.get('code'),int):failed |= data['code']>=400
        except (ValueError,TypeError):pass
        self.end(run_id, failed)
    def on_tool_error(self, error, *, run_id, **kw): self.end(run_id,True)
    def close(self):
        for run_id in list(self.spans): self.end(run_id,True)


def traced_stream(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        import inspect
        bound=inspect.signature(fn).bind(*args,**kwargs);bound.apply_defaults()
        trace=Trace(bound.arguments['user_id'],bound.arguments['thread_id'],bound.arguments.get('resume_data') is not None)
        token=_current.set(trace);status='cancelled'
        try:
            async for chunk in fn(*args,**kwargs):
                # Only inspect event type/status; never send SSE data payloads.
                if 'event: error\n' in chunk: status='failed'
                if 'event: interrupt\n' in chunk or 'event: interrupted\n' in chunk: status='interrupted'
                if 'event: interrupt\n' in chunk: trace.event('hitl.interrupt')
                if 'event: token\n' in chunk and not trace.first_token:
                    trace.first_token=True;trace.event('request.first_token',duration_ms=(time.perf_counter()-trace.started)*1000)
                if 'event: done\n' in chunk and status not in ('failed','interrupted'):
                    status='interrupted' if '"interrupted": true' in chunk else 'completed'
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            status='cancelled'
            raise
        except BaseException:
            status='failed'
            raise
        finally:
            trace.finish(status);_current.reset(token)
    return wrapped


def traced_config(config):
    trace=_current.get()
    if not trace or not trace.root: return config
    return {**config,'callbacks':[*config.get('callbacks',[]),trace.callback]}


async def shutdown():
    if _client:
        try: await asyncio.wait_for(asyncio.to_thread(_client.flush),3)
        except Exception: pass
