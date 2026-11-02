"""极简 HTTP 客户端（只用标准库）。

为什么不用 requests：本项目要能扔到任何一台小机器、容器、甚至 CI runner 上跑，
零依赖意味着 ``python -m autodl_autoconnect`` 直接就能用，不用先 pip install。

能力：
- 统一 ``Authorization: <token>`` 头；
- 连接/读取超时分开控制；
- 对「网络抖动」「5xx」「429」自动退避重试，指数退避 + 上限；
- 对 401/403 立刻失败并翻译成 AuthError（重试没有意义）；
- 响应体大小上限，防止异常大的返回把内存吃光；
- 错误消息统一脱敏。
"""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .errors import APIError, AuthError, NetworkError, redact

__all__ = ["HttpClient", "HttpResponse"]

DEFAULT_USER_AGENT = "autodl-autoconnect/1.0 (+https://github.com/)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class HttpResponse:
    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise APIError(
                f"AutoDL 返回的不是合法 JSON（{self.status}）：{redact(self.body[:200])}",
                code="BAD_JSON",
            ) from exc

    def text(self, limit: int = 500) -> str:
        return redact(self.body.decode("utf-8", errors="replace"), limit)


class HttpClient:
    """带鉴权与重试的 HTTP 客户端。"""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.autodl.com",
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.6,
        backoff_cap: float = 8.0,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ):
        self.token = (token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.user_agent = user_agent
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._opener = urllib.request.build_opener()

    # ------------------------------------------------------------------ 内部

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        return delay * (0.5 + self._rng.random() * 0.5)

    # ------------------------------------------------------------------ 公开

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> HttpResponse:
        """发一次请求并返回原始响应。业务错误（HTTP 200 + code!=Success）由调用方判断。"""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{urllib.parse.urlencode(filtered)}"

        data = None
        request_headers = self._headers(headers)
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=data, method=method.upper())
            for key, value in request_headers.items():
                request.add_header(key, value)
            try:
                with self._opener.open(request, timeout=timeout or self.timeout) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise APIError("AutoDL 响应体超过大小上限，已中止读取", code="RESPONSE_TOO_LARGE")
                    return HttpResponse(response.status, dict(response.headers), body, url)
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read(MAX_RESPONSE_BYTES)
                except Exception:  # noqa: BLE001 - 读取错误体失败不应掩盖原始错误
                    pass
                text = body.decode("utf-8", errors="replace")

                if exc.code in (401, 403):
                    raise AuthError(
                        "AutoDL 拒绝了鉴权（HTTP "
                        f"{exc.code}）。请检查开发者 Token 是否有效、是否已过期、"
                        "以及是否已开通该接口权限。",
                        status=exc.code,
                        detail=text,
                    ) from exc

                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = APIError(
                        f"AutoDL 临时错误（HTTP {exc.code}）：{redact(text)}",
                        code="HTTP_TRANSIENT",
                        retryable=True,
                        status=exc.code,
                    )
                    if attempt < retries:
                        self._sleep(self._backoff(attempt))
                        continue
                    raise last_error from exc

                raise APIError(
                    f"AutoDL 接口返回 HTTP {exc.code}：{redact(text)}",
                    code="HTTP_ERROR",
                    status=exc.code,
                    detail=text,
                ) from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                last_error = NetworkError(
                    f"网络访问失败：{redact(reason)}", code="NETWORK_ERROR", retryable=True
                )
                if attempt < retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error from exc
            except (TimeoutError, ConnectionError) as exc:
                # 典型场景：服务端在处理途中断开连接（ConnectionResetError）、
                # 读取超时（socket.timeout 是 OSError 的子类）。
                # 这类错误在下单场景必须转成「结果不明」，绝不能当成失败重试。
                last_error = NetworkError(
                    f"连接中断：{redact(exc)}", code="CONNECTION_RESET", retryable=True
                )
                if attempt < retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error from exc
            except http.client.HTTPException as exc:
                # 协议层错误（例如远端直接关闭连接导致 BadStatusLine / RemoteDisconnected）。
                last_error = NetworkError(
                    f"响应异常：{redact(exc)}", code="HTTP_PROTOCOL_ERROR", retryable=True
                )
                if attempt < retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error from exc

        raise last_error or NetworkError("请求失败", code="NETWORK_ERROR", retryable=True)

    def post(self, path: str, json_body: Any = None, **kwargs) -> HttpResponse:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def get(self, path: str, **kwargs) -> HttpResponse:
        return self.request("GET", path, **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs) -> HttpResponse:
        return self.request("PUT", path, json_body=json_body, **kwargs)
