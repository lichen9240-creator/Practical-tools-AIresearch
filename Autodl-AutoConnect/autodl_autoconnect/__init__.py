"""Autodl-AutoConnect —— 用 AutoDL 开发者 Token 直接抢 GPU 实例。

设计目标：让 Agent（Claude Code / MCode / OpenCode 等）能拿一个 token 就把卡抢到手，
不需要开浏览器、不需要人守页面。默认只观察不扣费，明确授权后才下单。

与官方关系：本项目与 AutoDL 官方无隶属关系，仅调用其公开文档所述的开发者 API
以及控制台自身使用的接口，请自行对照 AutoDL 用户协议使用。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
