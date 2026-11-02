"""匹配逻辑：一条机器报价是否满足任务条件。

设计原则：**宁可少抢，不可乱抢**。任何无法核实的条件（价格拿不到、型号拿不准）
都判为不匹配，并把原因写清楚，而不是默认放行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import TaskSpec
from .models import MachineOffer

__all__ = ["MatchResult", "normalize_gpu_name", "match_offer", "select_best"]

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")
_ANY = {"", "*", "any", "任意", "全部"}


def normalize_gpu_name(name: str) -> str:
    """把型号名归一化成便于比较的形式：``"RTX 4090"`` -> ``"rtx4090"``。"""
    if not name:
        return ""
    return re.sub(r"[^0-9a-z]+", "", name.lower())


def _tokens(name: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(name.lower()) if t]


def _name_matches(offer_names: list[str], wanted: str) -> bool:
    """型号匹配。

    规则：
    1. 归一化后完全相同 -> 命中（``RTX 4090`` vs ``rtx-4090``）；
    2. 用户写的是简写时按 token 前缀命中（``4090`` 命中 ``RTX 4090D``）；
    3. 否则不命中。
    """
    want_norm = normalize_gpu_name(wanted)
    if not want_norm:
        return False
    want_tokens = _tokens(wanted)
    for name in offer_names:
        if not name:
            continue
        if normalize_gpu_name(name) == want_norm:
            return True
        offer_tokens = _tokens(name)
        for want_token in want_tokens:
            for offer_token in offer_tokens:
                if offer_token == want_token:
                    return True
                # 允许 4090 命中 4090D 这类「同系列不同后缀」的写法
                if (
                    len(offer_token) > len(want_token)
                    and offer_token.startswith(want_token)
                    and len(offer_token) - len(want_token) <= 2
                    and want_token.isdigit()
                ):
                    return True
    return False


@dataclass(frozen=True)
class MatchResult:
    """一条报价的匹配结论。"""

    matched: bool
    reason: str = ""
    """不匹配时说明原因；匹配时是一句话摘要。"""

    offer: MachineOffer | None = None

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "reason": self.reason,
            "offer": self.offer.to_dict() if self.offer else None,
        }


def match_offer(offer: MachineOffer, spec: TaskSpec) -> MatchResult:
    """判断一条机器报价是否满足任务条件。"""
    if offer is None:
        return MatchResult(False, "报价为空")

    if offer.offline:
        return MatchResult(False, f"机器不在线（on_line={offer.on_line}）", offer)

    if not offer.has_free_slot:
        return MatchResult(
            False,
            f"该机器已挂满实例（{offer.binding_instance_num}/{offer.max_instance_num}）",
            offer,
        )

    if offer.idle_gpu_num < spec.gpu_count:
        return MatchResult(
            False, f"空闲卡不足（{offer.idle_gpu_num} < 需求 {spec.gpu_count}）", offer
        )

    wanted_regions = spec.normalized_regions()
    if wanted_regions and offer.region_sign and offer.region_sign not in wanted_regions:
        return MatchResult(
            False, f"地区不符（{offer.region_sign} 不在 {wanted_regions}）", offer
        )

    if spec.gpu_model_regex:
        pattern = re.compile(spec.gpu_model_regex, re.IGNORECASE)
        if not any(pattern.search(name) for name in offer.gpu_names if name):
            return MatchResult(
                False, f"型号不匹配正则 {spec.gpu_model_regex}（{offer.gpu_names}）", offer
            )
    else:
        wanted = [w for w in spec.gpu_models if w.strip() and w.strip().lower() not in _ANY]
        if wanted and not any(_name_matches(list(offer.gpu_names), w) for w in wanted):
            return MatchResult(
                False, f"型号不符（候选 {offer.gpu_names}，需求 {wanted}）", offer
            )

    price = offer.price_hourly
    if spec.max_price_hourly is not None:
        if price is None:
            if spec.require_known_price:
                return MatchResult(False, "拿不到价格，无法核实是否超出价格上限", offer)
        elif price > spec.max_price_hourly:
            return MatchResult(
                False, f"单卡价格 {price}/时 超出上限 {spec.max_price_hourly}/时", offer
            )

    if spec.max_total_cost is not None:
        if price is None:
            if spec.require_known_price:
                return MatchResult(False, "拿不到价格，无法核实是否超出总预算", offer)
        else:
            total = price * spec.gpu_count * spec.expected_hours
            if total > spec.max_total_cost:
                return MatchResult(
                    False,
                    f"预计总花费 {total:.2f} 元 超出上限 {spec.max_total_cost:.2f} 元",
                    offer,
                )

    summary = (
        f"{offer.region_sign or '未知地区'} {'/'.join(offer.gpu_names) or '未知型号'} "
        f"空闲 {offer.idle_gpu_num}"
    )
    if price is not None:
        summary += f" 单卡 {price}/时"
    return MatchResult(True, summary, offer)


def select_best(results: list[MatchResult], spec: TaskSpec) -> MatchResult | None:
    """从多个命中里挑一个最优的：先看能不能凑够卡数，再看价格，再看空闲卡多的。"""
    matched = [r for r in results if r.matched and r.offer is not None]
    if not matched:
        return None

    def sort_key(result: MatchResult):
        offer = result.offer
        price = offer.price_hourly if offer.price_hourly is not None else float("inf")
        return (
            -min(offer.idle_gpu_num, 8),  # 优先能一次给足卡数的
            price,                          # 其次便宜的
            -offer.idle_gpu_num,            # 再次空闲多的
            offer.machine_id,               # 最后按 id 稳定排序
        )

    return sorted(matched, key=sort_key)[0]
