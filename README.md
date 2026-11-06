# Practical-tools-AIresearch

[English](README.en.md) | **简体中文**

做 AI 研究过程中攒下来的一些实用小工具。每个工具一个独立目录，
互不依赖，可以单独 clone 出来用。

| 目录 | 简介 | 技术栈 |
| --- | --- | --- |
| [`Autodl-AutoConnect/`](Autodl-AutoConnect/) | 用 AutoDL 开发者 Token 直接抢空闲 GPU 实例，可被 Agent 接管；不用浏览器、不用人守页面 | Python（零依赖） |

---

## Autodl-AutoConnect

AutoDL 的热门卡常年 0 空闲，页面刷半天，出来的卡几十秒就被租走。
这个工具把「盯着 AutoDL 等卡」变成一条命令：

```bash
# 只通知，不下单（默认不花钱）
autodl-ac watch --gpu-models "RTX 4090" --regions 西北B区

# 授权下单
autodl-ac watch --gpu-models "RTX 4090" --regions 西北B区 --yes
```

- **不用开浏览器**：直接调用 AutoDL 自己的接口，用开发者 Token 鉴权
- **Agent 能接管**：每条命令都支持 `--json`，退出码有明确语义
- **不会重复扣费**：意图日志 + 幂等对账 + 「结果不明绝不重试」
- **花钱有护栏**：单价上限 / 总预算 / 余额预检，缺一不可
- **快**：长连接复用（1.1.0 起每个请求省一次 TCP+TLS 握手），
  下单前只留一个网络往返，轮询 2 秒一次

详见 [Autodl-AutoConnect/README.md](Autodl-AutoConnect/README.md)（[English](Autodl-AutoConnect/README.en.md)）。

---

## 使用方式

每个子项目都有自己的 README，直接进对应目录看即可。
大部分工具不依赖第三方框架，克隆下来就能跑。

## 许可

各子项目各自带 LICENSE。
