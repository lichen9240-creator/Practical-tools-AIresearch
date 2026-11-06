# Autodl-AutoConnect

[English](README.en.md) | **简体中文**

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
| 反应速度 | 人眼级，几十秒到几分钟 | 轮询 2 秒一次（±15% 抖动） |
| 需要开着电脑/浏览器 | 是 | 否，一台小机器即可 |
| Agent 能不能接管 | 不能 | **能**（`--json` + 明确退出码） |
| 会不会重复扣费 | — | 意图日志 + 幂等对账，**结果不明绝不重试** |
| 花费上限 | 靠自觉 | 硬性护栏：单价上限 / 总预算 / 余额预检 |

**一句话**：本工具把「盯着 AutoDL 页面等卡」这件事，变成一条 Agent 可以直接调用的命令。

---

## 1.1.0 做了什么：更快

抢卡是短跑：从「发现空闲卡」到「下单请求抵达 AutoDL」之间，谁快谁赢。
1.1.0 把这条路径上的每一段都量了一遍，然后砍掉其中的浪费。

### 1）长连接复用（最大的一笔）

旧实现用 `urllib`，**每个请求都要重新做一次 TCP + TLS 握手**。现在改用标准库
`http.client` 维护长连接（每个线程一条，用完全部关闭）：

| 同一台机器、连发 5 次余额请求 | 第 1 次 | 第 2 次 | 第 3 次 | 第 4 次 | 第 5 次 | 合计 |
| --- | --- | --- | --- | --- | --- | --- |
| 1.0（每个请求重连） | 3183 ms | 2184 ms | 2299 ms | 3023 ms | 5227 ms | **15.9 s** |
| 1.1（长连接） | 3087 ms | 440 ms | 430 ms | 440 ms | 440 ms | **4.8 s** |

> 这是在一台到 `api.autodl.com` 的 TCP+TLS 握手要 1.9 秒的机器上测的（跨境线路），
> 所以绝对值偏大；国内直连的握手通常是几十毫秒。但「每个请求多一次握手」在任何网络
> 上都成立——首单要发 6 个请求，就是 6 次握手。多次运行的总耗时在 14~16 秒 vs
> 4.8~5.3 秒之间。

### 2）下单前只留一个网络往返

「查余额」和「查这台机器上是不是已经下过单」是两个互不依赖的只读请求，
1.0 串行发，1.1 并发发。

用仓库自带的假 AutoDL 服务（每个响应人为延迟 50 ms 模拟 RTT）实测：

| | 一次 `grab` 耗时 | 建立的 TCP 连接 |
| --- | --- | --- |
| 1.0 | 约 450~510 ms | 6 |
| 1.1 | 约 360~370 ms | 3 |

连续轮询（`watch` 的稳态）10 轮：

| | 10 轮耗时 | 建立的 TCP 连接 |
| --- | --- | --- |
| 1.0 | 约 600~650 ms | 10 |
| 1.1 | 约 520~550 ms | **1** |

轮询一个下午（3 小时 × 2 秒 ≈ 5400 轮），旧实现要做 5400 次握手，新实现只做 1 次。

### 3）确认环节不再干等 12 秒

下单成功后，1.0 要「最多 6 次 × 2 秒」去确认实例是否出现，最坏 12 秒才返回。
1.1 在服务端已经返回 uuid 时只做一次轻量核对，核对不上再退化为查实例列表，
最坏约 1 秒。`grab` 是 Agent 循环里反复调用的命令，这个改动直接决定了循环频率。

### 4）顺手修掉的问题

- **退出码 3/6 之前是错的**：`grab`/`watch` 遇到鉴权失败、网络错误时都被归成了 7。
  现在按 README 的约定返回 3 / 6 / 7。
- **`doctor` 会在接口报错时崩掉**：`except AutoDLError` 用了一个没导入的名字。
- **`cooldown_seconds` 之前只写在配置里，没有实现**：现在下单失败后会真正冷却。
- **通知会刷屏**：`watch` 模式下余额不足每轮都发一次；现在 5 分钟内同类通知只发一次。
- **下单遇到鉴权失败会留下「未收尾」记录**：确定失败的请求现在也会写 result 收尾。
- 新增 `autodl-ac journal --ack`：人工核对完控制台后，一条命令解除「结果不明」的阻塞。

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
| 4 | 没匹配到符合条件的机器（含 `watch` 超时） | 稍后重试 |
| 5 | **下单结果不明** | **停止，通知人工核对**（工具自己也会停） |
| 6 | 网络错误 | 稍后重试 |
| 7 | 接口业务错误 | 看 `error_code` / `message` |

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

出错时多一个 `error_code`（如 `AUTH_FAILED` / `NETWORK_ERROR` / `NoStock`），
它就是退出码 3 / 6 / 7 的细分依据：

```json
{
  "ok": false,
  "matched": false,
  "ordered": false,
  "status": "error",
  "message": "查询库存失败：网络访问失败：...",
  "error_code": "NETWORK_ERROR"
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
    time.sleep(2)
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
  "poll_seconds": 2.0,
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
| `poll_seconds` | 轮询间隔，默认 2 秒；实际会在 ±15% 内抖动 |
| `cooldown_seconds` | 下单失败后的冷却时间（秒），默认 20 |
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
核对完控制台后，用 `autodl-ac journal --ack` 收尾即可继续。

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

Webhook 的请求体（本项目自定义的格式）：

```json
{
  "source": "autodl-autoconnect",
  "kind": "order_created",
  "headline": "AutoDL 抢卡成功",
  "label": "Autodl-AutoConnect",
  "message": "实例 inst-xxxx\n机器 b7f3c9a2\n卡数 1\n单价 2.5 元/时",
  "sent_at": "2026-11-02T162920+0800"
}
```

请求头带 `X-Event-Id`（幂等去重用），可选用 `Authorization: Bearer <webhook_token>`。

`kind` 取值：`stock_found`（有货）、`order_created`（抢到）、
`balance_low`（余额不足）、`ambiguous`（结果不明）。
同一类通知 5 分钟内只发一次（`balance_low` 这类不会每轮刷屏）。

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
| `journal --ack` | 人工核对后收尾，解除「结果不明」的阻塞 |
| `config init/show/path` | 配置管理 |

---

## 跑测试

测试用一个**本地假 AutoDL 服务**（`tests/fake_autodl.py`），
按真实接口的路径、请求体和信封格式应答，所以跑的是真实 HTTP 往返，
不会登录 AutoDL、不会租卡、不会花钱。假服务是 HTTP/1.1 长连接，
因此也能测出客户端有没有真的复用连接。

```bash
python -m unittest discover -s tests -t . -v
```

```
Ran 78 tests in 11.2s

OK
```

---

## 已知边界

- **必须用开发者 Token**，不能用账号密码（工具不碰验证码，也不做密码登录）。
- **`image_uuid` 建议显式指定**：留空时 AutoDL 多半会拒绝创建。
- 控制台那几个接口**没有官方文档**，官方改版可能改路径/字段。
  所有端点集中在 `autodl_autoconnect/api.py` 的 `ENDPOINTS` 里，
  解析层对字段名做了多候选兼容，改了不用动其它地方。
- **配了系统/环境变量代理时**，长连接会自动退回 `urllib`（保证代理、证书、
  `no_proxy` 的行为不变），代价是每个请求重新握手。直连不受影响。
- 本项目与 AutoDL 官方无隶属关系，使用前请对照 AutoDL 用户协议。


## 更新日志

### 1.1.0

- HTTP 层改为长连接复用（每线程一条），单次请求省掉一次 TCP+TLS 握手。
- 下单前的余额检查与重复下单检查改为并发。
- 下单确认从最坏 12 秒降到约 1 秒。
- 轮询默认 3 秒 → 2 秒，并加入 ±15% 抖动。
- 修复退出码 3/6 被错误映射成 7 的问题。
- 修复 `doctor` 在接口报错时抛 `NameError` 的问题。
- 实现 `cooldown_seconds`；通知去重，不再刷屏。
- 下单遇到业务/鉴权错误时也会写 result 收尾（不再误判为「结果不明」）。
- 新增 `journal --ack`。
- `--json` 输出增加 `error_code`；候选列表最多 20 条。
- 测试 69 → 78 项。

### 1.0.0

- 首个版本。

---

## 致谢

- [Stop-Manually-Refreshing-AutoDL-GPU](https://github.com/cc2025cheng/Stop-Manually-Refreshing-AutoDL-GPU) —— 同一个问题的开源解答，本项目的产品思路受其启发。
- [AutoDL 官方文档](https://www.autodl.com/docs/common_api/) —— 开发者 API 与地区代码表。
- 本项目与 AutoDL 官方无隶属关系。

## 许可证

[MIT](LICENSE)
