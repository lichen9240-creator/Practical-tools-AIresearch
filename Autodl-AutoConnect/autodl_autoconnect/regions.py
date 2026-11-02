"""AutoDL 地区代码表。

`region_sign` 是 AutoDL 接口里用的地区标识（如 ``westDC2``），
控制台页面上显示的是中文名（如 ``西北B区``）。两张表都来自 AutoDL 官方文档，
中文名仅用于展示与人类可读配置，接口调用一律用英文代码。

来源：AutoDL 官方文档《弹性部署API》的附录，以及控制台接口
``api/v1/region/list`` 的返回。两者若有出入，以接口返回为准。
"""

from __future__ import annotations

__all__ = ["REGION_CODES", "REGION_NAMES", "normalize_region", "region_display_name"]

#: 官方文档附录里出现的地区代码（已核对，顺序按文档出现顺序）。
REGION_CODES: tuple[str, ...] = (
    "beijingDC1",
    "beijingDC2",
    "beijingDC3",
    "beijingDC4",
    "chongqingDC1",
    "foshanDC1",
    "neimengDC1",
    "neimengDC3",
    "westDC2",
    "westDC3",
    "yangzhouDC1",
)

#: 代码 -> 控制台显示名。显示名会随官方调整而变化，仅作提示用。
REGION_NAMES: dict[str, str] = {
    "beijingDC1": "北京A区",
    "beijingDC2": "北京B区",
    "beijingDC3": "北京C区",
    "beijingDC4": "北京D区",
    "chongqingDC1": "重庆A区",
    "foshanDC1": "佛山A区",
    "neimengDC1": "内蒙A区",
    "neimengDC3": "内蒙C区",
    "westDC2": "西北B区",
    "westDC3": "西北C区",
    "yangzhouDC1": "扬州A区",
}

_ALIASES = {
    "beijing": "beijingDC1",
    "bj": "beijingDC1",
    "chongqing": "chongqingDC1",
    "cq": "chongqingDC1",
    "foshan": "foshanDC1",
    "fs": "foshanDC1",
    "neimeng": "neimengDC1",
    "nm": "neimengDC1",
    "west": "westDC2",
    "nw": "westDC2",
    "yangzhou": "yangzhouDC1",
    "yz": "yangzhouDC1",
}


def normalize_region(value: str) -> str:
    """把用户写的地区（中文名 / 别名 / 大小写混写）归一化成官方 ``region_sign``。

    无法识别时原样返回（去掉首尾空白），由调用方决定是否报错——
    这样将来官方新增地区时本工具不需要改代码也能用。
    """
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    for code, name in REGION_NAMES.items():
        if text == name or text.lower() == code.lower():
            return code
    lowered = text.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    # "北京B区" / "北京 B 区" 这类带空格的写法
    compact = text.replace(" ", "")
    for code, name in REGION_NAMES.items():
        if compact == name or compact.lower() == code.lower():
            return code
    return text


def region_display_name(region_sign: str) -> str:
    """返回地区的可读名，未知地区回退为代码本身。"""
    return REGION_NAMES.get(region_sign, region_sign)
