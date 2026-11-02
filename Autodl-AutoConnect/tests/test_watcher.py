"""抢卡主循环的端到端测试：对着假 AutoDL 服务跑完整的「查→匹配→下单→核对→通知」。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autodl_autoconnect.api import AutoDLClient
from autodl_autoconnect.config import NotifyConfig, TaskSpec
from autodl_autoconnect.httpclient import HttpClient
from autodl_autoconnect.journal import Journal
from autodl_autoconnect.notifier import Notifier
from autodl_autoconnect.watcher import Watcher

from .fake_autodl import FakeAutoDL


class WatcherTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeAutoDL()
        self.fake.__enter__()
        self.client = AutoDLClient(
            HttpClient(self.fake.token, base_url=self.fake.base_url, timeout=5, max_retries=1)
        )
        self._dir = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self._dir.name) / "journal.jsonl")
        self.notifications: list[tuple[str, str]] = []
        self.webhooks: list[dict] = []
        self.notifier = Notifier(
            NotifyConfig(webhook_url="http://example.invalid/hook", desktop=False),
            http_post=self._fake_post,
            printer=self._capture,
        )
        self.spec = TaskSpec(
            gpu_models=["RTX 4090"],
            gpu_count=1,
            regions=["westDC2"],
            image_uuid="img-test",
            paid_consent=True,
            poll_seconds=0.01,
        )

    def tearDown(self) -> None:
        self.fake.__exit__(None, None, None)
        self._dir.cleanup()

    def _capture(self, text: str) -> None:
        self.notifications.append(("desktop", text))

    def _fake_post(self, url: str, body, headers) -> tuple[int, str]:
        self.webhooks.append({"url": url, "body": body, "headers": headers})
        return 200, '{"code":"Success"}'

    def make_watcher(self, spec: TaskSpec | None = None) -> Watcher:
        return Watcher(
            self.client,
            spec or self.spec,
            journal=self.journal,
            notifier=self.notifier,
            sleep=lambda _: None,
        )


class ObserveModeTest(WatcherTestBase):
    def test_no_consent_only_notifies(self):
        """没有授权时，只通知，绝不下单。"""
        self.fake.add_machine(machine_id="m-1", gpu_idle_num=4)
        self.spec.paid_consent = False
        watcher = self.make_watcher()
        outcome = watcher.check_once()

        self.assertTrue(outcome.matched)
        self.assertFalse(outcome.ordered)
        self.assertEqual(outcome.status, "observed")
        self.assertEqual(self.fake.state.create_calls, [])
        self.assertEqual(len(self.webhooks), 1)

    def test_no_match_returns_no_match(self):
        self.fake.add_machine(machine_id="m-1", gpu_type_name=["A100"])
        outcome = self.make_watcher().check_once()
        self.assertFalse(outcome.matched)
        self.assertEqual(outcome.status, "no_match")
        self.assertEqual(self.fake.state.create_calls, [])


class GrabTest(WatcherTestBase):
    def test_grab_creates_and_confirms(self):
        self.fake.add_machine(machine_id="m-1", gpu_idle_num=4, pay_price=2.5)
        watcher = self.make_watcher()
        outcome = watcher.check_once()

        self.assertTrue(outcome.ordered)
        self.assertEqual(outcome.status, "confirmed")
        assert outcome.order is not None
        self.assertTrue(outcome.order.instance_uuid)
        self.assertEqual(len(self.fake.state.create_calls), 1)
        self.assertEqual(len(self.fake.state.instances), 1)
        self.assertEqual(self.fake.state.instances[0]["gpu_num"], 1)
        # 开机也被调用了
        self.assertEqual(len(self.fake.state.power_on_calls), 1)
        # 通知也发出去了
        titles = [w["body"].get("title") for w in self.webhooks]
        self.assertIn("AutoDL 抢卡成功", titles)

    def test_journal_records_intent_and_result(self):
        self.fake.add_machine(machine_id="m-1")
        self.make_watcher().check_once()
        kinds = [record["kind"] for record in self.journal.read_all()]
        self.assertIn("intent", kinds)
        self.assertIn("result", kinds)
        # 有收尾就不该再被判定为「结果不明」
        self.assertEqual(self.journal.pending_intents(), [])

    def test_idempotent_skips_duplicate(self):
        """这台机器上已经有实例时，不能再下第二单。"""
        self.fake.add_machine(machine_id="m-1")
        self.fake.add_instance(uuid="existing-1", machine_id="m-1")
        outcome = self.make_watcher().check_once()
        self.assertFalse(outcome.ordered)
        self.assertEqual(outcome.status, "observed")
        self.assertEqual(self.fake.state.create_calls, [])

    def test_balance_guard_blocks_order(self):
        """余额不够时必须拒绝下单。"""
        self.fake.add_machine(machine_id="m-1", pay_price=2.5)
        self.fake.state.balance_milli = 100  # 0.1 元
        self.spec.expected_hours = 10.0
        self.spec.balance_reserve = 0.0
        outcome = self.make_watcher().check_once()
        self.assertFalse(outcome.ordered)
        self.assertEqual(outcome.status, "error")
        self.assertIn("余额", outcome.message)
        self.assertEqual(self.fake.state.create_calls, [])

    def test_price_cap_blocks_order(self):
        self.fake.add_machine(machine_id="m-1", pay_price=9.9)
        self.spec.max_price_hourly = 3.0
        outcome = self.make_watcher().check_once()
        self.assertFalse(outcome.ordered)
        self.assertEqual(self.fake.state.create_calls, [])

    def test_multi_gpu_requirement(self):
        self.fake.add_machine(machine_id="m-1", gpu_idle_num=1)
        self.spec.gpu_count = 4
        outcome = self.make_watcher().check_once()
        self.assertFalse(outcome.matched)
        self.assertEqual(self.fake.state.create_calls, [])


class AmbiguityTest(WatcherTestBase):
    def test_network_error_never_retries_and_blocks(self):
        """下单超时时：只发一次请求，标记为结果不明，并停止后续下单。"""
        self.fake.add_machine(machine_id="m-1")
        self.fake.state.fail_next_create_with = ConnectionResetError("服务端断开")

        watcher = self.make_watcher()
        outcome = watcher.check_once()

        # 关键：只请求了一次，没有自动重试（否则可能重复扣费）
        self.assertEqual(len(self.fake.state.create_calls), 1)
        self.assertEqual(outcome.status, "ambiguous")
        self.assertIn("已停止下单", outcome.message)
        self.assertIn("核对", outcome.message)

        kinds = [record["kind"] for record in self.journal.read_all()]
        self.assertIn("intent", kinds)
        self.assertIn("ambiguous", kinds)

        # 再跑一次会被拦住，不再下单
        self.fake.state.create_calls.clear()
        second = watcher.check_once()
        self.assertEqual(second.status, "ambiguous")
        self.assertEqual(self.fake.state.create_calls, [])

    def test_ambiguity_detected_from_journal_on_new_run(self):
        """换一个进程（新的 Watcher）也应该被日志拦住。"""
        self.fake.add_machine(machine_id="m-1")
        self.journal.record("intent", machine_id="m-1", gpu_count=1, price_hourly=2.5)
        outcome = self.make_watcher().check_once()
        self.assertEqual(outcome.status, "ambiguous")
        self.assertEqual(self.fake.state.create_calls, [])


class RunLoopTest(WatcherTestBase):
    def test_run_once_returns_first_match(self):
        self.fake.add_machine(machine_id="m-1")
        outcome = self.make_watcher().run(once=True)
        self.assertEqual(outcome.status, "confirmed")

    def test_run_stops_after_creating(self):
        self.fake.add_machine(machine_id="m-1")
        watcher = self.make_watcher()
        outcome = watcher.run(max_iterations=5)
        self.assertEqual(outcome.status, "confirmed")
        self.assertEqual(len(self.fake.state.create_calls), 1)


if __name__ == "__main__":
    unittest.main()
