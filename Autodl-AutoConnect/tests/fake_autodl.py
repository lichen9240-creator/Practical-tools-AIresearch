"""一个假的 AutoDL 服务，用来端到端测试整条链路。

它按真实接口的路径、请求体与信封格式应答，因此测试跑的是**真实的 HTTP 往返**，
而不是把客户端的内部函数直接调用一遍——这样才能真正验证「请求体对不对、
解析对不对、超时后会不会误重试」。

启动：

    python -m tests.fake_autodl --port 8899

用法（跑测试）：

    with FakeAutoDL() as fake:
        client = AutoDLClient(HttpClient("tok", base_url=fake.base_url))
        ...
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["FakeAutoDL", "FakeState"]


class FakeState:
    """假服务的可变状态（库存、实例、余额等）。"""

    def __init__(self) -> None:
        self.token = "test-token"
        self.balance_milli = 100_000  # 100 元
        self.voucher_milli = 0
        self.machines: list[dict] = []
        self.instances: list[dict] = []
        self.orders: list[dict] = []
        self.next_instance_index = 1
        self.fail_next_create_with: Exception | None = None
        """设置后，下一次下单会抛出该异常（用来模拟超时/网络中断）。"""

        self.fail_create_with_code: str | None = None
        """设置后，下一次下单返回该业务错误码（确定性失败，不是结果不明）。"""

        self.create_calls: list[dict] = []
        self.power_on_calls: list[str] = []
        self.search_calls: list[dict] = []
        self.connection_count = 0
        """累计 TCP 连接数（HTTP/1.1 长连接下应远小于请求数）。"""
        self.response_delay = 0.0
        """每个响应的固定延迟（秒），用来模拟真实网络 RTT。"""
        self.lock = threading.Lock()

    def add_machine(self, **kwargs) -> dict:
        machine = {
            "machine_id": kwargs.get("machine_id", "m-1"),
            "region_sign": kwargs.get("region_sign", "westDC2"),
            "gpu_type_name": kwargs.get("gpu_type_name", ["RTX 4090"]),
            "gpu_idle_num": kwargs.get("gpu_idle_num", 4),
            "gpu_total_num": kwargs.get("gpu_total_num", 10),
            "max_instance_num": kwargs.get("max_instance_num", 8),
            "binding_instance_num": kwargs.get("binding_instance_num", 0),
            "on_line": kwargs.get("on_line", 1),
            "pay_price": kwargs.get("pay_price", 2.5),
            "disk_price": kwargs.get("disk_price", 0.02),
        }
        self.machines.append(machine)
        return machine


class _Handler(BaseHTTPRequestHandler):
    state: FakeState = None  # type: ignore[assignment]

    # HTTP/1.1 + Content-Length = 长连接；这样才能测出客户端有没有真的复用连接。
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        with self.state.lock:
            self.state.connection_count += 1

    # 静默，避免测试输出被日志淹没
    def log_message(self, fmt, *args):  # noqa: A003
        return

    # ------------------------------------------------------------- 工具

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send(self, status: int, payload: Any) -> None:
        if self.state.response_delay:
            time.sleep(self.state.response_delay)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data: Any = None, msg: str = "") -> None:
        self._send(200, {"code": "Success", "msg": msg, "data": data})

    def _fail(self, code: str, msg: str) -> None:
        self._send(200, {"code": code, "msg": msg, "data": None})

    # ------------------------------------------------------------- 路由

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()
        state = self.state

        if self.headers.get("Authorization") != state.token:
            self._send(200, {"code": "AuthorizeFailed", "msg": "登录超时，请重新登录", "data": None})
            return

        if path == "/api/v1/dev/wallet/balance":
            self._ok(
                {
                    "assets": state.balance_milli,
                    "accumulate": 0,
                    "voucher_balance": state.voucher_milli,
                }
            )
            return

        if path in ("/api/v1/machine/search", "/api/v1/user/machine/list"):
            with state.lock:
                state.search_calls.append(body)
            machines = self._filter_machines(body)
            self._ok({"list": machines, "result_total": len(machines)})
            return

        if path == "/api/v1/dev/machine/region/gpu_stock":
            region = body.get("region_sign")
            entries: dict[str, dict] = {}
            for machine in state.machines:
                if region and machine["region_sign"] != region:
                    continue
                for name in machine["gpu_type_name"]:
                    entry = entries.setdefault(name, {"idle_gpu_num": 0, "total_gpu_num": 0})
                    entry["idle_gpu_num"] += machine["gpu_idle_num"]
                    entry["total_gpu_num"] += machine["gpu_total_num"]
            self._ok([{name: stats} for name, stats in entries.items()])
            return

        if path == "/api/v1/order/instance/create/payg":
            self._handle_create(body)
            return

        if path == "/api/v1/instance":
            with state.lock:
                self._ok({"list": list(state.instances), "result_total": len(state.instances)})
            return

        if path == "/api/v1/instance/power_on":
            uuid = body.get("instance_uuid")
            with state.lock:
                state.power_on_calls.append(uuid)
                for item in state.instances:
                    if item["uuid"] == uuid:
                        item["status"] = "running"
            self._ok()
            return

        if path == "/api/v1/instance/power_off":
            self._ok()
            return

        if path == "/api/v1/instance/release":
            uuid = body.get("instance_uuid")
            with state.lock:
                state.instances = [i for i in state.instances if i["uuid"] != uuid]
            self._ok()
            return

        self._fail("NotFound", f"未知路径 {path}")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        state = self.state
        if self.headers.get("Authorization") != state.token:
            self._send(200, {"code": "AuthorizeFailed", "msg": "登录超时，请重新登录", "data": None})
            return
        if path == "/api/v1/instance/exist":
            uuid = ""
            if "instance_uuid=" in self.path:
                uuid = self.path.split("instance_uuid=")[1].split("&")[0]
            with state.lock:
                exists = any(i["uuid"] == uuid for i in state.instances)
            self._ok({"uuid": uuid if exists else ""})
            return
        if path == "/api/v1/region/list":
            self._ok([{"region_sign": "westDC2", "name": "西北B区"}])
            return
        if path == "/api/v1/machine/gpu_type":
            self._ok(["RTX 4090", "RTX 4090D", "A100"])
            return
        self._fail("NotFound", f"未知路径 {path}")

    # ------------------------------------------------------------- 业务

    def _filter_machines(self, body: dict) -> list[dict]:
        state = self.state
        regions = body.get("region_sign_list") or []
        charge_type = body.get("charge_type", "payg")
        result = []
        for machine in state.machines:
            if regions and machine["region_sign"] not in regions:
                continue
            if charge_type != "payg":
                continue
            result.append(machine)
        return result

    def _handle_create(self, body: dict) -> None:
        state = self.state
        info = body.get("instance_info") or {}
        price_info = body.get("price_info") or {}
        with state.lock:
            state.create_calls.append(body)
            failure = state.fail_next_create_with
            state.fail_next_create_with = None
            if failure is not None:
                raise failure  # 模拟服务端在处理中途断开连接
            error_code = state.fail_create_with_code
            state.fail_create_with_code = None
            if error_code:
                self._fail(error_code, "模拟业务错误")
                return

            machine_id = info.get("machine_id")
            machine = next((m for m in state.machines if m["machine_id"] == machine_id), None)
            if machine is None:
                self._fail("MachineNotFound", f"机器 {machine_id} 不存在")
                return
            if machine["gpu_idle_num"] < int(info.get("req_gpu_amount") or 1):
                self._fail("NoStock", "该机器空闲卡不足")
                return

            count = int(info.get("req_gpu_amount") or 1)
            machine["gpu_idle_num"] -= count
            machine["binding_instance_num"] += 1
            state.balance_milli -= int(machine["pay_price"] * count * 1000)

            index = state.next_instance_index
            state.next_instance_index += 1
            instance_uuid = f"inst-{index:04d}"
            state.instances.append(
                {
                    "uuid": instance_uuid,
                    "name": info.get("instance_name") or f"autodl-ac-{index}",
                    "status": "running",
                    "region_sign": machine["region_sign"],
                    "gpu_name": machine["gpu_type_name"],
                    "gpu_num": count,
                    "machine_id": machine_id,
                    "pay_price": machine["pay_price"],
                }
            )
            state.orders.append({"instance_uuid": instance_uuid, "price_info": price_info})
        self._ok({"uuid": instance_uuid})


class _QuietServer(ThreadingHTTPServer):
    """不打印连接异常的堆栈（模拟断连是测试的正常步骤，不是错误）。"""

    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: D102
        return


class FakeAutoDL:
    """可在测试里用 ``with`` 启停的假 AutoDL 服务。"""

    def __init__(self, token: str = "test-token", host: str = "127.0.0.1", port: int = 0):
        self.state = FakeState()
        self.state.token = token
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self.server = _QuietServer((host, port), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def token(self) -> str:
        return self.state.token

    def __enter__(self) -> "FakeAutoDL":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # 便捷方法
    def add_machine(self, **kwargs) -> dict:
        return self.state.add_machine(**kwargs)

    def add_instance(self, **kwargs) -> dict:
        with self.state.lock:
            index = self.state.next_instance_index
            self.state.next_instance_index += 1
            instance = {
                "uuid": kwargs.get("uuid", f"pre-{index:04d}"),
                "name": kwargs.get("name", "pre-existing"),
                "status": kwargs.get("status", "running"),
                "region_sign": kwargs.get("region_sign", "westDC2"),
                "gpu_name": kwargs.get("gpu_name", ["RTX 4090"]),
                "gpu_num": kwargs.get("gpu_num", 1),
                "machine_id": kwargs.get("machine_id", ""),
                "pay_price": kwargs.get("pay_price", 2.5),
            }
            self.state.instances.append(instance)
            return instance

    def wait_for(self, predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
        """轮询等待某个条件成立（测试里用来等异步副作用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False
