"""命令行入口。

设计目标是「Agent 友好」：
- 每个命令都支持 ``--json``，输出机器可解析的结果；
- 退出码有明确语义，Agent 可以据此分支（见 :data:`EXIT_CODES`）；
- 抢卡这种一次性动作提供一个原子命令 ``grab``，Agent 可以在自己的循环里调它，
  不需要自己解析日志或实现重试。

退出码：

===== ========================== =====================================
码    含义                        典型场景
===== ========================== =====================================
0     成功                        抢到 / 查询正常
2     配置错误                    配置文件缺字段、token 缺失
3     鉴权失败                    token 无效或过期
4     没匹配到                    ``grab`` 没找到符合条件且有货的机器
5     结果不明                    下单超时，已停止下单，需要人工核对
6     网络错误                    连不上 AutoDL
7     其它业务错误                接口返回错误码
===== ========================== =====================================
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import __version__
from .api import AutoDLClient
from .config import ENV_CONFIG, TaskSpec, default_config_path, load_config, save_config
from .errors import APIError, AuthError, AutoDLError, ConfigError, NetworkError, redact
from .httpclient import HttpClient
from .journal import Journal
from .secret import describe_storage, load_token, save_token, token_path
from .watcher import Watcher, WatchOutcome

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NO_MATCH = 4
EXIT_AMBIGUOUS = 5
EXIT_NETWORK = 6
EXIT_BUSINESS = 7

EXIT_CODES = {
    EXIT_OK: "成功",
    EXIT_CONFIG: "配置错误",
    EXIT_AUTH: "鉴权失败",
    EXIT_NO_MATCH: "未匹配到符合条件的机器",
    EXIT_AMBIGUOUS: "下单结果不明，需要人工核对",
    EXIT_NETWORK: "网络错误",
    EXIT_BUSINESS: "接口业务错误",
}

BANNER = """\
============================================================
  AutoDL 抢卡助手 v%s   |   开发者 Token 直连，不开浏览器
  查库存 -> 匹配 -> 下单 -> 开机 -> 核对 -> 通知
============================================================
"""


# ---------------------------------------------------------------------- 工具


def _out(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)


def _make_client(args: argparse.Namespace, token: str) -> AutoDLClient:
    http = HttpClient(
        token,
        base_url=args.base_url,
        timeout=args.http_timeout,
        max_retries=args.http_retries,
    )
    return AutoDLClient(http)


@contextmanager
def _open_client(args: argparse.Namespace, token: str | None = None) -> Iterator[AutoDLClient]:
    """建一个客户端，用完把长连接关掉（避免测试/脚本里一堆未关闭的 socket）。"""
    client = _make_client(args, token if token is not None else _need_token(args))
    try:
        yield client
    finally:
        client.http.close()


def _resolve_spec(args: argparse.Namespace) -> TaskSpec:
    """命令行参数覆盖配置文件里的同名字段。

    不同子命令支持的参数不一样，所以统一用 ``getattr`` 取，
    没定义的参数一律当作「没传」。
    """
    spec = load_config(args.config)
    overrides = {
        "gpu_models": "gpu_models",
        "gpu_model_regex": "gpu_model_regex",
        "gpu_count": "gpu_count",
        "regions": "regions",
        "image_uuid": "image_uuid",
        "max_price_hourly": "max_price_hourly",
        "max_total_cost": "max_total_cost",
        "expected_hours": "expected_hours",
        "balance_reserve": "balance_reserve",
        "poll_seconds": "poll_seconds",
        "timeout_minutes": "timeout_minutes",
    }
    for attr, field_name in overrides.items():
        value = getattr(args, attr, None)
        if value is not None:
            setattr(spec, field_name, value)
    if getattr(args, "yes", False):
        spec.paid_consent = True
    if getattr(args, "no_consent", False):
        spec.paid_consent = False
    if getattr(args, "instance_name", None) is not None:
        spec.instance_name = args.instance_name
    if getattr(args, "expand_data_disk_gb", None) is not None:
        spec.expand_data_disk_gb = args.expand_data_disk_gb
    if getattr(args, "webhook_url", None):
        spec.notify.webhook_url = args.webhook_url
    if getattr(args, "serverchan_key", None):
        spec.notify.serverchan_key = args.serverchan_key
    if getattr(args, "autodl_wechat_token", None):
        spec.notify.autodl_wechat_token = args.autodl_wechat_token
    if getattr(args, "no_desktop", False):
        spec.notify.desktop = False
    return spec


def _need_token(args: argparse.Namespace) -> str:
    token = (args.token or "").strip() or load_token()
    if not token:
        raise ConfigError(
            "没有找到 AutoDL 开发者 Token。请先运行：\n"
            "  autodl-ac login --token <你的Token>\n"
            f"或设置环境变量 AUTODL_TOKEN，或保存到 {token_path()}"
        )
    return token


# ---------------------------------------------------------------------- 命令


def cmd_login(args: argparse.Namespace) -> int:
    token = (args.token or "").strip()
    if not token:
        if not sys.stdin.isatty():
            raise ConfigError("非交互环境请用 --token 传入开发者 Token")
        print("请粘贴 AutoDL 开发者 Token（控制台 → 设置 → 开发者 Token）：", end=" ", flush=True)
        token = sys.stdin.readline().strip()
    if not token:
        raise ConfigError("Token 不能为空")

    with _open_client(args, token) as client:
        balance = client.balance()
    save_token(token, args.token_file)
    _out(
        {
            "ok": True,
            "token_saved_to": str(args.token_file or token_path()),
            "balance": balance.to_dict(),
        },
        args.json,
    )
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"ok": True, "version": __version__, "checks": []}
    checks = report["checks"]

    checks.append({"name": "配置文件", "ok": True, "detail": str(args.config or default_config_path())})

    token = ""
    try:
        token = _need_token(args)
        detail = "命令行 --token" if (args.token or "").strip() else describe_storage()
        checks.append({"name": "Token", "ok": True, "detail": detail})
    except ConfigError as exc:
        checks.append({"name": "Token", "ok": False, "detail": str(exc)})
        report["ok"] = False

    if token:
        with _open_client(args, token) as client:
            try:
                balance = client.balance()
                checks.append(
                    {"name": "余额接口", "ok": True, "detail": f"余额 ¥{balance.assets:.3f}"}
                )
            except AutoDLError as exc:
                checks.append({"name": "余额接口", "ok": False, "detail": str(exc)})
                report["ok"] = False
            try:
                offers = client.search_machines(page_size=5)
                checks.append(
                    {"name": "库存接口", "ok": True, "detail": f"取到 {len(offers)} 条报价"}
                )
            except AutoDLError as exc:
                checks.append({"name": "库存接口", "ok": False, "detail": str(exc)})
                report["ok"] = False

    journal = Journal()
    report["journal"] = journal.summary()
    pending = journal.pending_intents()
    if pending:
        checks.append(
            {
                "name": "未收尾的下单记录",
                "ok": False,
                "detail": f"{len(pending)} 条，需要人工核对后再下单",
            }
        )
        report["ok"] = False
    else:
        checks.append({"name": "未收尾的下单记录", "ok": True, "detail": "无"})

    _out(report, args.json)
    return EXIT_OK if report["ok"] else EXIT_ERROR


def cmd_balance(args: argparse.Namespace) -> int:
    with _open_client(args) as client:
        balance = client.balance()
    if args.json:
        _out({"ok": True, **balance.to_dict()}, True)
    else:
        _out(balance.to_dict(), False)
    return EXIT_OK


def cmd_stock(args: argparse.Namespace) -> int:
    spec = _resolve_spec(args)
    with _open_client(args) as client:
        offers = client.search_machines(
            regions=spec.normalized_regions(), page_size=args.page_size
        )
    if args.json:
        payload = {
            "ok": True,
            "checked_at": int(time.time()),
            "regions": spec.normalized_regions(),
            "offers": [offer.to_dict() for offer in offers],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not offers:
        print("没有取到任何报价。")
        return EXIT_OK
    print(f"{'地区':<14}{'型号':<26}{'空闲':>5}  {'单价/时':>9}  机器ID")
    print("-" * 78)
    for offer in offers:
        name = "/".join(offer.gpu_names) or "-"
        price = f"{offer.price_hourly}" if offer.price_hourly is not None else "-"
        print(f"{offer.region_sign:<14}{name:<26}{offer.idle_gpu_num:>5}  {price:>9}  {offer.machine_id}")
    return EXIT_OK


def cmd_instances(args: argparse.Namespace) -> int:
    with _open_client(args) as client:
        instances = client.list_instances()
    if args.json:
        _out(
            {"ok": True, "instances": [i.to_dict() for i in instances]},
            args.json,
        )
        return EXIT_OK
    if not instances:
        print("当前没有实例。")
        return EXIT_OK
    print(f"{'UUID':<40}{'地区':<14}{'型号':<24}{'卡数':>4}  状态")
    print("-" * 96)
    for item in instances:
        name = "/".join(item.gpu_names) or "-"
        print(f"{item.uuid:<40}{item.region_sign:<14}{name:<24}{item.gpu_count:>4}  {item.status}")
    return EXIT_OK


def _outcome_exit(outcome: WatchOutcome) -> int:
    """把一轮结果翻译成退出码（Agent 靠这个分支）。"""
    if outcome.status in ("created", "confirmed", "observed"):
        return EXIT_OK
    if outcome.status in ("no_match", "timeout"):
        return EXIT_NO_MATCH
    if outcome.status == "ambiguous":
        return EXIT_AMBIGUOUS
    if outcome.status == "error":
        if outcome.error_code == AuthError.code:
            return EXIT_AUTH
        if outcome.error_code == NetworkError.code:
            return EXIT_NETWORK
        return EXIT_BUSINESS
    return EXIT_OK


def _run_watcher(args: argparse.Namespace, *, once: bool) -> int:
    spec = _resolve_spec(args)
    if not args.skip_validate:
        spec.validate()
    if getattr(args, "json", False):
        # JSON 模式下 stdout 必须是干净的机器可读结果，
        # 终端横幅关掉（通知仍然会走 Webhook / 微信等渠道）。
        spec.notify.desktop = False
    client = _make_client(args, _need_token(args))
    watcher = Watcher(client, spec, journal=Journal(args.journal))
    try:
        outcome = watcher.run(once=once)
    finally:
        watcher.close()
        client.http.close()
    if args.json:
        _out(outcome.to_dict(), True)
    else:
        print(outcome.message or outcome.status)
    return _outcome_exit(outcome)


def cmd_grab(args: argparse.Namespace) -> int:
    """原子抢卡命令：查一次，有货就下单，没有就返回退出码 4。"""
    return _run_watcher(args, once=True)


def cmd_watch(args: argparse.Namespace) -> int:
    """持续监测。"""
    return _run_watcher(args, once=False)


def cmd_power(args: argparse.Namespace) -> int:
    with _open_client(args) as client:
        if args.action == "on":
            client.power_on(args.instance_uuid)
        elif args.action == "off":
            client.power_off(args.instance_uuid)
        else:
            client.release_instance(args.instance_uuid)
    _out({"ok": True, "instance_uuid": args.instance_uuid, "action": args.action}, args.json)
    return EXIT_OK


def cmd_journal(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    if args.ack:
        count = journal.ack_pending()
        if args.json:
            _out({"ok": True, "acked": count, "path": str(journal.path)}, True)
        else:
            print(f"已为 {count} 条未收尾记录补上「人工核对完成」，现在可以继续下单了。")
        return EXIT_OK
    records = journal.read_all()
    if args.json:
        _out({"ok": True, "summary": journal.summary(), "records": records}, True)
        return EXIT_OK
    print(json.dumps(journal.summary(), ensure_ascii=False, indent=2))
    if records:
        print()
        for record in records[-args.limit :]:
            print(
                f"{record.get('time')}  {record.get('kind'):<10}"
                f"{record.get('status', ''):<14}{record.get('message', '')[:90]}"
            )
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    if args.config_action == "init":
        spec = load_config(path)
        saved = save_config(spec, path)
        _out({"ok": True, "path": str(saved), "config": spec.to_dict()}, args.json)
        return EXIT_OK
    if args.config_action == "show":
        spec = load_config(path)
        _out({"ok": True, "path": str(path), "config": spec.to_dict()}, args.json)
        return EXIT_OK
    _out({"ok": True, "path": str(path), "env": ENV_CONFIG}, args.json)
    return EXIT_OK


# ---------------------------------------------------------------------- 解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autodl-ac",
        description="AutoDL 抢卡助手：拿开发者 Token 直接抢空闲 GPU 实例。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  autodl-ac login --token <Token>\n"
            "  autodl-ac doctor --json\n"
            "  autodl-ac stock --regions westDC2 --gpu-models 'RTX 4090'\n"
            "  autodl-ac grab --gpu-models 'RTX 4090' --regions westDC2 --yes\n"
            "  autodl-ac watch --gpu-models 'RTX 4090' --regions westDC2,beijingDC2 --yes\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"autodl-ac {__version__}")
    parser.add_argument("--config", help="配置文件路径（默认 ~/.autodl-autoconnect/config.json）")
    parser.add_argument("--token", help="AutoDL 开发者 Token（优先于本地保存的）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（给 Agent 用）")
    parser.add_argument("--base-url", default="https://api.autodl.com", help="AutoDL API 根地址")
    parser.add_argument("--http-timeout", type=float, default=10.0, help="单次请求超时（秒）")
    parser.add_argument("--http-retries", type=int, default=2, help="网络错误的重试次数")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- login
    p_login = sub.add_parser("login", help="保存并验证开发者 Token")
    p_login.add_argument("--token-file", help="Token 保存路径")
    p_login.set_defaults(func=cmd_login)

    # ---- doctor
    p_doctor = sub.add_parser("doctor", help="自检：Token、接口、日志状态")
    p_doctor.set_defaults(func=cmd_doctor)

    # ---- balance
    p_balance = sub.add_parser("balance", help="查看账户余额")
    p_balance.set_defaults(func=cmd_balance)

    # ---- stock
    p_stock = sub.add_parser("stock", help="查询一次库存")
    p_stock.add_argument("--regions", nargs="*", help="地区（官方代码或中文名）")
    p_stock.add_argument("--page-size", type=int, default=90)
    p_stock.set_defaults(func=cmd_stock)

    # ---- instances
    p_instances = sub.add_parser("instances", help="列出我的实例")
    p_instances.set_defaults(func=cmd_instances)

    # ---- power
    p_power = sub.add_parser("power", help="实例开关机/释放")
    p_power.add_argument("action", choices=["on", "off", "release"])
    p_power.add_argument("instance_uuid")
    p_power.set_defaults(func=cmd_power)

    # ---- journal
    p_journal = sub.add_parser("journal", help="查看意图日志")
    p_journal.add_argument("--journal", help="日志路径")
    p_journal.add_argument("--limit", type=int, default=20)
    p_journal.add_argument(
        "--ack", action="store_true", help="人工核对后收尾：解除「结果不明」对下单的阻塞"
    )
    p_journal.set_defaults(func=cmd_journal)

    # ---- config
    p_config = sub.add_parser("config", help="查看/初始化配置")
    p_config.add_argument("config_action", nargs="?", default="path", choices=["init", "show", "path"])
    p_config.set_defaults(func=cmd_config)

    # ---- grab / watch（共享参数）
    for name, help_text in (("grab", "查一次并立刻下单（原子操作）"),
                            ("watch", "持续监测并下单")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--gpu-models", nargs="*", help='要的型号，例如 "RTX 4090" "RTX 4090D"')
        p.add_argument("--gpu-model-regex", help='型号正则，例如 "RTX 40(90|90D)$"')
        p.add_argument("--gpu-count", type=int, help="需要的卡数")
        p.add_argument("--regions", nargs="*", help="地区（官方代码或中文名）")
        p.add_argument("--image-uuid", help="官方镜像 uuid")
        p.add_argument("--instance-name", help="实例名称")
        p.add_argument("--expand-data-disk-gb", type=int, help="扩展数据盘 GB")
        p.add_argument("--max-price-hourly", type=float, help="单卡每小时价格上限（元）")
        p.add_argument("--max-total-cost", type=float, help="本次预计总花费上限（元）")
        p.add_argument("--expected-hours", type=float, help="预计使用小时数")
        p.add_argument("--balance-reserve", type=float, help="余额保留额（元）")
        p.add_argument("--poll-seconds", type=float, help="轮询间隔（秒）")
        p.add_argument("--timeout-minutes", type=float, help="最长监测时长（分钟）")
        p.add_argument("--webhook-url", help="Webhook 地址")
        p.add_argument("--serverchan-key", help="Server酱 key")
        p.add_argument("--autodl-wechat-token", help="AutoDL 微信消息 token")
        p.add_argument("--no-desktop", action="store_true", help="不打印终端通知")
        p.add_argument("--yes", action="store_true", help="授权真实下单（扣费）")
        p.add_argument("--no-consent", action="store_true", help="只通知不下单")
        p.add_argument("--skip-validate", action="store_true", help="跳过配置校验")
        p.add_argument("--journal", help="意图日志路径")
        p.set_defaults(func=cmd_grab if name == "grab" else cmd_watch)

    # 所有子命令都支持 --journal，方便脚本统一传参。
    for subparser in sub.choices.values():
        has_journal = any(
            "--journal" in action.option_strings for action in subparser._actions
        )
        if not has_journal:
            subparser.add_argument("--journal", help="意图日志路径")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.json and getattr(args, "command", "") not in ("login",):
        # 横幅走 stderr：stdout 留给结果，方便管道和 Agent 直接读。
        print(BANNER % __version__, file=sys.stderr, end="")

    try:
        return args.func(args)
    except AuthError as exc:
        return _fail(exc, EXIT_AUTH, args.json)
    except ConfigError as exc:
        return _fail(exc, EXIT_CONFIG, args.json)
    except NetworkError as exc:
        return _fail(exc, EXIT_NETWORK, args.json)
    except APIError as exc:
        return _fail(exc, EXIT_BUSINESS, args.json)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, EXIT_ERROR, args.json)


def _fail(exc: Exception, code: int, as_json: bool) -> int:
    message = redact(str(exc))
    if as_json:
        print(
            json.dumps(
                {"ok": False, "error": {"code": code, "message": message}},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"错误：{message}", file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
