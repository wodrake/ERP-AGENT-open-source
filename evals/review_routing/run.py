"""Real, bounded, resumable paired routing evaluation. No ERP tool calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage

from src.agent.env_utils import get_env
from src.agent.config import get_llm, LLM_MODEL
from src.agent.harness import load_harness_config
from src.agent.jev_router import JevRouter
from src.agent.review_policy import ReviewPolicy, ReviewRoute

HERE = Path(__file__).parent


class MeasuredModel:
    """Per-request capture; no global mutable metadata across concurrent users."""
    def __init__(self, provider, options):
        self.provider = provider
        self.meta = {"attempted": False, "valid_response": False}
        if provider == "jev":
            self.client = JevRouter(model=get_env("TYPESAFE_MODEL", "jev-latest"),
                                    timeout=options["router_timeout_seconds"],
                                    min_confidence=options.get("jev_min_confidence", 0.0))
        else:
            self.client = get_llm(thinking=False, timeout=options["router_timeout_seconds"], max_retries=0, max_tokens=400)

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages, config=None):
        self.meta["attempted"] = True
        start = time.perf_counter()
        try:
            if self.provider == "jev":
                route, meta = await self.client.ainvoke_with_metadata(messages)
                self.meta.update(meta)
            else:
                result = await self.client.with_structured_output(ReviewRoute, include_raw=True).ainvoke(messages, config=config)
                raw = result["raw"]
                self.meta["usage"] = raw.usage_metadata or {}
                self.meta["model"] = raw.response_metadata.get("model_name", LLM_MODEL)
                if result.get("parsing_error") or result.get("parsed") is None:
                    raise ValueError("Invalid structured output")
                route = ReviewRoute.model_validate(result["parsed"])
            self.meta["valid_response"] = True
            self.meta["raw_decision"] = route.decision
            return route
        except BaseException as exc:
            self.meta["error"] = type(exc).__name__
            # Never write exception strings: HTTP exceptions can carry request details.
            response = getattr(exc, "response", None)
            if response is not None:
                self.meta["http_status"] = response.status_code
            raise
        finally:
            self.meta["call_ms"] = round((time.perf_counter() - start) * 1000, 2)


def state_of(row):
    s = dict(row["state"])
    s["messages"] = [(HumanMessage if m["role"] == "human" else AIMessage)(content=m["content"]) for m in s["messages"]]
    return s


def ratio(x, n):
    return round(x / n, 5) if n else None


def wilson(x, n):
    if not n:
        return None
    z = 1.96
    p = x / n
    d = 1 + z*z/n
    center = (p+z*z/(2*n))/d
    half = z * math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [round(center-half, 5), round(center+half, 5)]


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values)*p)-1)], 2)


def metrics(rows):
    tp = sum(r["label"] == r["prediction"] == "review" for r in rows)
    tn = sum(r["label"] == r["prediction"] == "skip" for r in rows)
    fp = sum(r["label"] == "skip" and r["prediction"] == "review" for r in rows)
    fn = sum(r["label"] == "review" and r["prediction"] == "skip" for r in rows)
    calls = [r for r in rows if r["meta"]["attempted"]]
    latencies = [r["meta"]["call_ms"] for r in calls]
    usage = Counter()
    for r in calls:
        usage.update({k: v for k, v in r["meta"].get("usage", {}).items() if isinstance(v, (int, float))})
    return {"n": len(rows), "tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "label_agreement": ratio(tp+tn, len(rows)), "wilson95_reference_only": wilson(tp+tn, len(rows)),
            "review_recall": ratio(tp, tp+fn), "false_review_rate": ratio(fp, fp+tn),
            "review_precision": ratio(tp, tp+fp), "api_calls": len(calls),
            "api_valid_rate": ratio(sum(r["meta"]["valid_response"] for r in calls), len(calls)),
            "fallbacks": sum(r["source"] == "fallback" for r in rows),
            "p50_call_ms": percentile(latencies, .5), "p95_call_ms": percentile(latencies, .95),
            "usage_reported": dict(usage), "errors": dict(Counter(r["meta"].get("error") for r in calls if r["meta"].get("error")))}


def paired(rows, split):
    a = {r["id"]: r for r in rows if r["provider"] == "deepseek" and r["split"] == split and r["ambiguous"]}
    b = {r["id"]: r for r in rows if r["provider"] == "jev" and r["split"] == split and r["ambiguous"]}
    if not a or a.keys() != b.keys():
        return {"status": "incomplete"}
    groups = {}
    for key, old in a.items():
        new = b[key]
        delta = int(new["prediction"] == new["label"]) - int(old["prediction"] == old["label"])
        groups.setdefault(old["family"], []).append(delta)
    rng = random.Random(20260925)
    group_list = list(groups.values())
    draws = []
    for _ in range(5000):
        draw = [v for group in rng.choices(group_list, k=len(group_list)) for v in group]
        draws.append(sum(draw)/len(draw))
    draws.sort()
    return {"status": "complete", "n": len(a), "families": len(groups),
            "agreement_delta": sum(sum(g) for g in group_list)/len(a),
            "cluster_bootstrap95": [draws[124], draws[4874]],
            "note": "Synthetic assistant labels; family bootstrap, not online effect or independent human gold."}


def write_report(output, rows, manifest):
    summary = {"manifest": manifest, "metrics": {}, "paired_ambiguous": {s: paired(rows, s) for s in ("dev", "test")}}
    lines = ["# 审查路由真实 API 评测", "", "合成、assistant 编写参考标签；不是独立人工标注、线上准确率或端到端成功率。", "",
             "| 模型 | 集合 | 范围 | 样本 | 标签一致率 | 应审查召回率 | 误审率 | API有效率 | P95调用ms |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    def pct(v):
        return "—" if v is None else f"{v*100:.2f}%"
    for provider in manifest["providers"]:
        for split in ("dev", "test"):
            for scope in ("all", "ambiguous", "rule"):
                subset = [r for r in rows if r["provider"] == provider and r["split"] == split and
                          (scope == "all" or r["ambiguous"] == (scope == "ambiguous"))]
                m = metrics(subset)
                summary["metrics"][f"{provider}/{split}/{scope}"] = m
                lines.append(f"| {provider} | {split} | {scope} | {m['n']} | {pct(m['label_agreement'])} | {pct(m['review_recall'])} | {pct(m['false_review_rate'])} | {pct(m['api_valid_rate'])} | {m['p95_call_ms']} |")
    lines += ["", "## 模糊请求配对差异", "", "```json", json.dumps(summary["paired_ambiguous"], ensure_ascii=False, indent=2), "```",
              "", "## 解释边界", "", "- 超时与无效输出按线上现有策略回退 review，纳入一致率；API 有效率另列。",
              "- all 为整套前置路由，ambiguous 为真正进入模型的请求，rule 为相同规则处理的请求。",
              "- 每组含相关改写，配对区间按场景组重采样；单条 Wilson 区间仅作为参考。",
              "- 未以测试结果调整提示词、标签或阈值；无自动启用新模型。",
              "- 未获取计费账单，仅保存 API 返回的 token 用量；费用不估造。",
              "", "## 不一致样本（仅 ID，原文在冻结数据集）", ""]
    for r in rows:
        if r["prediction"] != r["label"]:
            lines.append(f"- {r['provider']} / {r['id']}: expected={r['label']}, actual={r['prediction']}, source={r['source']}")
    (output/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output/"REPORT.md").write_text("\n".join(lines).rstrip()+"\n", encoding="utf-8")


async def run(args):
    config = load_harness_config()
    data_bytes = (HERE/"dataset_v1.jsonl").read_bytes()
    samples = [json.loads(line) for line in data_bytes.decode().splitlines()]
    if args.split != "all":
        samples = [s for s in samples if s["split"] == args.split]
    policy = ReviewPolicy(config)
    if args.smoke:
        samples = [s for s in samples if policy.rule(state_of(s)) is None][:2]
    if args.limit:
        samples = samples[:args.limit]
    providers = args.providers.split(",")
    if any(p not in ("deepseek", "jev") for p in providers):
        raise SystemExit("Only deepseek,jev supported")
    for p in providers:
        if not get_env("TYPESAFE_API_KEY" if p == "jev" else "DEEPSEEK_API_KEY"):
            raise SystemExit(f"Missing key for {p}; no calls made")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"dataset_sha256": hashlib.sha256(data_bytes).hexdigest(),
                "sample_ids": [s["id"] for s in samples], "providers": providers,
                "baseline": "baseline/pre-jev-20260925", "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "config": config["review"], "requested_models": {"deepseek": LLM_MODEL, "jev": get_env("TYPESAFE_MODEL", "jev-latest")},
                "label_source": "synthetic assistant labels, not independently human-reviewed",
                "max_calls_per_provider": args.max_calls, "concurrency": args.concurrency}
    manifest_path = output/"manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous != manifest:
            raise SystemExit("Resume metadata mismatch; use a new output directory")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    results_path = output/"results.jsonl"
    rows = [json.loads(line) for line in results_path.read_text().splitlines()] if results_path.exists() else []
    done = {(r["provider"], r["id"]) for r in rows}
    calls = Counter(r["provider"] for r in rows if r["meta"]["attempted"])
    sem = asyncio.Semaphore(args.concurrency)
    fatal = asyncio.Event()
    async def evaluate(provider, sample):
        async with sem:
            if fatal.is_set() or (provider, sample["id"]) in done:
                return
            state = state_of(sample)
            ambiguous = policy.rule(state) is None
            if ambiguous:
                if calls[provider] >= args.max_calls:
                    fatal.set()
                    return
                calls[provider] += 1
            model = MeasuredModel(provider, config["review"])
            start = time.perf_counter()
            update = await ReviewPolicy(config, model).astart(state)
            decision = update["review_decision"]
            row = {"id": sample["id"], "family": sample["family"], "split": sample["split"],
                   "provider": provider, "label": sample["label"], "ambiguous": ambiguous,
                   "prediction": decision["decision"], "source": decision["source"],
                   "task_type": decision["task_type"], "checks": decision["checks"],
                   "elapsed_ms": round((time.perf_counter()-start)*1000, 2),
                   "meta": model.meta, "at": datetime.now(timezone.utc).isoformat()}
            rows.append(row)
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False)+"\n")
            if model.meta.get("http_status") in (401, 402, 403):
                fatal.set()
            if len(rows) % 20 == 0 or args.smoke:
                print(f"completed={len(rows)} calls={dict(calls)} latest={provider}/{sample['id']} source={decision['source']} error={model.meta.get('error')}", flush=True)
    tasks = []
    for i, sample in enumerate(samples):
        for provider in providers if i % 2 == 0 else list(reversed(providers)):
            tasks.append(evaluate(provider, sample))
    await asyncio.gather(*tasks)
    write_report(output, rows, manifest)
    print(f"DONE {len(rows)}/{len(samples)*len(providers)}; report={output/'REPORT.md'}", flush=True)
    if len(rows) != len(samples)*len(providers):
        raise SystemExit("Stopped: authentication/budget failure. Partial results are not final comparison.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="deepseek,jev")
    parser.add_argument("--split", choices=("all", "dev", "test"), default="all")
    parser.add_argument("--output", required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-calls", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 4 or args.max_calls < 1:
        parser.error("concurrency 1..4; max-calls positive")
    asyncio.run(run(args))
