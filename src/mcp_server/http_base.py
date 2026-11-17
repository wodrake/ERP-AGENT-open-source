"""
HTTP 基础客户端模块
httpx AsyncClient 单例（连接池、超时），封装 GET/POST/PUT/PATCH/DELETE

网络超时和连接失败返回结构化错误，不把 httpx 异常抛到工具层。
只有 GET 做指数退避重试，最多 2 次；写操作不重试。
"""
import asyncio
import json
import os
import time
from typing import Optional

import httpx

from .server_config import ERP_BASE_URL, HTTP_TIMEOUT, HTTP_MAX_CONNECTIONS

# 额外重试次数（不含第一次）。只用于 GET。
GET_MAX_RETRIES = 2
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
# 剩余预算低于这个值就不再发起下一次 GET，避免把最后几毫秒也耗进去。
_MIN_ATTEMPT_SECONDS = 0.05


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_read_budget_seconds() -> float:
    """一次 GET（含最多 2 次重试和退避）的最坏耗时，单位秒。

    单次尝试使用 HTTP_GET_ATTEMPT_TIMEOUT（默认 5），总预算使用
    HTTP_GET_DEADLINE_SECONDS（默认 12）。写操作不走这里，仍只用 HTTP_TIMEOUT 且不重试。
    """
    attempt = max(_MIN_ATTEMPT_SECONDS, _env_float("HTTP_GET_ATTEMPT_TIMEOUT", 5.0))
    deadline = max(_MIN_ATTEMPT_SECONDS, _env_float("HTTP_GET_DEADLINE_SECONDS", 12.0))
    base = max(0.0, _env_float("HTTP_GET_RETRY_BASE_SECONDS", 0.2))
    backoff = sum(base * (2 ** index) for index in range(GET_MAX_RETRIES))
    return min(deadline, (GET_MAX_RETRIES + 1) * attempt + backoff)


class ERPHttpClient:
    """ERP 后端 HTTP 客户端单例"""
    """
    HTTP 客户端采用单例模式，因为其维护昂贵的连接池资源，全局共享可复用 TCP 连接、
    显著提升性能并统一超时/并发配置；而 LLM 实例不适合单例，因为不同任务需要独立的模型、温度、API Key 等参数配置，
    且调用本身是无状态的，按需创建更灵活、更经济。
    """
    _instance: Optional['ERPHttpClient'] = None
    _client: Optional[httpx.AsyncClient] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=ERP_BASE_URL,
                timeout=httpx.Timeout(HTTP_TIMEOUT),
                limits=httpx.Limits(
                    max_connections=HTTP_MAX_CONNECTIONS,
                    max_keepalive_connections=10,
                ),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET 请求。超时或连接失败时指数退避，最多再试 2 次。"""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_data: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        """POST 请求。写操作失败不重试。"""
        return await self._request("POST", path, params=params, json_data=json_data)

    async def put(self, path: str, json_data: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        """PUT 请求。写操作失败不重试。"""
        return await self._request("PUT", path, params=params, json_data=json_data)

    async def patch(self, path: str, params: Optional[dict] = None) -> dict:
        """PATCH 请求。写操作失败不重试。"""
        return await self._request("PATCH", path, params=params)

    async def delete(self, path: str) -> dict:
        """DELETE 请求。写操作失败不重试。"""
        return await self._request("DELETE", path)

    def _retry_limit(self, method: str) -> int:
        if method.upper() == "GET":
            return GET_MAX_RETRIES
        return 0

    def _retry_base_delay(self) -> float:
        return max(0.0, _env_float("HTTP_GET_RETRY_BASE_SECONDS", 0.2))

    def _read_attempt_timeout(self) -> float:
        return max(_MIN_ATTEMPT_SECONDS, _env_float("HTTP_GET_ATTEMPT_TIMEOUT", 5.0))

    def _read_deadline(self) -> float:
        return max(_MIN_ATTEMPT_SECONDS, _env_float("HTTP_GET_DEADLINE_SECONDS", 12.0))

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
    ) -> dict:
        kwargs = {}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if json_data is not None:
            kwargs["json"] = json_data

        is_get = method.upper() == "GET"
        attempts_allowed = self._retry_limit(method) + 1
        deadline = self._read_deadline() if is_get else None
        started = time.monotonic()
        last_error: Exception | None = None
        made = 0
        for attempt in range(1, attempts_allowed + 1):
            request_kwargs = dict(kwargs)
            if is_get:
                remaining = deadline - (time.monotonic() - started)
                if remaining < _MIN_ATTEMPT_SECONDS:
                    break
                request_kwargs["timeout"] = httpx.Timeout(min(self._read_attempt_timeout(), remaining))
            try:
                response = await self.client.request(method, path, **request_kwargs)
                return self._handle_response(response)
            except _NETWORK_ERRORS as exc:
                last_error = exc
                made = attempt
                if attempt >= attempts_allowed:
                    break
                if not is_get:
                    break
                remaining = deadline - (time.monotonic() - started)
                delay = self._retry_base_delay() * (2 ** (attempt - 1))
                delay = min(delay, max(0.0, remaining - _MIN_ATTEMPT_SECONDS))
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
        if made == 0:
            last_error = last_error or TimeoutError("GET deadline exhausted")
        return self._network_error(last_error, method, made, deadline if is_get else None)

    def _network_error(
        self,
        exc: Exception | None,
        method: str,
        attempts: int,
        deadline: float | None = None,
    ) -> dict:
        """工具层可以直接序列化的错误，不向外抛 httpx 异常。"""
        is_timeout = isinstance(exc, (httpx.TimeoutException, TimeoutError))
        write = method.upper() != "GET"
        if write:
            message = (
                "ERP 写入失败（超时或连接中断）。请告知用户本次没有确认写入，"
                "不要重试写操作，也不要假设订单、库存或供应商数据已经变更。"
            )
        else:
            message = (
                "ERP 查询超时或连接失败。请告知用户稍后再试，"
                "不要编造零部件、库存、供应商或价格数据。"
            )
        payload = {
            "code": 504 if is_timeout else 503,
            "message": message,
            "error": "timeout" if is_timeout else "connect_error",
            "data": None,
            "method": method.upper(),
            "attempts": attempts,
            "retried": attempts > 1,
            "detail": exc.__class__.__name__ if exc else "network_error",
        }
        if deadline is not None:
            payload["deadline_seconds"] = deadline
        return payload

    def _handle_response(self, response: httpx.Response) -> dict:
        """统一响应处理"""
        try:
            data = response.json()
            if response.status_code == 200:
                return data
            else:
                return {
                    "code": response.status_code,
                    "message": f"HTTP Error: {response.status_code}",
                    "data": data,
                }
        except json.JSONDecodeError:
            return {
                "code": response.status_code,
                "message": f"Response not JSON: {response.text[:500]}",
                "data": None,
            }

    async def close(self):
        """关闭客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局单例
erp_client = ERPHttpClient()
