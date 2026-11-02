"""型号 / 地区 / 价格 / 卡数 匹配逻辑的测试。"""

from __future__ import annotations

import unittest

from autodl_autoconnect.config import TaskSpec
from autodl_autoconnect.matcher import match_offer, normalize_gpu_name, select_best
from autodl_autoconnect.models import MachineOffer


def offer(**kwargs) -> MachineOffer:
    base = {
        "machine_id": "m-1",
        "region_sign": "westDC2",
        "gpu_names": ("RTX 4090",),
        "idle_gpu_num": 4,
        "total_gpu_num": 10,
        "max_instance_num": 8,
        "binding_instance_num": 0,
        "on_line": 1,
        "price_hourly": 2.5,
    }
    base.update(kwargs)
    return MachineOffer(**base)


class NormalizeTest(unittest.TestCase):
    def test_normalize_strips_noise(self):
        self.assertEqual(normalize_gpu_name("RTX 4090"), "rtx4090")
        self.assertEqual(normalize_gpu_name("rtx-4090"), "rtx4090")
        self.assertEqual(normalize_gpu_name("  A100 80G "), "a10080g")


class MatchTest(unittest.TestCase):
    def test_exact_model_matches(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1)
        result = match_offer(offer(), spec)
        self.assertTrue(result.matched, result.reason)

    def test_short_model_matches_with_suffix(self):
        """"4090" 应该能匹配 "RTX 4090D"，这是用户最常见的写法。"""
        spec = TaskSpec(gpu_models=["4090"], gpu_count=1)
        result = match_offer(offer(gpu_names=("RTX 4090D",)), spec)
        self.assertTrue(result.matched, result.reason)

    def test_different_model_rejected(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1)
        result = match_offer(offer(gpu_names=("A100",)), spec)
        self.assertFalse(result.matched)
        self.assertIn("型号不符", result.reason)

    def test_regex_model(self):
        spec = TaskSpec(gpu_models=[], gpu_model_regex=r"RTX 40(90|90D)$", gpu_count=1)
        self.assertTrue(match_offer(offer(gpu_names=("RTX 4090D",)), spec).matched)
        self.assertFalse(match_offer(offer(gpu_names=("A100",)), spec).matched)

    def test_gpu_count_insufficient(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=4)
        result = match_offer(offer(idle_gpu_num=2), spec)
        self.assertFalse(result.matched)
        self.assertIn("空闲卡不足", result.reason)

    def test_region_filter(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1, regions=["westDC2"])
        self.assertTrue(match_offer(offer(region_sign="westDC2"), spec).matched)
        self.assertFalse(match_offer(offer(region_sign="beijingDC2"), spec).matched)

    def test_region_accepts_chinese_name(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1, regions=["西北B区"])
        self.assertTrue(match_offer(offer(region_sign="westDC2"), spec).matched)

    def test_offline_machine_rejected(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1)
        result = match_offer(offer(on_line=4), spec)
        self.assertFalse(result.matched)
        self.assertIn("不在线", result.reason)

    def test_full_machine_rejected(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1)
        result = match_offer(offer(binding_instance_num=8, max_instance_num=8), spec)
        self.assertFalse(result.matched)
        self.assertIn("挂满", result.reason)

    def test_price_cap(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1, max_price_hourly=2.0)
        result = match_offer(offer(price_hourly=2.5), spec)
        self.assertFalse(result.matched)
        self.assertIn("超出上限", result.reason)

    def test_unknown_price_is_rejected_when_cap_set(self):
        """拿不到价格时不能默认放行——这是「宁可少抢，不可乱抢」。"""
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1, max_price_hourly=5.0)
        result = match_offer(offer(price_hourly=None), spec)
        self.assertFalse(result.matched)
        self.assertIn("拿不到价格", result.reason)

    def test_total_cost_cap(self):
        # 单卡 2.5 元 × 2 卡 × 1 小时 = 5 元，预算 4 元 -> 超了
        spec = TaskSpec(
            gpu_models=["RTX 4090"], gpu_count=2, max_total_cost=4.0, expected_hours=1.0
        )
        result = match_offer(offer(price_hourly=2.5), spec)
        self.assertFalse(result.matched)
        self.assertIn("总花费", result.reason)

    def test_total_cost_exactly_at_cap_is_allowed(self):
        spec = TaskSpec(
            gpu_models=["RTX 4090"], gpu_count=2, max_total_cost=5.0, expected_hours=1.0
        )
        self.assertTrue(match_offer(offer(price_hourly=2.5), spec).matched)


class SelectBestTest(unittest.TestCase):
    def test_prefers_cheaper(self):
        spec = TaskSpec(gpu_models=["RTX 4090"], gpu_count=1)
        results = [
            match_offer(offer(machine_id="a", price_hourly=3.0), spec),
            match_offer(offer(machine_id="b", price_hourly=1.8), spec),
        ]
        best = select_best(results, spec)
        self.assertIsNotNone(best)
        assert best.offer is not None
        self.assertEqual(best.offer.machine_id, "b")

    def test_returns_none_when_nothing_matches(self):
        spec = TaskSpec(gpu_models=["A100"], gpu_count=1)
        results = [match_offer(offer(gpu_names=("RTX 4090",)), spec)]
        self.assertIsNone(select_best(results, spec))


if __name__ == "__main__":
    unittest.main()
