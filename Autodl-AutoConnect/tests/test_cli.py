"""CLI 的测试：重点是 JSON 输出与退出码（Agent 靠这个做分支）。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from autodl_autoconnect.cli import (
    EXIT_AMBIGUOUS,
    EXIT_AUTH,
    EXIT_NO_MATCH,
    EXIT_OK,
    main,
)

from .fake_autodl import FakeAutoDL


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeAutoDL()
        self.fake.__enter__()
        self._dir = tempfile.TemporaryDirectory()
        self.config = Path(self._dir.name) / "config.json"
        # 日志也隔离到临时目录，绝不写到用户真实的 ~/.autodl-autoconnect/journal.jsonl
        self.journal = Path(self._dir.name) / "journal.jsonl"
        self.config.write_text(
            json.dumps(
                {
                    "gpu_models": ["RTX 4090"],
                    "gpu_count": 1,
                    "regions": ["westDC2"],
                    "image_uuid": "img-test",
                    "paid_consent": False,
                    "poll_seconds": 0.01,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.fake.__exit__(None, None, None)
        self._dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--config",
                    str(self.config),
                    "--token",
                    self.fake.token,
                    "--base-url",
                    self.fake.base_url,
                    "--json",
                    *args,
                    "--journal",
                    str(self.journal),
                ]
            )
        return code, buffer.getvalue()

    # ------------------------------------------------------------- 基本

    def test_balance_json(self):
        self.fake.state.balance_milli = 4321
        code, out = self.run_cli("balance")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertAlmostEqual(payload["assets"], 4.321, places=3)

    def test_stock_json(self):
        self.fake.add_machine(machine_id="m-1", gpu_type_name=["RTX 4090"], gpu_idle_num=3)
        code, out = self.run_cli("stock")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(len(payload["offers"]), 1)
        self.assertEqual(payload["offers"][0]["gpu_names"], ["RTX 4090"])

    def test_wrong_token_gives_auth_exit_code(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--config",
                    str(self.config),
                    "--token",
                    "wrong-token",
                    "--base-url",
                    self.fake.base_url,
                    "--json",
                    "balance",
                ]
            )
        self.assertEqual(code, EXIT_AUTH)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("鉴权", payload["error"]["message"])

    # ------------------------------------------------------------- 抢卡

    def test_grab_without_consent_returns_observed(self):
        self.fake.add_machine(machine_id="m-1")
        code, out = self.run_cli("grab")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload["matched"])
        self.assertFalse(payload["ordered"])
        self.assertEqual(self.fake.state.create_calls, [])

    def test_grab_with_consent_creates_instance(self):
        self.fake.add_machine(machine_id="m-1")
        code, out = self.run_cli("grab", "--yes")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload["ordered"])
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(len(self.fake.state.create_calls), 1)

    def test_grab_without_match_returns_exit_code_4(self):
        self.fake.add_machine(machine_id="m-1", gpu_type_name=["A100"])
        code, out = self.run_cli("grab")
        self.assertEqual(code, EXIT_NO_MATCH)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "no_match")

    def test_grab_ambiguous_returns_exit_code_5(self):
        self.fake.add_machine(machine_id="m-1")
        self.fake.state.fail_next_create_with = ConnectionResetError("服务端断开")
        code, out = self.run_cli("grab", "--yes")
        self.assertEqual(code, EXIT_AMBIGUOUS)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ambiguous")
        # 只发了一次下单请求
        self.assertEqual(len(self.fake.state.create_calls), 1)

    # ------------------------------------------------------------- 其它

    def test_journal_json(self):
        self.fake.add_machine(machine_id="m-1")
        self.run_cli("grab", "--yes")
        code, out = self.run_cli("journal")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertIn("summary", payload)

    def test_config_show(self):
        code, out = self.run_cli("config", "show")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["config"]["gpu_models"], ["RTX 4090"])


if __name__ == "__main__":
    unittest.main()
