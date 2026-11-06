"""极简 HTTP 客户端（只用标准库）。

为什么不用 requests：本项目要能扔到任何一台小机器、容器、甚至 CI runner 上跑，
零依赖意味着 ``python -m autodl_autoconnect`` 直接就能用，不用先 pip install。

能力：

- **长连接复用（keep-alive）**：抢卡比的是「看到货 -> 下完单」之间的时间，
  而 ``urllib`` 默认每个请求都重建 TCP + TLS。实测直连 api.autodl.com、
  同一个余额请求：新连接 3187 ms，复用连接 470 ~ 780 ms。
  下单路径上一共五个请求，旧实现光握手就要多花好几秒，卡早就没了。
- 每个线程一条连接（线程内复用），因此可以安全地并发查余额 / 查实例；
- 统一 ``Authorization: <token>`` 头；
- 超时控制；
- 对「网络抖动」「5xx」「429」自动退避重试，指数退避 + 上限；
- 对 401/403 立刻失败并翻译成 AuthError（重试没有意义）；
- 响应体大小上限，防止异常大的返回把内存吃光；
- 错误消息统一脱敏；
- 检测到系统代理时自动退回 ``urllib``（代理配置、证书、no_proxy 全部沿用原逻辑）。
"""

from __future__ import annotations

import http.client
import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .errors import APIError, AuthError, AutoDLError, NetworkError, redact

__all__ = ["HttpClient", "HttpResponse"]

DEFAULT_USER_AGENT = "autodl-autoconnect/1.1 (+https://github.com/lichen9240-creator/Practical-tools-AIresearch)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: SSL 上下文只建一次（加载一次 CA 证书约 10~30ms，不该每个请求都做）。
_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_CONTEXT_LOCK = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        with _SSL_CONTEXT_LOCK:
            if _SSL_CONTEXT is None:
                _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _proxy_configured(scheme: str) -> bool:
    """系统/环境变量里是否给这个 scheme 配了代理。

    配了代理时不用长连接：``urllib`` 已经处理好代理认证、``no_proxy``、证书等细节，
    自己实现一遍不如直接退回它。
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - 读不到代理配置就当作没有
        return False
    return bool(proxies.get(scheme) or proxies.get("all"))


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


def _status_error(status: int, text: str) -> AutoDLError:
    """把 HTTP 状态码翻译成异常（鉴权类单独处理）。"""
    if status in (401, 403):
        return AuthError(
            "AutoDL 拒绝了鉴权（HTTP "
            f"{status}）。请检查开发者 Token 是否有效、是否已过期、"
            "以及是否已开通该接口权限。",
            status=status,
            detail=text,
        )
    if status == 429 or 500 <= status < 600:
        return APIError(
            f"AutoDL 临时错误（HTTP {status}）：{redact(text)}",
            code="HTTP_TRANSIENT",
            retryable=True,
            status=status,
        )
    return APIError(
        f"AutoDL 接口返回 HTTP {status}：{redact(text)}",
        code="HTTP_ERROR",
        status=status,
        detail=text,
    )


class HttpClient:
    """带鉴权、长连接与重试的 HTTP 客户端。"""

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
        keepalive: bool = True,
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

        parts = urllib.parse.urlsplit(self.base_url)
        self._scheme = (parts.scheme or "https").lower()
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._base_path = (parts.path or "").rstrip("/")

        # 长连接只在「直连 + http(s)」时启用；配了代理就退回 urllib 的老路径。
        self.keepalive = bool(keepalive) and self._scheme in ("http", "https") and not _proxy_configured(
            self._scheme
        )
        self._local = threading.local()
        self._conns: set[http.client.HTTPConnection] = set()
        self._conns_lock = threading.Lock()
        self._urllib_opener = urllib.request.build_opener()

    # ------------------------------------------------------------------ 内部

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": self.user_agent,
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        return delay * (0.5 + self._rng.random() * 0.5)

    def _connection(self) -> tuple[http.client.HTTPConnection, bool]:
        """取本线程的长连接，返回 ``(连接, 是否为复用连接)``。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with self._conns_lock:
                if conn in self._conns:
                    return conn, True
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout, context=_ssl_context()
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        self._local.conn = conn
        with self._conns_lock:
            self._conns.add(conn)
        return conn, False

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            with self._conns_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关连接失败不影响主流程
                pass

    def close(self) -> None:
        """关闭所有线程建立的长连接（进程退出或收工时调用）。"""
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        self._local.conn = None
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

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
        query_string = ""
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                query_string = urllib.parse.urlencode(filtered)
                url = f"{url}{'&' if '?' in url else '?'}{query_string}"

        data = None
        request_headers = self._headers(headers)
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        retries = self.max_retries if max_retries is None else max(0, int(max_retries))

        # 绝对 URL（含别的主机）交给 urllib，避免把请求发错主机。
        if not self.keepalive or path.startswith("http"):
            return self._request_urllib(url, method, data, request_headers, timeout, retries)

        target = f"{self._base_path}{path}"
        if query_string:
            target = f"{target}{'&' if '?' in target else '?'}{query_string}"
        return self._request_pooled(method.upper(), target, data, request_headers, retries, timeout)

    # ------------------------------------------------------------------ 长连接

    def _request_pooled(
        self,
        method: str,
        target: str,
        data: bytes | None,
        headers: Mapping[str, str],
        retries: int,
        timeout: float | None = None,
    ) -> HttpResponse:
        last_error: Exception | None = None
        pooled_headers = dict(headers)
        pooled_headers.setdefault("Connection", "keep-alive")
        for attempt in range(retries + 1):
            conn, reused = self._connection()
            if timeout is not None:
                conn.timeout = timeout
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(timeout)
                    except OSError:  # noqa: PERF203 - 套接字已断，交给下面统一处理
                        pass
            try:
                conn.request(method, target, body=data, headers=pooled_headers)
                response = conn.getresponse()
                body = response.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                # 典型场景：连接被服务端空闲回收（复用时才第一次写入就断开）、
                # 读超时、TLS 握手失败。统一关掉连接，下次重建。
                self._drop_connection()
                last_error = NetworkError(
                    f"网络访问失败：{redact(exc)}", code="NETWORK_ERROR", retryable=True
                )
                if attempt < retries:
                    # 复用连接上的第一次失败多半只是「服务端把空闲连接关了」，
                    # 立刻换一条新连接重试，不必等退避。
                    if not reused:
                        self._sleep(self._backoff(attempt))
                    continue
                raise last_error from exc

            status = response.status
            response_headers = dict(response.getheaders())
            if response.will_close:
                self._drop_connection()

            if len(body) > MAX_RESPONSE_BYTES:
                # 响应体没读完，这条连接不能再复用。
                self._drop_connection()
                raise APIError("AutoDL 响应体超过大小上限，已中止读取", code="RESPONSE_TOO_LARGE")

            if status >= 400:
                error = _status_error(status, body.decode("utf-8", errors="replace"))
                if error.retryable and attempt < retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise error

            return HttpResponse(status, response_headers, body, target)

        raise last_error or NetworkError("请求失败", code="NETWORK_ERROR", retryable=True)

    # ------------------------------------------------------------------ urllib

    def _request_urllib(
        self,
        url: str,
        method: str,
        data: bytes | None,
        request_headers: Mapping[str, str],
        timeout: float | None,
        retries: int,
    ) -> HttpResponse:
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=data, method=method.upper())
            for key, value in request_headers.items():
                request.add_header(key, value)
            try:
                with self._urllib_opener.open(request, timeout=timeout or self.timeout) as response:
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
                error = _status_error(exc.code, body.decode("utf-8", errors="replace"))
                if error.retryable and attempt < retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise error from exc
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
