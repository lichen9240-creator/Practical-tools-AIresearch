"""数据模型。

所有从 AutoDL 返回的字段都在这里做一次「防御式提取」：
AutoDL 的接口返回里同一含义可能有多种字段名（历史原因），
所以每个模型都接受多个候选键，取第一个拿到合法值的。
这样官方改字段名时不会直接炸掉，而是退化成 None 再由上层判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["Balance", "MachineOffer", "StockEntry", "Instance", "OrderResult"]


def _first(data: Any, *keys: str, default: Any = None) -> Any:
    """按候选键顺序取第一个非 None 值。"""
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


@dataclass(frozen=True)
class Balance:
    """账户余额。金额统一换算成「元」保存，保留千分元整数以避免浮点误差。"""

    assets_milli: int
    """钱包余额，单位千分之一元（AutoDL 原样返回）。"""

    voucher_milli: int = 0
    """代金券余额，单位千分之一元。"""

    accumulate_milli: int = 0
    """累计消费，单位千分之一元。"""

    @property
    def assets(self) -> float:
        return self.assets_milli / 1000.0

    @property
    def vouchers(self) -> float:
        return self.voucher_milli / 1000.0

    @property
    def accumulate(self) -> float:
        return self.accumulate_milli / 1000.0

    @classmethod
    def from_payload(cls, data: Any) -> "Balance":
        data = data if isinstance(data, dict) else {}
        return cls(
            assets_milli=_as_int(_first(data, "assets", "balance"), 0) or 0,
            voucher_milli=_as_int(_first(data, "voucher_balance", "vouchers"), 0) or 0,
            accumulate_milli=_as_int(_first(data, "accumulate"), 0) or 0,
        )

    def to_dict(self) -> dict:
        return {
            "assets": self.assets,
            "assets_milli": self.assets_milli,
            "vouchers": self.vouchers,
            "vouchers_milli": self.voucher_milli,
            "accumulate": self.accumulate,
            "accumulate_milli": self.accumulate_milli,
        }


@dataclass(frozen=True)
class MachineOffer:
    """一条可租的机器报价（来自 ``api/v1/machine/search`` / ``api/v1/user/machine/list``）。"""

    machine_id: str
    region_sign: str
    gpu_names: tuple[str, ...]
    idle_gpu_num: int = 0
    total_gpu_num: int = 0
    max_instance_num: int = 0
    binding_instance_num: int = 0
    on_line: int | None = None
    price_hourly: float | None = None
    """单卡每小时价格（元）。拿不到时为 None，交给上层做「无法核实就停下」的处理。"""

    disk_price_daily: float | None = None
    gpu_type_name_hint: str = ""
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def offline(self) -> bool:
        """控制台把 on_line ∈ {3,4,5,6} 视为不可用。"""
        return self.on_line in (3, 4, 5, 6)

    @property
    def has_free_slot(self) -> bool:
        """除了空闲卡数，还要看是否还能再挂实例。"""
        if self.max_instance_num and self.binding_instance_num >= self.max_instance_num:
            return False
        return True

    @classmethod
    def from_payload(cls, data: Any) -> "MachineOffer | None":
        if not isinstance(data, dict):
            return None
        machine_id = _first(data, "machine_id", "id", "uuid")
        if not machine_id:
            return None
        gpu_names = tuple(
            _as_str_list(_first(data, "gpu_type_name", "gpu_name", "gpu_type", "gpu_names"))
        )
        price = _as_float(
            _first(data, "pay_price", "price", "price_per_hour", "hourly_price", "base_price")
        )
        return cls(
            machine_id=str(machine_id),
            region_sign=str(_first(data, "region_sign", "region", "region_code", default="")),
            gpu_names=gpu_names,
            idle_gpu_num=_as_int(_first(data, "gpu_idle_num", "idle_gpu_num", "idle_num"), 0) or 0,
            total_gpu_num=_as_int(_first(data, "gpu_total_num", "total_gpu_num"), 0) or 0,
            max_instance_num=_as_int(_first(data, "max_instance_num"), 0) or 0,
            binding_instance_num=_as_int(_first(data, "binding_instance_num"), 0) or 0,
            on_line=_as_int(_first(data, "on_line")),
            price_hourly=price,
            disk_price_daily=_as_float(_first(data, "disk_price", "data_disk_price")),
            gpu_type_name_hint=str(_first(data, "gpu_idle_type", "gpu_type_name_str", default="")),
            raw=data,
        )

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "region_sign": self.region_sign,
            "gpu_names": list(self.gpu_names),
            "idle_gpu_num": self.idle_gpu_num,
            "total_gpu_num": self.total_gpu_num,
            "max_instance_num": self.max_instance_num,
            "binding_instance_num": self.binding_instance_num,
            "on_line": self.on_line,
            "offline": self.offline,
            "price_hourly": self.price_hourly,
            "disk_price_daily": self.disk_price_daily,
        }


@dataclass(frozen=True)
class StockEntry:
    """按地区统计的单个 GPU 型号库存（官方 ``gpu_stock`` 接口）。"""

    gpu_name: str
    idle_gpu_num: int
    total_gpu_num: int

    @classmethod
    def from_payload(cls, data: Any) -> list["StockEntry"]:
        """``data`` 形如 ``[{"RTX 4090": {"idle_gpu_num": 215, "total_gpu_num": 2285}}, ...]``。"""
        entries: list[StockEntry] = []
        if not isinstance(data, list):
            return entries
        for row in data:
            if not isinstance(row, dict):
                continue
            for gpu_name, stats in row.items():
                if not isinstance(stats, dict):
                    continue
                entries.append(
                    cls(
                        gpu_name=str(gpu_name),
                        idle_gpu_num=_as_int(_first(stats, "idle_gpu_num"), 0) or 0,
                        total_gpu_num=_as_int(_first(stats, "total_gpu_num"), 0) or 0,
                    )
                )
        return entries

    def to_dict(self) -> dict:
        return {
            "gpu_name": self.gpu_name,
            "idle_gpu_num": self.idle_gpu_num,
            "total_gpu_num": self.total_gpu_num,
        }


@dataclass(frozen=True)
class Instance:
    """用户的一台容器实例。"""

    uuid: str
    name: str = ""
    status: str = ""
    region_sign: str = ""
    gpu_names: tuple[str, ...] = ()
    gpu_count: int = 0
    machine_id: str = ""
    price_hourly: float | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_payload(cls, data: Any) -> "Instance | None":
        if not isinstance(data, dict):
            return None
        uuid = _first(data, "uuid", "instance_uuid", "id")
        if not uuid:
            return None
        gpu_names = tuple(
            _as_str_list(_first(data, "gpu_name", "gpu_names", "gpu_type_name"))
        )
        return cls(
            uuid=str(uuid),
            name=str(_first(data, "name", "instance_name", default="")),
            status=str(_first(data, "status", "status_name", "power_status", default="")),
            region_sign=str(_first(data, "region_sign", "region", default="")),
            gpu_names=gpu_names,
            gpu_count=_as_int(_first(data, "gpu_num", "req_gpu_amount", "gpu_count"), 0) or 0,
            machine_id=str(_first(data, "machine_id", default="")),
            price_hourly=_as_float(_first(data, "pay_price", "price", "hourly_price")),
            raw=data,
        )

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "status": self.status,
            "region_sign": self.region_sign,
            "gpu_names": list(self.gpu_names),
            "gpu_count": self.gpu_count,
            "machine_id": self.machine_id,
            "price_hourly": self.price_hourly,
        }


@dataclass
class OrderResult:
    """一次下单的结果。"""

    instance_uuid: str
    machine_id: str
    gpu_count: int
    price_hourly: float | None
    confirmed: bool = False
    """True 表示已经通过实例列表核对确认创建成功。"""

    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "instance_uuid": self.instance_uuid,
            "machine_id": self.machine_id,
            "gpu_count": self.gpu_count,
            "price_hourly": self.price_hourly,
            "confirmed": self.confirmed,
            "detail": self.detail,
        }


def machines_from_payload(payload: Any) -> list[MachineOffer]:
    """从 ``machine/search`` / ``user/machine/list`` 的返回里提取机器列表。

    这两个接口的返回有两种形态：
    ``{"list": [...], "result_total": N}`` 或直接是一个数组。
    """
    rows: Iterable[Any]
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("list"), list):
            rows = inner["list"]
        elif isinstance(inner, list):
            rows = inner
        elif isinstance(payload.get("list"), list):
            rows = payload["list"]
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    offers: list[MachineOffer] = []
    for row in rows:
        offer = MachineOffer.from_payload(row)
        if offer is not None:
            offers.append(offer)
    return offers


def instances_from_payload(payload: Any) -> list[Instance]:
    """从实例列表返回里提取实例，兼容包裹形态。"""
    rows: Iterable[Any]
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            for key in ("list", "instance_list", "instances"):
                if isinstance(inner.get(key), list):
                    rows = inner[key]
                    break
            else:
                rows = []
        elif isinstance(inner, list):
            rows = inner
        elif isinstance(payload.get("list"), list):
            rows = payload["list"]
        else:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    result: list[Instance] = []
    for row in rows:
        instance = Instance.from_payload(row)
        if instance is not None:
            result.append(instance)
    return result
