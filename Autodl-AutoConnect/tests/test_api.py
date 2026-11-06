"""API 客户端的测试：对着本地假 AutoDL 服务跑真实的 HTTP 往返。"""

from __future__ import annotations

import unittest

from autodl_autoconnect.api import AutoDLClient
from autodl_autoconnect.errors import APIError, AuthError
from autodl_autoconnect.httpclient import HttpClient

from .fake_autodl import FakeAutoDL


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeAutoDL()
        self.fake.__enter__()
        self.client = AutoDLClient(
            HttpClient(self.fake.token, base_url=self.fake.base_url, timeout=5, max_retries=1)
        )

    def tearDown(self) -> None:
        self.client.http.close()
        self.fake.__exit__(None, None, None)

    # ------------------------------------------------------------- 鉴权

    def test_wrong_token_raises_auth_error(self):
        bad = AutoDLClient(
            HttpClient("wrong", base_url=self.fake.base_url, timeout=5, max_retries=0)
        )
        try:
            with self.assertRaises(AuthError):
                bad.balance()
        finally:
            bad.http.close()

    def test_business_error_code_raises_api_error(self):
        self.fake.state.token = "changed-after-client-created"
        with self.assertRaises(AuthError):
            self.client.balance()

    # ------------------------------------------------------------- 余额

    def test_balance(self):
        self.fake.state.balance_milli = 12345
        self.fake.state.voucher_milli = 500
        balance = self.client.balance()
        self.assertEqual(balance.assets_milli, 12345)
        self.assertAlmostEqual(balance.assets, 12.345, places=3)
        self.assertAlmostEqual(balance.vouchers, 0.5, places=3)

    # ------------------------------------------------------------- 库存

    def test_search_machines(self):
        self.fake.add_machine(machine_id="m-1", gpu_type_name=["RTX 4090"], gpu_idle_num=4)
        self.fake.add_machine(machine_id="m-2", region_sign="beijingDC2", gpu_type_name=["A100"])
        offers = self.client.search_machines()
        self.assertEqual(len(offers), 2)
        self.assertEqual({o.machine_id for o in offers}, {"m-1", "m-2"})
        first = next(o for o in offers if o.machine_id == "m-1")
        self.assertEqual(first.gpu_names, ("RTX 4090",))
        self.assertEqual(first.idle_gpu_num, 4)
        self.assertAlmostEqual(first.price_hourly, 2.5, places=3)

    def test_search_machines_filters_by_region(self):
        self.fake.add_machine(machine_id="m-1", region_sign="westDC2")
        self.fake.add_machine(machine_id="m-2", region_sign="beijingDC2")
        offers = self.client.search_machines(regions=["westDC2"])
        self.assertEqual([o.machine_id for o in offers], ["m-1"])
        self.assertEqual(self.fake.state.search_calls[-1]["region_sign_list"], ["westDC2"])

    def test_search_sends_expected_payload_shape(self):
        """请求体字段必须和 AutoDL 控制台自己发的一致，否则服务端会当成非法查询。"""
        self.fake.add_machine()
        self.client.search_machines(regions=["westDC2"], page_size=42)
        body = self.fake.state.search_calls[-1]
        for key in (
            "charge_type",
            "region_sign",
            "gpu_type_name",
            "machine_tag_name",
            "gpu_idle_num",
            "mount_net_disk",
            "page_index",
            "page_size",
            "default_order",
            "region_sign_list",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["charge_type"], "payg")
        self.assertEqual(body["page_size"], 42)
        self.assertEqual(body["page_index"], 1)

    def test_region_gpu_stock(self):
        self.fake.add_machine(gpu_type_name=["RTX 4090"], gpu_idle_num=7, gpu_total_num=9)
        entries = self.client.region_gpu_stock("westDC2")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].gpu_name, "RTX 4090")
        self.assertEqual(entries[0].idle_gpu_num, 7)
        self.assertEqual(entries[0].total_gpu_num, 9)

    # ------------------------------------------------------------- 下单

    def test_create_payg_instance(self):
        self.fake.add_machine(machine_id="m-1", gpu_idle_num=4, pay_price=2.5)
        order = self.client.create_payg_instance(
            machine_id="m-1", gpu_count=2, image_uuid="img-1", instance_name="my-gpu"
        )
        self.assertTrue(order.instance_uuid)
        sent = self.fake.state.create_calls[-1]
        self.assertEqual(sent["instance_info"]["machine_id"], "m-1")
        self.assertEqual(sent["instance_info"]["charge_type"], "payg")
        self.assertEqual(sent["instance_info"]["req_gpu_amount"], 2)
        self.assertEqual(sent["instance_info"]["base_image_uuid"], "img-1")
        self.assertEqual(sent["price_info"]["num"], 2)
        self.assertEqual(sent["price_info"]["machine_id"], "m-1")

    def test_create_payg_deducts_stock_and_balance(self):
        self.fake.add_machine(machine_id="m-1", gpu_idle_num=4, pay_price=2.5)
        self.client.create_payg_instance(machine_id="m-1", gpu_count=1)
        self.assertEqual(self.fake.state.machines[0]["gpu_idle_num"], 3)
        self.assertEqual(self.fake.state.balance_milli, 100_000 - 2500)

    def test_create_rejects_unknown_machine(self):
        with self.assertRaises(APIError):
            self.client.create_payg_instance(machine_id="nope", gpu_count=1)

    # ------------------------------------------------------------- 实例

    def test_list_instances(self):
        self.fake.add_instance(uuid="i-1", machine_id="m-1")
        instances = self.client.list_instances()
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].uuid, "i-1")
        self.assertEqual(instances[0].machine_id, "m-1")

    def test_power_on(self):
        self.fake.add_instance(uuid="i-1")
        self.client.power_on("i-1")
        self.assertEqual(self.fake.state.power_on_calls, ["i-1"])

    def test_instance_exists(self):
        self.fake.add_instance(uuid="i-1")
        self.assertTrue(self.client.instance_exists("i-1"))
        self.assertFalse(self.client.instance_exists("i-999"))


class KeepAliveTest(unittest.TestCase):
    """长连接是抢卡速度的关键：5 个请求应该只握手 1 次。"""

    def setUp(self) -> None:
        self.fake = FakeAutoDL()
        self.fake.__enter__()

    def tearDown(self) -> None:
        self.fake.__exit__(None, None, None)

    def _client(self, **kwargs) -> AutoDLClient:
        return AutoDLClient(
            HttpClient(self.fake.token, base_url=self.fake.base_url, timeout=5, **kwargs)
        )

    def test_reuses_one_connection_for_many_requests(self):
        client = self._client()
        try:
            for _ in range(5):
                client.balance()
        finally:
            client.http.close()
        self.assertEqual(self.fake.state.connection_count, 1)

    def test_disabled_keepalive_opens_new_connections(self):
        client = self._client(keepalive=False)
        try:
            for _ in range(3):
                client.balance()
        finally:
            client.http.close()
        self.assertGreaterEqual(self.fake.state.connection_count, 3)


if __name__ == "__main__":
    unittest.main()
