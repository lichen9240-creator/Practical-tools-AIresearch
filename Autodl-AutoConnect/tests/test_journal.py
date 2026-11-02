"""意图日志的测试：重点是「结果不明要能被发现」。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from autodl_autoconnect.journal import Journal


class JournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "journal.jsonl"
        self.journal = Journal(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_intent_without_result_is_pending(self):
        self.journal.record("intent", machine_id="m-1", gpu_count=1)
        pending = self.journal.pending_intents()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["machine_id"], "m-1")

    def test_result_resolves_intent(self):
        self.journal.record("intent", machine_id="m-1", gpu_count=1)
        self.journal.record("result", machine_id="m-1", instance_uuid="i-1", status="created")
        self.assertEqual(self.journal.pending_intents(), [])

    def test_result_for_other_machine_does_not_resolve(self):
        """别的机器的 result 不算这条 intent 的收尾。"""
        self.journal.record("intent", machine_id="m-1", gpu_count=1)
        self.journal.record("result", machine_id="m-2", instance_uuid="i-9", status="created")
        self.assertEqual(len(self.journal.pending_intents()), 1)

    def test_old_intents_are_ignored(self):
        self.journal.record("intent", machine_id="m-1", gpu_count=1)
        # 把时间戳改老，模拟历史遗留
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text(
            "\n".join(lines).replace('"ts":', '"ts":') + "\n", encoding="utf-8"
        )
        records = self.journal.read_all()
        records[0]["ts"] = time.time() - 7200
        import json

        self.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self.journal.pending_intents(window_seconds=3600), [])

    def test_summary_counts(self):
        self.journal.record("intent", machine_id="m-1", gpu_count=2, price_hourly=2.0)
        self.journal.record(
            "result", machine_id="m-1", instance_uuid="i-1", status="created",
            gpu_count=2, price_hourly=2.0, extra={"hours": 3},
        )
        summary = self.journal.summary()
        self.assertEqual(summary["intents"], 1)
        self.assertEqual(summary["results"], 1)
        self.assertEqual(summary["created"], 1)
        self.assertAlmostEqual(summary["estimated_spend"], 12.0, places=4)

    def test_secret_is_redacted_in_messages(self):
        self.journal.record("notice", message="token=abcdef123456789012345678901234")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("abcdef123456789012345678901234", raw)


if __name__ == "__main__":
    unittest.main()
