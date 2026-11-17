"""
HTTP 基础客户端模块
httpx AsyncClient 单例（连接池、超时），封装 GET/POST/PUT/PATCH/DELETE

网络超时和连接失败返回结构化错误，不把 httpx 异常抛到工具层。
只有 GET 做指数退避重试，最多 2 次；写操作不重试。
"""
import asyncio
import json
import os
from typing import Optional

import httpx

from .server_config import ERP_BASE_URL, HTTP_TIMEOUT, HTTP_MAX_CONNECTIONS

# 额外重试次数（不含第一次）。只用于 GET。
GET_MAX_RETRIES = 2
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError)


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
        raw = os.getenv("HTTP_GET_RETRY_BASE_SECONDS", "0.2")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.2

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

        attempts = self._retry_limit(method) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.request(method, path, **kwargs)
                return self._handle_response(response)
            except _NETWORK_ERRORS as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self._retry_base_delay() * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
        return self._network_error(last_error, method, attempts)

    def _network_error(self, exc: Exception | None, method: str, attempts: int) -> dict:
        """工具层可以直接序列化的错误，不向外抛 httpx 异常。"""
        is_timeout = isinstance(exc, httpx.TimeoutException)
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
        return {
            "code": 504 if is_timeout else 503,
            "message": message,
            "error": "timeout" if is_timeout else "connect_error",
            "data": None,
            "method": method.upper(),
            "attempts": attempts,
            "retried": attempts > 1,
            "detail": exc.__class__.__name__ if exc else "network_error",
        }

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
