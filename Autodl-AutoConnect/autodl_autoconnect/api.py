"""AutoDL 接口封装。

所有端点都来自 AutoDL 官方文档与控制台自身前端代码：

======================================  ==========================================
端点                                     用途
======================================  ==========================================
``POST /api/v1/dev/wallet/balance``      官方文档：账户余额（开发者 Token 鉴权）
``GET  /api/v1/wallet``                  控制台：钱包余额（备用口径）
``GET  /api/v1/region/list``             地区列表
``GET  /api/v1/machine/gpu_type``        全站 GPU 型号列表
``POST /api/v1/machine/region/gpu_type`` 某地区的 GPU 型号列表
``POST /api/v1/dev/machine/region/gpu_stock`` 官方文档：地区 GPU 库存（型号→空闲数）
``POST /api/v1/machine/search``          机器报价搜索（抢卡主用）
``POST /api/v1/user/machine/list``       机器报价列表（搜索的等价端点，备用）
``GET  /api/v1/image/get_framework``     官方镜像树
``POST /api/v1/image/get``               按 uuid 取镜像详情
``POST /api/v1/order/instance/create/payg``  创建按量计费实例（真正下单）
``POST /api/v1/instance``                实例列表
``GET  /api/v1/instance/exist``          实例是否已存在（对账用）
``POST /api/v1/instance/power_on``       开机
``POST /api/v1/instance/power_off``      关机
``POST /api/v1/instance/release``        释放实例
``GET  /api/v1/bill/confirm``            账单确认（下单后可选）
======================================  ==========================================

响应统一是信封格式 ``{"code": "Success", "msg": "", "data": ...}``，
``code != "Success"`` 一律视为业务失败并抛出 :class:`APIError`。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .errors import APIError, AuthError, redact
from .httpclient import HttpClient
from .models import (
    Balance,
    Instance,
    MachineOffer,
    OrderResult,
    StockEntry,
    instances_from_payload,
    machines_from_payload,
)

__all__ = ["AutoDLClient", "ENDPOINTS", "AUTH_ERROR_CODES"]

#: AutoDL 用 HTTP 200 + 业务错误码表示鉴权问题，这些码一律翻译成鉴权失败。
AUTH_ERROR_CODES = frozenset(
    {
        "AuthorizeFailed",
        "AuthFailed",
        "LoginExpired",
        "TokenInvalid",
        "Unauthorized",
        "Forbidden",
    }
)


ENDPOINTS = {
    "balance": "/api/v1/dev/wallet/balance",
    "wallet": "/api/v1/wallet",
    "region_list": "/api/v1/region/list",
    "gpu_type": "/api/v1/machine/gpu_type",
    "region_gpu_type": "/api/v1/machine/region/gpu_type",
    "gpu_stock": "/api/v1/dev/machine/region/gpu_stock",
    "machine_search": "/api/v1/machine/search",
    "machine_list": "/api/v1/user/machine/list",
    "machine_detail": "/api/v1/machine/detail",
    "image_framework": "/api/v1/image/get_framework",
    "image_get": "/api/v1/image/get",
    "create_payg": "/api/v1/order/instance/create/payg",
    "instance_list": "/api/v1/instance",
    "instance_detail": "/api/v1/instance/detail",
    "instance_exist": "/api/v1/instance/exist",
    "instance_power_on": "/api/v1/instance/power_on",
    "instance_power_off": "/api/v1/instance/power_off",
    "instance_release": "/api/v1/instance/release",
    "bill_confirm": "/api/v1/bill/confirm",
}

#: ``machine/search`` 的默认查询体，取自控制台前端自己发出的请求。
_MACHINE_SEARCH_DEFAULTS: dict[str, Any] = {
    "charge_type": "payg",
    "region_sign": "",
    "gpu_type_name": [],
    "machine_tag_name": [],
    "gpu_idle_num": 0,
    "mount_net_disk": False,
    "instance_disk_size_order": "",
    "date_range": "",
    "date_from": "",
    "date_to": "",
    "page_index": 1,
    "page_size": 90,
    "pay_price_order": "",
    "gpu_idle_type": "",
    "default_order": True,
    "region_sign_list": [],
}


class AutoDLClient:
    """AutoDL 接口客户端。所有方法只做「发请求 + 解析信封」，不含任何业务策略。"""

    def __init__(self, http: HttpClient):
        self.http = http

    @classmethod
    def from_token(cls, token: str, **kwargs) -> "AutoDLClient":
        return cls(HttpClient(token, **kwargs))

    # ------------------------------------------------------------- 信封解析

    @staticmethod
    def _unwrap(payload: Any, *, where: str) -> Any:
        """取出信封里的 ``data``；业务错误直接抛异常。"""
        if not isinstance(payload, dict):
            raise APIError(
                f"{where} 返回了意料之外的结构：{redact(payload)}", code="BAD_ENVELOPE"
            )
        code = payload.get("code")
        if code is None:
            # 有些接口直接返回数据体，没有信封。
            return payload
        if code != "Success":
            message = redact(payload.get("msg") or payload.get("message") or "")
            if str(code) in AUTH_ERROR_CODES:
                raise AuthError(
                    f"{where} 鉴权失败（{code}）：{message}。"
                    "请检查开发者 Token 是否有效、是否已过期。",
                    code=str(code),
                    detail=payload,
                )
            raise APIError(
                f"{where} 返回错误码 {code}：{message}",
                code=str(code),
                detail=payload,
            )
        return payload.get("data")

    def _call(self, method: str, path: str, *, where: str, **kwargs) -> Any:
        response = self.http.request(method, path, **kwargs)
        return self._unwrap(response.json(), where=where)

    # ------------------------------------------------------------------ 账户

    def balance(self) -> Balance:
        """账户余额（官方开发者接口）。"""
        data = self._call("POST", ENDPOINTS["balance"], where="查询余额")
        return Balance.from_payload(data)

    def wallet(self) -> Balance:
        """控制台口径的钱包余额，作为官方接口的备用。"""
        data = self._call("GET", ENDPOINTS["wallet"], where="查询钱包余额")
        if isinstance(data, dict):
            return Balance.from_payload(data)
        return Balance(0, 0, 0)

    # ------------------------------------------------------------------ 库存

    def regions(self) -> list[dict]:
        data = self._call("GET", ENDPOINTS["region_list"], where="查询地区列表")
        return data if isinstance(data, list) else []

    def gpu_types(self) -> list[dict]:
        data = self._call("GET", ENDPOINTS["gpu_type"], where="查询 GPU 型号")
        return data if isinstance(data, list) else []

    def region_gpu_stock(self, region_sign: str, cuda_from: int | None = None,
                         cuda_to: int | None = None) -> list[StockEntry]:
        """官方文档接口：按地区查各型号的空闲卡数。

        这是「官方口径」的库存，适合做二次校验；抢卡主循环用机器报价搜索，
        因为后者带有地区、价格、可挂载数量等下单所需的字段。
        """
        body: dict[str, Any] = {"region_sign": region_sign}
        if cuda_from is not None:
            body["cuda_v_from"] = cuda_from
        if cuda_to is not None:
            body["cuda_v_to"] = cuda_to
        data = self._call("POST", ENDPOINTS["gpu_stock"], where="查询地区库存", json_body=body)
        return StockEntry.from_payload(data)

    def search_machines(
        self,
        *,
        regions: Iterable[str] | None = None,
        page_index: int = 1,
        page_size: int = 90,
        charge_type: str = "payg",
        extra: Mapping[str, Any] | None = None,
    ) -> list[MachineOffer]:
        """搜索可租机器报价。

        只把「地区」下推给服务端（地区是精确代码，语义明确），
        型号/卡数/价格的过滤全部在本地做，避免服务端精确匹配把
        ``4090`` 这类简写直接过滤成空结果。
        """
        body = dict(_MACHINE_SEARCH_DEFAULTS)
        body["page_index"] = max(1, int(page_index))
        body["page_size"] = max(1, min(200, int(page_size)))
        body["charge_type"] = charge_type
        region_list = [r for r in (regions or []) if r]
        if region_list:
            body["region_sign_list"] = region_list
        if extra:
            body.update(extra)

        data = self._call(
            "POST", ENDPOINTS["machine_search"], where="搜索机器报价", json_body=body
        )
        offers = machines_from_payload(data)
        if offers or _has_list_key(data):
            return offers
        # 结构对不上就换等价端点再试一次（``user/machine/list`` 返回同一种结构）。
        data = self._call(
            "POST", ENDPOINTS["machine_list"], where="搜索机器报价(备用端点)", json_body=body
        )
        return machines_from_payload(data)

    def machine_detail(self, machine_id: str) -> dict:
        data = self._call(
            "POST", ENDPOINTS["machine_detail"], where="查询机器详情",
            json_body={"machine_id": machine_id},
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 镜像

    def image_frameworks(self) -> Any:
        return self._call("GET", ENDPOINTS["image_framework"], where="查询官方镜像")

    def image_detail(self, image_uuid: str) -> dict:
        data = self._call(
            "POST", ENDPOINTS["image_get"], where="查询镜像详情",
            json_body={"image_uuid": image_uuid},
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 实例

    def create_payg_instance(
        self,
        *,
        machine_id: str,
        gpu_count: int = 1,
        image_uuid: str = "",
        instance_name: str = "",
        expand_data_disk_gb: int = 0,
        coupon_id_list: Iterable[int] | None = None,
        duration: int = 1,
        private_image_uuid: str = "",
    ) -> OrderResult:
        """创建按量计费实例 —— 真正扣费的那一步。

        请求体结构取自控制台前端 ``order/instance/create/payg`` 的调用代码。
        """
        gpu_count = max(1, int(gpu_count))
        expand_bytes = int(expand_data_disk_gb) * 1024 * 1024 * 1024
        body = {
            "instance_info": {
                "machine_id": machine_id,
                "charge_type": "payg",
                "req_gpu_amount": gpu_count,
                "base_image_uuid": image_uuid,
                "private_image_uuid": private_image_uuid,
                "reproduction_uuid": "",
                "cg_application_uuid": "",
                "cg_application_info": {
                    "image_name": "",
                    "current_version_id": 0,
                    "current_version": "",
                    "image_id": 0,
                },
                "instance_name": instance_name or "",
                "expand_data_disk": expand_bytes,
                "reproduction_id": 0,
            },
            "price_info": {
                "coupon_id_list": list(coupon_id_list or []),
                "machine_id": machine_id,
                "charge_type": "payg",
                "duration": duration,
                "num": gpu_count,
                "expand_data_disk": expand_bytes,
            },
        }
        data = self._call(
            "POST", ENDPOINTS["create_payg"], where="创建实例", json_body=body, max_retries=0
        )
        instance_uuid = data if isinstance(data, str) else (data or {}).get("uuid", "")
        return OrderResult(
            instance_uuid=str(instance_uuid or ""),
            machine_id=machine_id,
            gpu_count=gpu_count,
            price_hourly=None,
            detail={"response": data if isinstance(data, dict) else {"uuid": instance_uuid}},
        )

    def list_instances(self, page_index: int = 1, page_size: int = 100) -> list[Instance]:
        body = {"page_index": max(1, int(page_index)), "page_size": max(1, min(200, int(page_size)))}
        data = self._call(
            "POST", ENDPOINTS["instance_list"], where="查询实例列表", json_body=body
        )
        return instances_from_payload(data)

    def instance_exists(self, instance_uuid: str) -> bool:
        try:
            data = self._call(
                "GET", ENDPOINTS["instance_exist"], where="核对实例是否存在",
                query={"instance_uuid": instance_uuid},
            )
        except APIError:
            return False
        if isinstance(data, bool):
            return data
        if isinstance(data, dict):
            return bool(data.get("uuid") or data.get("exist") or data.get("data"))
        return bool(data)

    def instance_detail(self, instance_uuid: str) -> dict:
        data = self._call(
            "GET", ENDPOINTS["instance_detail"], where="查询实例详情",
            query={"instance_uuid": instance_uuid},
        )
        return data if isinstance(data, dict) else {}

    def power_on(self, instance_uuid: str) -> Any:
        return self._call(
            "POST", ENDPOINTS["instance_power_on"], where="开机",
            json_body={"instance_uuid": instance_uuid}, max_retries=0,
        )

    def power_off(self, instance_uuid: str) -> Any:
        return self._call(
            "POST", ENDPOINTS["instance_power_off"], where="关机",
            json_body={"instance_uuid": instance_uuid}, max_retries=0,
        )

    def release_instance(self, instance_uuid: str) -> Any:
        return self._call(
            "POST", ENDPOINTS["instance_release"], where="释放实例",
            json_body={"instance_uuid": instance_uuid}, max_retries=0,
        )

    def confirm_bill(self, instance_uuid: str = "") -> Any:
        """账单确认（可选步骤，失败不影响主流程）。"""
        query = {"instance_uuid": instance_uuid} if instance_uuid else None
        return self._call("GET", ENDPOINTS["bill_confirm"], where="确认账单", query=query)


def _has_list_key(payload: Any) -> bool:
    """判断返回体里是否真的带了 ``list`` 字段（用来区分「结构不对」和「确实没货」）。"""
    if isinstance(payload, dict):
        if isinstance(payload.get("list"), list):
            return True
        inner = payload.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("list"), list):
            return True
    return False
