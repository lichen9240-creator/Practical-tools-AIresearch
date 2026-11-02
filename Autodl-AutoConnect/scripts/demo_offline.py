"""离线演示：不登录 AutoDL、不租卡、不花钱，跑一遍完整链路。

它会启动一个本地假 AutoDL 服务，然后用**真实的 CLI 命令**打过去，
把每一步的输出打印出来。想看这个工具到底怎么工作、又不想拿真卡试，
就跑这个：

    python scripts/demo_offline.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fake_autodl import FakeAutoDL  # noqa: E402


def banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {text}")
    print("=" * 72)


def run(args: list[str], env: dict) -> tuple[int, str, str]:
    """跑一条真实 CLI 命令，返回 (退出码, stdout, stderr)。

    stdout 只放机器可读结果；终端横幅等人类提示都在 stderr。
    """
    result = subprocess.run(
        [sys.executable, "-m", "autodl_autoconnect", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )
    return result.returncode, result.stdout or "", result.stderr or ""


def main() -> int:
    home = tempfile.mkdtemp(prefix="autodl-ac-demo-")
    env = dict(os.environ)
    env["AUTODL_AUTOCONNECT_HOME"] = home
    env["PYTHONIOENCODING"] = "utf-8"

    with FakeAutoDL() as fake:
        fake.add_machine(
            machine_id="b7f3c9a2",
            region_sign="westDC2",
            gpu_type_name=["RTX 4090"],
            gpu_idle_num=4,
            pay_price=2.5,
        )
        fake.add_machine(
            machine_id="c1d2e3f4",
            region_sign="beijingDC2",
            gpu_type_name=["A100"],
            gpu_idle_num=8,
            pay_price=6.8,
        )
        fake.state.balance_milli = 100_000

        base = [
            "--config",
            str(Path(home) / "config.json"),
            "--token",
            fake.token,
            "--base-url",
            fake.base_url,
            "--json",
        ]

        banner("1) doctor —— 自检")
        code, out, err = run([*base, "doctor"], env)
        print(out.strip())
        print(f"[exit={code}]")

        banner("2) stock —— 看一眼库存")
        code, out, err = run([*base, "stock"], env)
        payload = json.loads(out)
        for offer in payload["offers"]:
            print(
                f"  {offer['region_sign']:<12} {'/'.join(offer['gpu_names']):<14}"
                f" 空闲 {offer['idle_gpu_num']:<3} 单价 {offer['price_hourly']}/时"
            )
        print(f"[exit={code}]")

        banner("3) grab（未授权）—— 只通知，不下单")
        code, out, err = run(
            [*base, "grab", "--gpu-models", "RTX 4090", "--regions", "westDC2"], env
        )
        payload = json.loads(out)
        print(f"  status   = {payload['status']}")
        print(f"  matched  = {payload['matched']}")
        print(f"  ordered  = {payload['ordered']}   <- 没有下任何单")
        print(f"  message  = {payload['message']}")
        print(f"[exit={code}]  实际下单次数 = {len(fake.state.create_calls)}")

        banner("4) grab --yes —— 授权下单")
        code, out, err = run(
            [
                *base,
                "grab",
                "--gpu-models",
                "RTX 4090",
                "--regions",
                "westDC2",
                "--image-uuid",
                "img-demo",
                "--yes",
            ],
            env,
        )
        payload = json.loads(out)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[exit={code}]")
        print(f"  服务器端实际创建的实例数 = {len(fake.state.instances)}")
        print(f"  实例 uuid = {fake.state.instances[0]['uuid']}")

        banner("5) journal —— 意图日志")
        code, out, err = run([*base, "journal"], env)
        payload = json.loads(out)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        for record in payload["records"]:
            print(
                f"  {record['time']}  {record['kind']:<9}{record['status']:<11}"
                f"{record['message'][:60]}"
            )

        banner("6) 下单结果不明 —— 只发一次请求，然后停下")
        # 换一个地区，避免被第 4 步已经建好的实例触发幂等保护。
        fake.add_machine(
            machine_id="deadbeef",
            region_sign="yangzhouDC1",
            gpu_type_name=["RTX 4090"],
            gpu_idle_num=2,
        )
        fake.state.fail_next_create_with = ConnectionResetError("模拟服务端中途断开")
        before = len(fake.state.create_calls)
        code, out, err = run(
            [
                *base,
                "grab",
                "--gpu-models",
                "RTX 4090",
                "--regions",
                "yangzhouDC1",
                "--yes",
            ],
            env,
        )
        payload = json.loads(out)
        sent = len(fake.state.create_calls) - before
        print(f"  status  = {payload['status']}")
        print(f"  message = {payload['message']}")
        print(f"  下单请求发出次数 = {sent}  <- 只发 1 次，没有自动重试（避免重复扣费）")
        print(f"[exit={code}]")

        banner("完成：全程没有登录 AutoDL、没有租卡、没有花钱")
        print(f"  临时状态目录：{home}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
