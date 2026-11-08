<div align="center">

# 🧰 Practical Tools for AI Research

*Small tools I built while running experiments — each one removes a specific annoying step.*

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Tools](https://img.shields.io/badge/tools-Autodl--AutoConnect-6f42c1) ![License](https://img.shields.io/badge/license-MIT-green)

<sub><b>English</b> · <a href="README.zh-CN.md">中文</a></sub>

</div>

## 📦 Tools

| Tool | What problem it solves | Status |
| --- | --- | --- |
| [Autodl-AutoConnect](Autodl-AutoConnect/) | Watches AutoDL for an idle GPU and grabs it with one command — no browser, no manual page-refreshing | 🧪 Beta · v1.1.0 |

## ⚡ Autodl-AutoConnect

AutoDL's popular GPUs sit at zero idle capacity most of the day: you refresh for half an hour, a card appears, and it is gone before you can click. Autodl-AutoConnect watches AutoDL for an idle GPU and grabs it with one command. It calls AutoDL's own endpoints with your developer token, so there is no browser, password, or captcha flow.

```bash
# Save the developer token once (AutoDL console -> Settings -> Developer Token)
autodl-ac --token <your-token> login

# Notify only — the default, no money is spent
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2

# Real order, with hard spending guard-rails
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2 \
  --max-price-hourly 3.0 --expected-hours 4 --max-total-cost 20 \
  --balance-reserve 5 --yes
```

**The flags that matter:**

- `--gpu-models` — models to match (for example `"RTX 4090"`); `--regions` — one or more AutoDL regions (for example `westDC2`).
- `--image-uuid` — the official AutoDL image UUID; AutoDL usually rejects orders without one, so set it explicitly.
- 🛡️ `--max-price-hourly` — per-GPU price ceiling in CNY/hour.
- 💸 `--expected-hours` + `--max-total-cost` — the estimated total is price × GPUs × expected hours; the order is skipped if it would exceed the cap.
- 🏦 `--balance-reserve` — available balance must cover the estimated cost plus this reserve.
- ✋ `--yes` — authorizes a real, paid order for this run. The default config is notify-only; `--no-consent` forces notify-only.
- 🤖 `--json` — machine-readable output for agent hand-off. It is a **global** option, so it goes before the subcommand:

```bash
autodl-ac --json grab --gpu-models "RTX 4090" --regions westDC2 --yes
```

**Safety by construction:** before ordering, the tool checks whether that machine already hosts one of your instances (idempotent reconciliation), and it journals every order intent. If an order result is ambiguous, it never retries; it reconciles read-only and blocks further ordering until you verify in the console and run `autodl-ac journal --ack`. Exit code `5` means "ambiguous"; `4` means no match or watch timeout.

See [Autodl-AutoConnect/README.en.md](Autodl-AutoConnect/README.en.md) for the full command reference ([Chinese](Autodl-AutoConnect/README.md)).

## 🧪 Why these exist

Most of my experiment time was not lost to thinking. It was lost to waiting — waiting for a GPU to free up, for a token to load, for yet another page refresh. Each tool here removes one of those specific waits, so the loop from "I have an idea" to "the run is going" stays as short as possible.

## 🤝 Using them

- **Python 3.9+**, with **zero runtime dependencies** for Autodl-AutoConnect (standard library only), as declared in [`pyproject.toml`](Autodl-AutoConnect/pyproject.toml).
- An **AutoDL developer token** is required; get it from the AutoDL console under Settings -> Developer Token.
- Install the `autodl-ac` command from a clone, or run the package directly:

```bash
cd Autodl-AutoConnect
pip install -e .
autodl-ac --help
```

Without installing: `python -m autodl_autoconnect --help`. Every sub-project has its own README; start with the directory you need.

## 📄 License

Autodl-AutoConnect is released under the [MIT License](Autodl-AutoConnect/LICENSE). Each sub-project ships its own `LICENSE` file.
