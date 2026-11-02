"""抢卡主循环。

一条铁律：**下单结果不明时，绝不重试下单。**

原因：AutoDL 的空闲卡只存在几十秒，抢到就立刻有人抢走；而且下单是扣费动作。
如果请求超时，我们无法区分「没下成功」和「下成功了但回包丢了」。
此时如果盲目重试，很可能：
- 下了两单，白花两份钱；
- 或者在别的机器上又下了一单，而原来那台其实已经开出来了。

所以这里的做法是：先写意图 -> 发下单 -> 无论成功失败都写结果；
一旦结果是「不明」，立刻转成只读对账（查实例列表），并**停止一切下单动作**，
把情况交给人判断。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .api import AutoDLClient
from .config import TaskSpec
from .errors import APIError, AutoDLError, NetworkError
from .journal import Journal
from .matcher import MatchResult, match_offer, select_best
from .models import Balance, MachineOffer, OrderResult
from .notifier import Notifier

__all__ = ["Watcher", "WatchOutcome"]

#: 下单后轮询「实例是否已出现」的次数与间隔。
CONFIRM_ATTEMPTS = 6
CONFIRM_INTERVAL = 2.0

#: 余额快照的复用时长（秒）。
BALANCE_TTL = 30.0


@dataclass
class WatchOutcome:
    """一轮检查的结果。"""

    matched: bool = False
    ordered: bool = False
    order: OrderResult | None = None
    status: str = "no_match"
    """``no_match`` / ``observed`` / ``created`` / ``confirmed`` / ``ambiguous`` / ``error``。"""

    message: str = ""
    candidates: list[dict] = field(default_factory=list)
    balance: dict | None = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.status not in ("error", "ambiguous"),
            "matched": self.matched,
            "ordered": self.ordered,
            "status": self.status,
            "message": self.message,
            "candidates": self.candidates,
            "balance": self.balance,
            "attempts": self.attempts,
            "order": self.order.to_dict() if self.order else None,
        }


class Watcher:
    """把「查库存 -> 匹配 -> 下单 -> 核对 -> 通知」串起来。"""

    def __init__(
        self,
        client: AutoDLClient,
        spec: TaskSpec,
        *,
        journal: Journal | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.spec = spec
        self.journal = journal or Journal()
        self.notifier = notifier or Notifier(spec.notify)
        self._clock = clock
        self._sleep = sleep
        self._log = logger or (lambda text: print(text, flush=True))
        self._balance_cache: tuple[float, Balance] | None = None
        self._attempts = 0
        self._blocked_reason = ""

    # ------------------------------------------------------------------ 余额

    def balance(self, *, force: bool = False) -> Balance:
        now = self._clock()
        if not force and self._balance_cache and now - self._balance_cache[0] < BALANCE_TTL:
            return self._balance_cache[1]
        snapshot = self.client.balance()
        self._balance_cache = (now, snapshot)
        return snapshot

    def _required_milli(self, offer: MachineOffer) -> int:
        """本次下单需要的金额（千分之一元）。拿不到价格时返回 0（表示无法核实）。"""
        price = offer.price_hourly
        if price is None:
            return 0
        total = price * self.spec.gpu_count * self.spec.expected_hours
        return int(round((total + self.spec.balance_reserve) * 1000))

    def _check_balance(self, offer: MachineOffer) -> tuple[bool, str]:
        required = self._required_milli(offer)
        if required <= 0:
            if self.spec.require_known_price and (
                self.spec.max_total_cost is not None or self.spec.balance_reserve > 0
            ):
                return False, "拿不到价格，无法核实余额是否够用"
            return True, ""
        snapshot = self.balance()
        available = snapshot.assets_milli
        if self.spec.include_vouchers:
            available += snapshot.voucher_milli
        if available < required:
            return False, (
                f"可用余额 ¥{available / 1000:.3f} 低于本次预检所需 ¥{required / 1000:.3f}"
                f"（单卡价 {offer.price_hourly}/时 × {self.spec.gpu_count} 卡 × "
                f"{self.spec.expected_hours} 小时 + 保留 {self.spec.balance_reserve}）"
            )
        # AutoDL 规则：租用多卡实例时钱包余额（不含代金券）需大于 10 元。
        if self.spec.gpu_count > 1 and snapshot.assets_milli <= 10000:
            return False, (
                f"钱包余额 ¥{snapshot.assets / 1000:.2f}：AutoDL 规定租用多卡实例时"
                "钱包余额需大于 10 元（代金券不计）"
            )
        return True, ""

    # ------------------------------------------------------------------ 主流程

    def check_once(self, *, allow_order: bool = True) -> WatchOutcome:
        """检查一轮库存。``allow_order=False`` 时只看不动。"""
        outcome = WatchOutcome()

        blocked = self._blocked_reason or self._detect_block()
        if blocked:
            outcome.status = "ambiguous"
            outcome.message = blocked
            self._blocked_reason = blocked
            return outcome

        try:
            offers = self.client.search_machines(
                regions=self.spec.normalized_regions(),
                page_size=max(50, min(200, self.spec.gpu_count * 20 + 50)),
            )
        except AutoDLError as exc:
            outcome.status = "error"
            outcome.message = f"查询库存失败：{exc}"
            return outcome

        results = [match_offer(offer, self.spec) for offer in offers]
        outcome.candidates = [
            {
                "machine_id": r.offer.machine_id,
                "region": r.offer.region_sign,
                "gpu_names": list(r.offer.gpu_names),
                "idle_gpu_num": r.offer.idle_gpu_num,
                "price_hourly": r.offer.price_hourly,
                "matched": r.matched,
                "reason": r.reason,
            }
            for r in results
            if r.offer is not None
        ]
        best = select_best(results, self.spec)
        if best is None:
            outcome.status = "no_match"
            outcome.message = f"没有匹配的机器（本轮共 {len(offers)} 条报价）"
            return outcome

        outcome.matched = True
        assert best.offer is not None
        offer = best.offer
        outcome.message = best.reason

        if not allow_order or not self.spec.paid_consent:
            outcome.status = "observed"
            outcome.message = f"有货：{best.reason}（未授权下单，仅通知）"
            self._notify_once(
                "AutoDL 有空闲显卡",
                f"{best.reason}\n机器 {offer.machine_id}\n"
                f"如需自动下单，请把配置里的 paid_consent 设为 true。",
                event="stock_found",
            )
            return outcome

        return self._place_order(offer, outcome)

    def _place_order(self, offer: MachineOffer, outcome: WatchOutcome) -> WatchOutcome:
        """执行下单，并在任何情况下都留下可核对的日志。"""
        ok, why = self._check_balance(offer)
        if not ok:
            outcome.status = "error"
            outcome.message = why
            self.journal.record("skipped", machine_id=offer.machine_id, status="balance", message=why)
            self._notify_once("AutoDL 余额不足", why, event="balance_low")
            return outcome

        # 幂等：如果这台机器上已经有我方的实例，就不要再下第二单。
        existing = self._find_existing_instance(offer.machine_id)
        if existing is not None:
            outcome.status = "observed"
            outcome.message = f"机器 {offer.machine_id} 上已有实例 {existing.uuid}，跳过重复下单"
            outcome.order = OrderResult(
                instance_uuid=existing.uuid,
                machine_id=offer.machine_id,
                gpu_count=existing.gpu_count or self.spec.gpu_count,
                price_hourly=offer.price_hourly,
                confirmed=True,
            )
            return outcome

        self._attempts += 1
        outcome.attempts = self._attempts
        if self.spec.max_attempts and self._attempts > self.spec.max_attempts:
            outcome.status = "error"
            outcome.message = f"已达到最大下单次数 {self.spec.max_attempts}"
            return outcome

        # 第一步：把「打算做什么」落盘。
        self.journal.record(
            "intent",
            machine_id=offer.machine_id,
            gpu_names=list(offer.gpu_names),
            gpu_count=self.spec.gpu_count,
            price_hourly=offer.price_hourly,
            message=best_offer_summary(offer),
            extra={"hours": self.spec.expected_hours, "regions": self.spec.normalized_regions()},
        )

        # 第二步：真正下单。这里刻意不重试——超时就当作结果不明。
        try:
            order = self.client.create_payg_instance(
                machine_id=offer.machine_id,
                gpu_count=self.spec.gpu_count,
                image_uuid=self.spec.image_uuid,
                instance_name=self.spec.instance_name,
                expand_data_disk_gb=self.spec.expand_data_disk_gb,
            )
        except NetworkError as exc:
            # 结果不明：绝不重试，转为只读对账。
            self.journal.record(
                "ambiguous",
                machine_id=offer.machine_id,
                gpu_names=list(offer.gpu_names),
                gpu_count=self.spec.gpu_count,
                price_hourly=offer.price_hourly,
                status="network_error",
                message=str(exc),
            )
            recovered = self._reconcile(offer.machine_id)
            if recovered is not None:
                outcome.status = "confirmed"
                outcome.ordered = True
                outcome.order = OrderResult(
                    instance_uuid=recovered.uuid,
                    machine_id=offer.machine_id,
                    gpu_count=self.spec.gpu_count,
                    price_hourly=offer.price_hourly,
                    confirmed=True,
                )
                outcome.message = f"下单请求超时，但对账发现实例已创建：{recovered.uuid}"
                self.journal.record(
                    "result", machine_id=offer.machine_id, instance_uuid=recovered.uuid,
                    status="confirmed", message=outcome.message,
                )
                self._notify_success(outcome)
                return outcome

            self._blocked_reason = (
                f"上一次下单结果不明（{exc}），已停止下单。请到 AutoDL 控制台核对实例列表，"
                "确认后再重新运行。"
            )
            outcome.status = "ambiguous"
            outcome.message = self._blocked_reason
            self._notify_once("AutoDL 下单结果不明", self._blocked_reason, event="ambiguous")
            return outcome
        except APIError as exc:
            self.journal.record(
                "result",
                machine_id=offer.machine_id,
                gpu_names=list(offer.gpu_names),
                gpu_count=self.spec.gpu_count,
                price_hourly=offer.price_hourly,
                status="failed",
                message=str(exc),
            )
            outcome.status = "error"
            outcome.message = f"下单失败：{exc}"
            return outcome

        # 第三步：下单成功，接着开机（失败不影响实例本身，只是没跑起来）。
        order.price_hourly = offer.price_hourly
        outcome.ordered = True
        outcome.order = order
        self.journal.record(
            "result",
            machine_id=offer.machine_id,
            gpu_names=list(offer.gpu_names),
            gpu_count=self.spec.gpu_count,
            price_hourly=offer.price_hourly,
            instance_uuid=order.instance_uuid,
            status="created",
            message="实例已创建",
            extra={"hours": self.spec.expected_hours},
        )

        if order.instance_uuid:
            try:
                self.client.power_on(order.instance_uuid)
            except AutoDLError as exc:
                self.journal.record(
                    "notice", instance_uuid=order.instance_uuid, status="power_on_failed",
                    message=str(exc),
                )

        confirmed = self._confirm(order.instance_uuid, offer.machine_id)
        if confirmed:
            outcome.status = "confirmed"
            outcome.order.confirmed = True
            outcome.message = f"已抢到：实例 {order.instance_uuid}"
            self.journal.record(
                "result", machine_id=offer.machine_id, instance_uuid=order.instance_uuid,
                status="confirmed", message="实例已确认存在",
            )
        else:
            outcome.status = "created"
            outcome.message = (
                f"已下单，实例 {order.instance_uuid}，但在实例列表里还没确认到，"
                "请到控制台核对"
            )
        self._notify_success(outcome)
        return outcome

    # ------------------------------------------------------------------ 对账

    def _find_existing_instance(self, machine_id: str):
        """在实例列表里找这台机器上已有的实例（幂等保护）。"""
        try:
            instances = self.client.list_instances()
        except AutoDLError:
            return None
        for instance in instances:
            if instance.machine_id and instance.machine_id == machine_id:
                return instance
        return None

    def _reconcile(self, machine_id: str):
        """只读对账：看这台机器上是否已经出现了实例。"""
        try:
            instances = self.client.list_instances()
        except AutoDLError:
            return None
        for instance in instances:
            if instance.machine_id and instance.machine_id == machine_id:
                return instance
        return None

    def _confirm(self, instance_uuid: str, machine_id: str) -> bool:
        """确认实例真的建出来了。"""
        if not instance_uuid:
            return False
        for attempt in range(CONFIRM_ATTEMPTS):
            try:
                if self.client.instance_exists(instance_uuid):
                    return True
            except AutoDLError:
                pass
            if attempt < CONFIRM_ATTEMPTS - 1:
                self._sleep(CONFIRM_INTERVAL)
        return self._reconcile(machine_id) is not None

    def _detect_block(self) -> str:
        """检查日志里有没有「结果不明」的旧记录，有就阻止继续下单。"""
        pending = self.journal.pending_intents()
        if not pending:
            return ""
        last = pending[-1]
        return (
            f"日志中存在未收尾的下单记录（{last.get('time')} 机器 {last.get('machine_id')}），"
            "为避免重复扣费已停止下单。请先到 AutoDL 控制台核对实例列表，"
            "确认后在日志里补一条 result 记录，或删除该条 intent 记录后再运行。"
        )

    # ------------------------------------------------------------------ 通知

    def _notify_once(self, title: str, content: str, *, event: str) -> None:
        try:
            self.notifier.send(title, content, event=event, name="Autodl-AutoConnect")
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响抢卡
            self._log(f"[通知失败] {exc}")

    def _notify_success(self, outcome: WatchOutcome) -> None:
        order = outcome.order
        content = (
            f"实例 {order.instance_uuid if order else ''}\n"
            f"机器 {order.machine_id if order else ''}\n"
            f"卡数 {order.gpu_count if order else ''}\n"
            f"单价 {order.price_hourly if order else ''} 元/时\n"
            f"{outcome.message}"
        )
        self._notify_once("AutoDL 抢卡成功", content, event="order_created")

    # ------------------------------------------------------------------ 循环

    def run(self, *, once: bool = False, max_iterations: int | None = None) -> WatchOutcome:
        """主循环。``once=True`` 只跑一轮。"""
        deadline = (
            self._clock() + self.spec.timeout_minutes * 60
            if self.spec.timeout_minutes > 0
            else None
        )
        iteration = 0
        last: WatchOutcome = WatchOutcome()

        while True:
            iteration += 1
            last = self.check_once()
            if last.status in ("created", "confirmed", "ambiguous", "observed"):
                if last.status in ("created", "confirmed") or once:
                    return last
                if last.status == "observed" and not self.spec.paid_consent:
                    # 只观察模式：通知一次就退出，避免刷屏。
                    return last
            if once:
                return last
            if max_iterations is not None and iteration >= max_iterations:
                return last
            if deadline is not None and self._clock() >= deadline:
                last.status = last.status if last.status != "no_match" else "timeout"
                last.message = last.message or "监测超时结束"
                return last
            self._sleep(self.spec.poll_seconds)


def best_offer_summary(offer: MachineOffer) -> str:
    """给日志用的报价摘要。"""
    parts = [offer.region_sign or "未知地区"]
    if offer.gpu_names:
        parts.append(" ".join(offer.gpu_names))
    parts.append(f"空闲 {offer.idle_gpu_num}")
    if offer.price_hourly is not None:
        parts.append(f"单价 {offer.price_hourly} 元/时")
    return " · ".join(parts)