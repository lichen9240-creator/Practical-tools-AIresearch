"""意图日志（intent journal）。

为什么要写日志：抢卡是「花钱的动作」，而且卡只存在几十秒。
一旦下单请求超时/断线，你无法区分「没下成功」和「下成功了但没收到回包」。
本模块的规则是：

1. **先写意图，再点下单**——把打算做什么落盘；
2. **结果不明时绝不重试下单**，改为只读对账；
3. 任何时候都能从日志里回答「我到底下过几单、花了多少钱」。

日志是纯追加的 JSONL，一行一条记录，方便 ``grep`` 和机器解析。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import state_dir
from .errors import redact

__all__ = ["Journal", "IntentRecord"]


@dataclass(frozen=True)
class IntentRecord:
    """一条意图记录。"""

    ts: float
    kind: str
    """``intent`` / ``result`` / ``ambiguous`` / ``skipped`` / ``notice``。"""

    machine_id: str = ""
    gpu_names: list[str] | None = None
    gpu_count: int = 0
    price_hourly: float | None = None
    instance_uuid: str = ""
    status: str = ""
    message: str = ""
    extra: dict | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "kind": self.kind,
            "machine_id": self.machine_id,
            "gpu_names": self.gpu_names or [],
            "gpu_count": self.gpu_count,
            "price_hourly": self.price_hourly,
            "instance_uuid": self.instance_uuid,
            "status": self.status,
            "message": self.message,
            "extra": self.extra or {},
        }


class Journal:
    """追加写的意图日志。"""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else state_dir() / "journal.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写

    def append(self, record: IntentRecord) -> IntentRecord:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        # 追加写 + 立刻 flush：进程被强杀也不丢最后一行的意图。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def record(
        self,
        kind: str,
        *,
        machine_id: str = "",
        gpu_names: Iterable[str] | None = None,
        gpu_count: int = 0,
        price_hourly: float | None = None,
        instance_uuid: str = "",
        status: str = "",
        message: str = "",
        extra: dict | None = None,
    ) -> IntentRecord:
        return self.append(
            IntentRecord(
                ts=time.time(),
                kind=kind,
                machine_id=machine_id,
                gpu_names=list(gpu_names or []),
                gpu_count=gpu_count,
                price_hourly=price_hourly,
                instance_uuid=instance_uuid,
                status=status,
                message=redact(message, 500),
                extra=extra or {},
            )
        )

    # ------------------------------------------------------------------ 读

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def pending_intents(self, *, window_seconds: float = 3600.0) -> list[dict]:
        """找出「写了意图但没有对应结果」的记录。

        这类记录意味着上一次下单的结果不明——必须人工核对，
        不能自动重试。``window_seconds`` 限制只关心最近的一段时间。

        判定规则：一条 intent 记录，如果**在它之后**没有同机器的 result 记录，
        就认为没有收尾。
        """
        records = self.read_all()
        now = time.time()
        results = [r for r in records if r.get("kind") == "result"]
        pending: list[dict] = []
        for record in records:
            if record.get("kind") != "intent":
                continue
            ts = record.get("ts", 0)
            if now - ts > window_seconds:
                continue
            machine_id = record.get("machine_id")
            resolved = any(
                (r.get("ts", 0) >= ts) and (not machine_id or r.get("machine_id") == machine_id)
                for r in results
            )
            if not resolved:
                pending.append(record)
        return pending

    def ack_pending(self, *, note: str = "人工核对后收尾（journal --ack）") -> int:
        """给所有「未收尾的意图」补一条 result 记录，解除对下单的阻塞。

        只应在**人工确认过 AutoDL 控制台的实例列表**之后使用：
        这个动作不会去查接口，只是把「我知道结果了」写进日志。
        """
        pending = self.pending_intents()
        for record in pending:
            self.record(
                "result",
                machine_id=record.get("machine_id", ""),
                instance_uuid=record.get("instance_uuid", ""),
                status="manual_resolved",
                message=note,
                extra={"acked_intent_ts": record.get("ts")},
            )
        return len(pending)

    def summary(self) -> dict:
        """给 ``status`` / ``doctor`` 用的统计。"""
        records = self.read_all()
        intents = [r for r in records if r.get("kind") == "intent"]
        results = [r for r in records if r.get("kind") == "result"]
        ambiguous = [r for r in records if r.get("kind") == "ambiguous"]
        created = [r for r in results if r.get("status") in ("created", "confirmed")]
        return {
            "path": str(self.path),
            "records": len(records),
            "intents": len(intents),
            "results": len(results),
            "created": len(created),
            "ambiguous": len(ambiguous),
            "estimated_spend": round(
                sum(
                    (r.get("price_hourly") or 0)
                    * (r.get("gpu_count") or 0)
                    * ((r.get("extra") or {}).get("hours") or 0)
                    for r in created
                ),
                4,
            ),
        }
