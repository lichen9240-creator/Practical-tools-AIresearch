"""配置：一份 JSON 文件 + 环境变量覆盖。

为什么用 JSON 而不是 YAML：不想给用户装 pyyaml。JSON 的子集足够表达配置，
而且 Agent 也能直接生成/修改它。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .regions import normalize_region

__all__ = ["TaskSpec", "NotifyConfig", "load_config", "default_config_path", "save_config"]

ENV_TOKEN = "AUTODL_TOKEN"
ENV_CONFIG = "AUTODL_AUTOCONNECT_CONFIG"
ENV_HOME = "AUTODL_AUTOCONNECT_HOME"


def state_dir() -> Path:
    """本工具的状态目录（Token、意图日志都放这里）。

    默认 ``~/.autodl-autoconnect``；可用环境变量 ``AUTODL_AUTOCONNECT_HOME`` 改到别处，
    方便在服务器上给每个账号单独开一个目录。
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".autodl-autoconnect"


def default_config_path() -> Path:
    """默认配置文件位置。"""
    return state_dir() / "config.json"


@dataclass
class NotifyConfig:
    """通知渠道配置。"""

    webhook_url: str = ""
    """通用 Webhook，POST 一个 JSON。"""

    webhook_token: str = ""
    """Webhook 的 Bearer Token（可选）。"""

    serverchan_key: str = ""
    """Server酱 key，把消息推到微信。"""

    autodl_wechat_token: str = ""
    """AutoDL 微信消息接口的 token，消息会推到你绑定微信的 AutoDL 账号。"""

    desktop: bool = True
    """在本机终端打印醒目提示（Windows 下额外尝试系统通知）。"""

    def enabled(self) -> bool:
        return bool(self.webhook_url or self.serverchan_key or self.autodl_wechat_token or self.desktop)


@dataclass
class TaskSpec:
    """一次抢卡任务的全部条件。"""

    # ---- 抢什么 ----
    gpu_models: list[str] = field(default_factory=list)
    """要等的 GPU 型号，例如 ``["RTX 4090", "RTX 4090D"]``。留空表示不限型号。"""

    gpu_model_regex: str = ""
    """高级用法：用正则匹配型号，例如 ``"RTX 40(90|90D)$"``。设置后忽略 ``gpu_models``。"""

    gpu_count: int = 1
    """需要的卡数。"""

    regions: list[str] = field(default_factory=list)
    """要盯的地区（官方代码或中文名均可），留空表示全国。"""

    any_region: bool = False
    """为 True 时忽略地区限制。"""

    # ---- 用什么镜像 ----
    image_uuid: str = ""
    """官方镜像 uuid。留空会尝试用默认镜像（多数情况下 AutoDL 会拒绝，需要显式指定）。"""

    instance_name: str = ""
    """实例名称，留空由 AutoDL 自动命名。"""

    expand_data_disk_gb: int = 0
    """扩展数据盘（GB）。"""

    # ---- 钱的护栏 ----
    max_price_hourly: float | None = None
    """单卡每小时价格上限（元）。超过就不抢。"""

    max_total_cost: float | None = None
    """本次预计总花费上限（元）：``单卡价 × 卡数 × 预计时长``。"""

    expected_hours: float = 1.0
    """预计使用时长（小时），用于算 ``max_total_cost``。"""

    balance_reserve: float = 0.0
    """余额保留额（元），预检时余额必须高于「预计花费 + 保留额」。"""

    include_vouchers: bool = True
    """预检时是否把代金券算进可用余额。"""

    require_known_price: bool = True
    """拿不到价格时是否放弃下单（True 更安全，False 会让没有报价的机器通过）。"""

    # ---- 行为 ----
    paid_consent: bool = False
    """**默认 False**：只发现库存就通知，不真的下单。改成 True 才会创建实例。"""

    poll_seconds: float = 3.0
    """轮询间隔（秒）。"""

    timeout_minutes: float = 0.0
    """最长监测时长（分钟），0 表示不限。"""

    cooldown_seconds: float = 20.0
    """下单失败后的冷却时间，避免疯狂重试。"""

    max_attempts: int = 0
    """最多尝试下单次数，0 表示不限。"""

    notify: NotifyConfig = field(default_factory=NotifyConfig)

    # ------------------------------------------------------------------ 校验

    def validate(self) -> "TaskSpec":
        if self.gpu_count < 1:
            raise ConfigError("gpu_count 至少为 1")
        if self.expected_hours <= 0:
            raise ConfigError("expected_hours 必须大于 0")
        if self.poll_seconds <= 0:
            raise ConfigError("poll_seconds 必须大于 0")
        if self.max_total_cost is not None and self.max_total_cost <= 0:
            raise ConfigError("max_total_cost 必须大于 0")
        if self.max_price_hourly is not None and self.max_price_hourly <= 0:
            raise ConfigError("max_price_hourly 必须大于 0")
        if self.balance_reserve < 0:
            raise ConfigError("balance_reserve 不能为负")
        if self.gpu_model_regex:
            import re

            try:
                re.compile(self.gpu_model_regex)
            except re.error as exc:
                raise ConfigError(f"gpu_model_regex 不是合法正则：{exc}") from exc
        if not self.gpu_models and not self.gpu_model_regex:
            raise ConfigError(
                "必须指定 gpu_models（例如 [\"RTX 4090\"]）或 gpu_model_regex，"
                "否则会在任意显卡上盲目下单"
            )
        return self

    def normalized_regions(self) -> list[str]:
        """把地区列表归一化成官方 region_sign 列表。"""
        if self.any_region:
            return []
        out: list[str] = []
        for region in self.regions:
            code = normalize_region(region)
            if code and code not in out:
                out.append(code)
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        if not isinstance(data, dict):
            raise ConfigError("配置根节点必须是对象")
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        unknown = sorted(set(data) - known)
        notify_data = data.get("notify") or {}
        if not isinstance(notify_data, dict):
            raise ConfigError("notify 必须是对象")
        notify_known = {f.name for f in fields(NotifyConfig)}
        payload["notify"] = NotifyConfig(
            **{k: v for k, v in notify_data.items() if k in notify_known}
        )
        spec = cls(**payload)
        if spec.gpu_count is None:
            spec.gpu_count = 1
        spec.gpu_models = [str(m) for m in (spec.gpu_models or [])]
        spec.regions = [str(r) for r in (spec.regions or [])]
        return spec


def load_config(path: str | os.PathLike | None = None, *, required: bool = False) -> TaskSpec:
    """读取配置文件。找不到时返回一份默认配置（``required=True`` 时报错）。"""
    target = Path(path) if path else Path(os.environ.get(ENV_CONFIG) or default_config_path())
    if not target.exists():
        if required:
            raise ConfigError(f"配置文件不存在：{target}")
        return TaskSpec()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc
    return TaskSpec.from_dict(raw)


def save_config(spec: TaskSpec, path: str | os.PathLike | None = None) -> Path:
    """把配置写回磁盘（用于 ``config init`` / ``config set``）。"""
    target = Path(path) if path else Path(os.environ.get(ENV_CONFIG) or default_config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
