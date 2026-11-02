"""通知渠道。

支持四种，任意组合，失败不影响主流程：

* **终端**：醒目的横幅 + Windows 上响一声（抢到卡时你可能在忙别的，得听得见）；
* **Webhook**：POST 一个 JSON 到你自己的地址（钉钉/飞书/企微机器人适配器都能收）；
* **Server酱**：把消息推到微信；
* **AutoDL 微信消息**：推到你绑定了微信的 AutoDL 账号。

Webhook 与 AutoDL 微信消息的请求体结构，取自 AutoDL 控制台自身与参考实现，
保证对接现有机器人时字段名一致。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import NotifyConfig
from .errors import redact

__all__ = ["Notifier", "DeliveryResult"]

AUTODL_WECHAT_URL = "https://www.autodl.com/api/v1/wechat/message/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"

#: AutoDL 微信消息有频率限制，这里比官方更保守。
WECHAT_MIN_INTERVAL = 15.0
WECHAT_DAILY_LIMIT = 50


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    message: str = ""

    def to_dict(self) -> dict:
        return {"channel": self.channel, "ok": self.ok, "message": self.message}


class Notifier:
    """把一条消息发到所有启用的渠道。"""

    def __init__(
        self,
        config: NotifyConfig,
        *,
        http_post: Callable[[str, dict, dict], tuple[int, str]] | None = None,
        printer: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._http_post = http_post or _default_post
        self._printer = printer or (lambda text: print(text, file=sys.stderr, flush=True))
        self._clock = clock
        self._wechat_sent: list[float] = []
        self._wechat_next_at = 0.0

    # ------------------------------------------------------------------ 公开

    def send(self, title: str, content: str, *, event: str = "notice", name: str = "") -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        if self.config.desktop:
            results.append(self._send_desktop(title, content))
        if self.config.webhook_url:
            results.append(self._send_webhook(title, content, event=event, name=name))
        if self.config.serverchan_key:
            results.append(self._send_serverchan(title, content))
        if self.config.autodl_wechat_token:
            results.append(self._send_autodl_wechat(title, content, name=name))
        return results

    # ------------------------------------------------------------------ 渠道

    def _send_desktop(self, title: str, content: str) -> DeliveryResult:
        line = "=" * 68
        text = f"\n{line}\n  {title}\n{line}\n  {content}\n{line}"
        try:
            self._printer(text)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult("desktop", False, redact(exc))
        self._beep()
        return DeliveryResult("desktop", True)

    @staticmethod
    def _beep() -> None:
        try:
            import winsound

            winsound.Beep(880, 180)
            winsound.Beep(1180, 260)
        except Exception:  # noqa: BLE001 - 非 Windows 或无声卡，忽略
            pass

    def _send_webhook(self, title: str, content: str, *, event: str, name: str) -> DeliveryResult:
        # 本工具自定义的 Webhook 报文格式（字段名由本项目自己定，
        # 与任何其它同类工具无关；对接机器人时按这个格式写适配器即可）。
        event_id = f"aac-{int(self._clock() * 1000)}"
        payload = {
            "source": "autodl-autoconnect",
            "kind": event,
            "headline": title,
            "label": name[:40],
            "message": content[:2000],
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        headers = {"Content-Type": "application/json", "X-Event-Id": event_id}
        if self.config.webhook_token:
            headers["Authorization"] = f"Bearer {self.config.webhook_token}"
        try:
            status, body = self._http_post(self.config.webhook_url, payload, headers)
            ok = 200 <= status < 300
            return DeliveryResult("webhook", ok, f"HTTP {status} {redact(body, 200)}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult("webhook", False, redact(exc))

    def _send_serverchan(self, title: str, content: str) -> DeliveryResult:
        url = SERVERCHAN_URL.format(key=self.config.serverchan_key)
        form = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            status, body = self._http_post(url, form, headers)
            ok = 200 <= status < 300
            return DeliveryResult("serverchan", ok, f"HTTP {status} {redact(body, 200)}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult("serverchan", False, redact(exc))

    def _send_autodl_wechat(self, title: str, content: str, *, name: str) -> DeliveryResult:
        now = self._clock()
        if now < self._wechat_next_at:
            wait = int(self._wechat_next_at - now)
            return DeliveryResult("autodl_wechat", False, f"触发频率限制，{wait}s 后才能再发")
        recent = [ts for ts in self._wechat_sent if now - ts < 86400]
        if len(recent) >= WECHAT_DAILY_LIMIT:
            return DeliveryResult("autodl_wechat", False, "24 小时内发送次数已达上限")
        payload = {"title": title, "name": name[:40], "content": content[:200]}
        headers = {
            "Content-Type": "application/json",
            "Authorization": self.config.autodl_wechat_token,
        }
        try:
            status, body = self._http_post(AUTODL_WECHAT_URL, payload, headers)
            ok = 200 <= status < 300
            if ok:
                self._wechat_sent.append(now)
                self._wechat_next_at = now + WECHAT_MIN_INTERVAL
            return DeliveryResult("autodl_wechat", ok, f"HTTP {status} {redact(body, 200)}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult("autodl_wechat", False, redact(exc))


def _default_post(url: str, body: Any, headers: dict[str, str]) -> tuple[int, str]:
    """默认的 POST 实现：body 可以是 dict（转 JSON）或 bytes（原样发送）。"""
    data: bytes
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    else:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(65536).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        return exc.code, text
    except urllib.error.URLError as exc:
        raise RuntimeError(f"通知发送失败：{redact(exc.reason)}") from exc
