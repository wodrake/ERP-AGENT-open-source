"""脚本化冒烟：零件查询 + 下单审批。

审批在脚本里决定，不调用大模型。拒绝时不写 ERP；批准后只创建一笔订单。
默认还会检查后端 /health 的 mcp、mongo、sandbox 都是 ok。
只想打 mock-erp 时设置 SMOKE_REQUIRE_BACKEND=0。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class SmokeError(RuntimeError):
    pass


def _request(method: str, url: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SmokeError(f"{method} {url} 失败: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{method} {url} 返回的不是 JSON: {raw[:200]}") from exc


def search_part(base: str, keyword: str = "制动片") -> dict:
    query = urllib.parse.urlencode({"name": keyword})
    payload = _request("GET", f"{base}/api/parts/search?{query}")
    rows = payload.get("data") or []
    if payload.get("code") != 200 or not rows:
        raise SmokeError(f"零件查询没有命中 {keyword}: {payload}")
    return rows[0]


def list_orders(base: str) -> list:
    payload = _request("GET", f"{base}/api/orders/search")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise SmokeError(f"订单查询结果异常: {payload}")
    return data


def decide_and_maybe_create(base: str, part: dict, decision: str, quantity: int = 2) -> dict | None:
    """脚本化审批。reject 不调用写接口；approve 才创建订单。"""
    if decision not in {"approve", "reject"}:
        raise SmokeError(f"未知审批决定: {decision}")
    if decision == "reject":
        return None
    price = part["purchasePrice"]
    order = {
        "orderNumber": f"SMOKE-{part['id']}",
        "totalAmount": round(price * quantity, 2),
        "status": 0,
        "remark": "scripted approval",
        "orderDetail": [
            {"partId": part["id"], "quantity": quantity, "unitPrice": price}
        ],
    }
    created = _request("POST", f"{base}/api/orders/create", order)
    if created.get("code") != 200 or not (created.get("data") or {}).get("id"):
        raise SmokeError(f"批准后的下单失败: {created}")
    return created["data"]


def assert_backend_health(url: str) -> dict:
    payload = _request("GET", url)
    components = payload.get("components") or {}
    bad = {
        name: item.get("status")
        for name, item in components.items()
        if item.get("status") != "ok"
    }
    if payload.get("status") != "ok" or bad or set(components) < {"mcp", "mongo", "sandbox"}:
        raise SmokeError(f"/health 未全部就绪: {payload}")
    return payload


def run(mock_url: str, health_url: str | None, require_backend: bool) -> dict:
    health = _request("GET", f"{mock_url}/health")
    if health.get("status") != "ok":
        raise SmokeError(f"mock-erp 不健康: {health}")
    part = search_part(mock_url)
    before = len(list_orders(mock_url))
    rejected = decide_and_maybe_create(mock_url, part, "reject")
    if rejected is not None or len(list_orders(mock_url)) != before:
        raise SmokeError("拒绝审批后仍然写入了订单")
    created = decide_and_maybe_create(mock_url, part, "approve")
    orders = list_orders(mock_url)
    if created is None or len(orders) != before + 1:
        raise SmokeError(f"批准后订单数应为 {before + 1}，实际 {len(orders)}")
    fetched = _request("GET", f"{mock_url}/api/orders/get/{created['id']}")
    if (fetched.get("data") or {}).get("id") != created["id"]:
        raise SmokeError(f"按 id 回读订单失败: {fetched}")
    backend = None
    if require_backend:
        if not health_url:
            raise SmokeError("缺少 BACKEND_HEALTH_URL")
        backend = assert_backend_health(health_url)
    return {"part": part, "order": created, "backend": backend}


def main() -> int:
    mock_url = os.getenv("MOCK_ERP_URL", "http://127.0.0.1:8081").rstrip("/")
    health_url = os.getenv("BACKEND_HEALTH_URL", "http://127.0.0.1:8000/health")
    require_backend = os.getenv("SMOKE_REQUIRE_BACKEND", "1") != "0"
    try:
        result = run(mock_url, health_url, require_backend)
    except SmokeError as exc:
        print(f"SMOKE FAIL: {exc}", file=sys.stderr)
        return 1
    part = result["part"]
    order = result["order"]
    print(
        "SMOKE OK: "
        f"part={part.get('name')} id={part.get('id')} price={part.get('purchasePrice')} "
        f"order_id={order.get('id')} decision=approve"
    )
    if result["backend"] is not None:
        print("SMOKE OK: /health components mcp, mongo, sandbox = ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
