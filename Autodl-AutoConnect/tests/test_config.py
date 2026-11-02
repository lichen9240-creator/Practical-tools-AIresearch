"""地区归一化与配置校验的测试。"""

from __future__ import annotations

import unittest

from autodl_autoconnect.config import TaskSpec
from autodl_autoconnect.errors import ConfigError
from autodl_autoconnect.regions import normalize_region, region_display_name


class RegionTest(unittest.TestCase):
    def test_known_codes_pass_through(self):
        self.assertEqual(normalize_region("westDC2"), "westDC2")
        self.assertEqual(normalize_region("beijingDC2"), "beijingDC2")

    def test_chinese_names(self):
        self.assertEqual(normalize_region("西北B区"), "westDC2")
        self.assertEqual(normalize_region("北京B区"), "beijingDC2")

    def test_aliases(self):
        self.assertEqual(normalize_region("west"), "westDC2")
        self.assertEqual(normalize_region("bj"), "beijingDC1")

    def test_unknown_region_passes_through(self):
        """官方将来加了新地区，本工具不该崩。"""
        self.assertEqual(normalize_region("newDC9"), "newDC9")

    def test_display_name(self):
        self.assertEqual(region_display_name("westDC2"), "西北B区")
        self.assertEqual(region_display_name("unknown"), "unknown")


class ConfigTest(unittest.TestCase):
    def test_requires_a_model(self):
        with self.assertRaises(ConfigError):
            TaskSpec().validate()

    def test_accepts_regex_only(self):
        TaskSpec(gpu_model_regex=r"RTX 40(90|90D)$").validate()

    def test_rejects_bad_regex(self):
        with self.assertRaises(ConfigError):
            TaskSpec(gpu_model_regex="[unclosed").validate()

    def test_rejects_bad_gpu_count(self):
        with self.assertRaises(ConfigError):
            TaskSpec(gpu_models=["4090"], gpu_count=0).validate()

    def test_regions_normalized(self):
        spec = TaskSpec(gpu_models=["4090"], regions=["西北B区", "westDC2", "西北B区"])
        self.assertEqual(spec.normalized_regions(), ["westDC2"])

    def test_any_region_overrides(self):
        spec = TaskSpec(gpu_models=["4090"], regions=["西北B区"], any_region=True)
        self.assertEqual(spec.normalized_regions(), [])

    def test_from_dict_ignores_unknown_keys(self):
        spec = TaskSpec.from_dict({"gpu_models": ["4090"], "不存在的键": 1})
        self.assertEqual(spec.gpu_models, ["4090"])

    def test_notify_roundtrip(self):
        spec = TaskSpec.from_dict(
            {"gpu_models": ["4090"], "notify": {"webhook_url": "http://x", "未知": 1}}
        )
        self.assertEqual(spec.notify.webhook_url, "http://x")


if __name__ == "__main__":
    unittest.main()
