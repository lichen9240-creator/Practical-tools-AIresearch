# Autodl-AutoConnect

> 拿一个 AutoDL 开发者 Token，让 Agent 直接把空闲 GPU 抢到手。
> 不用开浏览器、不用守着页面刷、不用手点「租用」。

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![依赖](https://img.shields.io/badge/dependencies-0-brightgreen)](pyproject.toml)

---

## 这东西解决什么

AutoDL 的热门卡常年 0 空闲，页面刷半天，出来的卡几十秒就被租走。
本工具做的是同一件事，但不需要人守着：

| | 手动刷新页面 | 本工具 |
| --- | --- | --- |
| 反应速度 | 人眼级，几十秒到几分钟 | 轮询 3 秒一次 |
| 需要开着电脑/浏览器 | 是 | 否，一台小机器即可 |
| Agent 能不能接管 | 不能 | **能**（`--json` + 明确退出码） |
| 会不会重复扣费 | — | 意图日志 + 幂等对账，**结果不明绝不重试** |
| 花费上限 | 靠自觉 | 硬性护栏：单价上限 / 总预算 / 余额预检 |

**一句话**：本工具把「盯着 AutoDL 页面等卡」这件事，变成一条 Agent 可以直接调用的命令。

---

## 它凭什么不用开浏览器

AutoDL 的**官方开放 API 只覆盖余额查询**，没有公开的「查库存 / 下单」接口。
但 AutoDL 控制台本身就是网页应用，它下订单时调的是自己的接口。

本工具直接调用同一批接口，用的是**同一个开发者 Token**：

```
Authorization: <你的开发者 Token>
```

关键发现（可自行复核）：AutoDL 控制台前端在 `api.autodl.com` 上用
`?token=xxx` 就能直接进入控制台，说明开发者 Token 与网页会话 Token 是同一套凭据。

所以：**你只要有一个开发者 Token，就够跑完整个流程，不需要账号密码、不需要验证码、不需要浏览器。**

接口清单与逆向过程见 [`docs/API.md`](docs/API.md)。

---

## 安装

零依赖，Python 3.9+ 即可：

```bash
git clone <本仓库>
cd Autodl-AutoConnect

# Windows
python -m autodl_autoconnect --help

# 或者装成命令
pip install -e .
autodl-ac --help
```

不想装包也可以直接跑：`python -m autodl_autoconnect`。

---

## 三分钟上手

### 1. 拿 Token

AutoDL 控制台 → 设置 → 开发者 Token → 复制。

### 2. 存好 Token

```bash
autodl-ac login --token <你的Token>
```

Windows 上 Token 用 **DPAPI** 加密后落盘（当前用户才能解）；
其它平台退化为 base64 + 0600 权限，并会提示你改用环境变量 `AUTODL_TOKEN`。

### 3. 自检

```bash
autodl-ac doctor
```

```
✔ Token           已配置
✔ 余额接口         余额 ¥100.000
✔ 库存接口         取到 42 条报价
✔ 未收尾的下单记录  无
```

### 4. 看一眼库存

```bash
autodl-ac stock --regions 西北B区
```

### 5. 抢卡

```bash
# 只通知，不下单（默认行为，不花钱）
autodl-ac watch --gpu-models "RTX 4090" --regions 西北B区,北京B区

# 确认要下单了（会真实扣费）
autodl-ac watch --gpu-models "RTX 4090" --regions 西北B区,北京B区 --yes
```

---

## 给 Agent 用

每个命令都支持 `--json`，输出机器可解析的结果；退出码有明确语义：

| 退出码 | 含义 | Agent 该怎么做 |
| --- | --- | --- |
| 0 | 成功（含「只通知」模式命中库存） | 读 JSON 里的 `status` |
| 2 | 配置错误 | 修配置 |
| 3 | 鉴权失败 | Token 过期，让用户换 |
| 4 | 没匹配到符合条件的机器 | 稍后重试 |
| 5 | **下单结果不明** | **停止，通知人工核对**（工具自己也会停） |
| 6 | 网络错误 | 稍后重试 |
| 7 | 接口业务错误 | 看 message |

一次性的原子命令（最适合放进 Agent 的循环里）：

```bash
autodl-ac grab --gpu-models "RTX 4090" --regions 西北B区 --yes --json
```

```json
{
  "ok": true,
  "matched": true,
  "ordered": true,
  "status": "confirmed",
  "message": "已抢到：实例 inst-xxxx",
  "order": {
    "instance_uuid": "inst-xxxx",
    "machine_id": "b7f3c9a2",
    "gpu_count": 1,
    "price_hourly": 2.5,
    "confirmed": true
  }
}
```

Agent 的典型用法：

```python
import subprocess, json

while True:
    r = subprocess.run(
        ["autodl-ac", "grab", "--gpu-models", "RTX 4090",
         "--regions", "西北B区", "--yes", "--json"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        result = json.loads(r.stdout)
        if result["ordered"]:
            print("抢到了:", result["order"]["instance_uuid"])
            break
    elif r.returncode == 5:
        print("结果不明，请人工核对 AutoDL 控制台"); break
    time.sleep(5)
```

---

## 配置

配置文件默认在 `~/.autodl-autoconnect/config.json`，
用 `autodl-ac config init` 生成、`autodl-ac config show` 查看。
命令行参数会覆盖配置文件。

```json
{
  "gpu_models": ["RTX 4090", "RTX 4090D"],
  "gpu_count": 1,
  "regions": ["西北B区", "北京B区"],
  "image_uuid": "",
  "max_price_hourly": 3.0,
  "max_total_cost": 20.0,
  "expected_hours": 4.0,
  "balance_reserve": 5.0,
  "paid_consent": false,
  "poll_seconds": 3.0,
  "timeout_minutes": 1440,
  "notify": {
    "webhook_url": "",
    "serverchan_key": "",
    "autodl_wechat_token": "",
    "desktop": true
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `gpu_models` | 要的型号。`"4090"` 这种简写也能匹配 `RTX 4090D` |
| `gpu_model_regex` | 高级用法，正则匹配型号，和 `gpu_models` 二选一 |
| `regions` | 地区，官方代码（`westDC2`）或中文名（`西北B区`）都行 |
| `image_uuid` | 官方镜像 uuid。**留空多数情况下下单会被 AutoDL 拒绝，建议显式指定** |
| `max_price_hourly` | 单卡每小时价格上限（元），超了不抢 |
| `max_total_cost` | 本次预计总花费上限（元）＝ 单价 × 卡数 × `expected_hours` |
| `balance_reserve` | 余额保留额，预检时余额必须高于「预计花费 + 保留额」 |
| `paid_consent` | **默认 `false`**：只通知。改成 `true` 才会真的下单扣费 |
| `poll_seconds` | 轮询间隔，默认 3 秒 |
| `timeout_minutes` | 最长监测时长，0 表示不限 |

---

## 安全设计

花的是真钱，所以这一层做得比较认真。

**1. 默认不扣费。** `paid_consent` 默认 `false`，命令行还要再给 `--yes`。
两道锁都打开才会真的下单。

**2. 意图日志（intent journal）。** 下单前先把「打算做什么」写进
`~/.autodl-autoconnect/journal.jsonl`（追加写 + fsync），下单后写结果。

**3. 结果不明绝不重试。** 下单请求超时/断线时，无法区分「没下成功」和
「下成功了但回包丢了」。此时工具**不会重试**，而是转成只读对账
（查实例列表），并且**停止后续一切下单**，等人工确认。
这是防重复扣费最关键的一条。

**4. 幂等。** 下单前先看这台机器上是不是已经有自己的实例了，有就跳过。

**5. 拿不准就不抢。** 价格拿不到、机器不在线、机器已挂满实例、余额不足、
超预算——任何一条不满足就不下单，并在日志里写清原因。

**6. 凭据不外泄。** Token 不进日志、不进异常消息（统一脱敏）；
Windows 用 DPAPI 加密；服务端只回显必要字段。

**7. 一切在本机。** 不经过任何第三方服务器；通知是直连你自己配的地址。

细节见 [`docs/SECURITY.md`](docs/SECURITY.md)。

---

## 通知

| 渠道 | 配置 |
| --- | --- |
| 终端横幅 + 蜂鸣（Windows） | 默认开，`--no-desktop` 关 |
| Webhook | `notify.webhook_url`，POST JSON，可带 `Bearer` 认证 |
| Server酱（推微信） | `notify.serverchan_key` |
| AutoDL 微信消息 | `notify.autodl_wechat_token`（AutoDL 控制台 → 设置 → 微信消息） |

Webhook 的请求体：

```json
{
  "id": "autodl-1793607760000",
  "event": "order_created",
  "title": "AutoDL 抢卡成功",
  "name": "Autodl-AutoConnect",
  "content": "实例 inst-xxxx\n机器 b7f3c9a2\n卡数 1\n单价 2.5 元/时",
  "timestamp": "2026-11-02T16:29:20+08:00"
}
```

`event` 取值：`stock_found`（有货）、`order_created`（抢到）、
`balance_low`（余额不足）、`ambiguous`（结果不明）。

---

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `login` | 保存并验证 Token |
| `doctor` | 自检：Token、接口、日志状态 |
| `balance` | 查看余额 |
| `stock` | 查询一次库存 |
| `grab` | 查一次并立刻下单（原子操作，最适合 Agent） |
| `watch` | 持续监测并下单 |
| `instances` | 列出我的实例 |
| `power on/off/release <uuid>` | 开关机 / 释放实例 |
| `journal` | 查看意图日志 |
| `config init/show/path` | 配置管理 |

---

## 跑测试

测试用一个**本地假 AutoDL 服务**（`tests/fake_autodl.py`），
按真实接口的路径、请求体和信封格式应答，所以跑的是真实 HTTP 往返，
不会登录 AutoDL、不会租卡、不会花钱。

```bash
python -m unittest discover -s tests -t . -v
```

```
Ran 69 tests in 20.6s

OK
```

---

## 已知边界

- **必须用开发者 Token**，不能用账号密码（工具不碰验证码，也不做密码登录）。
- **`image_uuid` 建议显式指定**：留空时 AutoDL 多半会拒绝创建。
- 控制台那几个接口**没有官方文档**，官方改版可能改路径/字段。
  所有端点集中在 `autodl_autoconnect/api.py` 的 `ENDPOINTS` 里，
  解析层对字段名做了多候选兼容，改了不用动其它地方。
- 本项目与 AutoDL 官方无隶属关系，使用前请对照 AutoDL 用户协议。

---

## 致谢

参考并借鉴了 [Stop-Manually-Refreshing-AutoDL-GPU](https://github.com/cc2025cheng/Stop-Manually-Refreshing-AutoDL-GPU)
的产品思路（安全护栏、意图日志、克隆换卡、微信通知）。
本项目的差异是**绕开浏览器，直接走接口**，因此可以跑在无图形界面的机器上，
也更容易被 Agent 接管。

## 许可证

[MIT](LICENSE)
