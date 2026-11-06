# 安全与费用说明

本工具会**真实花钱**（AutoDL 按量计费）。这一页把每一条护栏讲清楚，
以及你需要自己承担什么。

## 需要你做决定的事

| 决定 | 在哪里 | 默认 |
| --- | --- | --- |
| 要不要真的下单 | `paid_consent`（配置）+ `--yes`（命令行） | 两处都默认「否」 |
| 最多花多少钱 | `max_price_hourly` / `max_total_cost` | 无上限（**建议自己设**） |
| 至少留多少钱 | `balance_reserve` | 0 |
| 盯哪些卡、哪些地区 | `gpu_models` / `regions` | 无（**必须自己填，否则配置校验会拦下**） |

两道锁：`paid_consent` 和 `--yes` **都**打开，才会真的下单。
只开一个不会扣费。

## 七条护栏

### 1. 默认只观察

`paid_consent` 默认 `false`。此时命中库存只会发通知，不会调用下单接口。

### 2. 意图日志先行

下单**之前**把意图写进 `~/.autodl-autoconnect/journal.jsonl`：

```json
{"ts": 1793607760.85, "kind": "intent", "machine_id": "b7f3c9a2",
 "gpu_names": ["RTX 4090"], "gpu_count": 1, "price_hourly": 2.5, ...}
```

写入方式：追加写 + `flush` + `fsync`。进程被强杀也不丢最后一行。

### 3. 结果不明绝不重试

下单请求超时或连接中断时，客户端**无法区分**两种情况：

- 请求没到服务端，什么都没发生；
- 服务端已经创建了实例，只是回包丢了。

如果盲目重试，可能重复扣费。因此本工具：

1. 下单请求**关闭自动重试**（`max_retries=0`）；
2. 捕获到网络错误后写一条 `ambiguous` 记录；
3. 立刻转成**只读对账**——查实例列表，看这台机器上是不是已经有实例了；
4. 找到了 → 认定成功，继续；没找到 → **停止一切下单**，提示人工核对。

被拦下之后，即使换一个进程重新运行，`doctor` 和抢卡命令也会因为日志里
存在「未收尾的 intent」而拒绝下单，直到你人工确认。

### 4. 幂等

下单前先查实例列表：如果这台机器上已经有本工具创建的实例，直接跳过，
不会下第二单。

### 5. 拿不准就不抢

以下任一情况都不下单，并在日志里写清原因：

- 机器不在线（`on_line` ∈ {3,4,5,6}）
- 机器已挂满实例（`binding_instance_num >= max_instance_num`）
- 空闲卡数不足
- 地区不符
- 型号不符
- **拿不到价格**（设了价格上限时）——宁可错过，不可乱抢
- 单价超过 `max_price_hourly`
- 预计总花费超过 `max_total_cost`
- 余额不足（含 AutoDL 的多卡规则：钱包余额需大于 10 元，代金券不计）

### 6. 凭据不外泄

- Token 不进日志、不进异常消息：所有错误消息统一走 `errors.redact()`。
- Windows 用 **DPAPI**（当前用户作用域）加密落盘；换机器/换账号解不开。
- 其它平台退化为 base64 + `0600` 权限，并写一个提示文件告诉你更安全的做法
  （环境变量 `AUTODL_TOKEN`）。
- 通知渠道的 Token 同样脱敏。

### 7. 一切在本机

- 不经过任何第三方服务器；Webhook 是直连你自己配的地址。
- 状态目录 `~/.autodl-autoconnect/` 里是 Token、配置和意图日志，
  **不要分享这个目录**（可以用环境变量 `AUTODL_AUTOCONNECT_HOME` 换位置）。
- 工具只访问 `https://api.autodl.com` 和你配置的通知地址。

## 日志里能看到什么

```bash
autodl-ac journal
```

```
{
  "records": 12,
  "intents": 3,
  "results": 3,
  "created": 2,
  "ambiguous": 1,
  "estimated_spend": 20.0
}
```

`estimated_spend` 是按「单价 × 卡数 × 记录的时长」估算的，
用来回答「我到底花了多少」。

## 手动解除「结果不明」的拦截

先在 AutoDL 控制台核对实例列表，然后：

```bash
# 1. 看清楚那条未收尾的记录
autodl-ac journal

# 2. 确认过控制台之后，一条命令收尾（会给每条未收尾的 intent 补一条 result）
autodl-ac journal --ack
```

`--ack` 只写本地日志，不会去查接口、也不会下单；它表达的是「我人工核对过了」。

如果更喜欢手工处理，也可以直接编辑
`~/.autodl-autoconnect/journal.jsonl`：删掉那行 `kind=intent` 的记录，
或者补一条 `result` 把它收尾：

```json
{"ts": 1793607761.0, "kind": "result", "machine_id": "b7f3c9a2", "instance_uuid": "inst-xxxx", "status": "confirmed"}
```

## 与 AutoDL 官方的关系

本项目与 AutoDL 官方无隶属关系，是第三方开源工具。
它调用的是 AutoDL 控制台自身在用的接口（除余额外，官方未公开文档），
使用前请自行对照 [AutoDL 用户协议](https://www.autodl.com/docs/agreements/)。

账号异常、频繁下单、异常行为都有可能被平台风控，
请按自己的判断使用，不要用来囤卡或转售。
