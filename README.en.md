# Practical-tools-AIresearch

**English** | [简体中文](README.md)

A collection of small, practical tools accumulated while doing AI research.
Each tool lives in its own directory, independent of the others, so you can
clone just the one you need.

| Directory | What it does | Stack |
| --- | --- | --- |
| [`Autodl-AutoConnect/`](Autodl-AutoConnect/) | Grab idle AutoDL GPU instances directly with a developer token; agent-friendly, no browser, no manual page-refreshing | Python (zero dependencies) |

---

## Autodl-AutoConnect

Popular AutoDL GPUs sit at zero idle capacity all day. You refresh for half an
hour, a card appears, and it is gone in thirty seconds. This tool turns
"staring at the AutoDL page" into a single command:

```bash
# Notify only, do not order (costs nothing)
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2

# Actually place orders
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2 --yes
```

- **No browser**: calls AutoDL's own endpoints with a developer token
- **Agent-friendly**: every command supports `--json` with meaningful exit codes
- **No double charging**: intent journal + idempotent reconciliation + "never retry an ambiguous order"
- **Spending guards**: price cap / total budget / balance pre-check
- **Fast**: keep-alive connections (one fewer TCP+TLS handshake per request
  since 1.1.0), a single round-trip before ordering, 2-second polling

See [Autodl-AutoConnect/README.en.md](Autodl-AutoConnect/README.en.md)
([中文](Autodl-AutoConnect/README.md)).

---

## Usage

Every sub-project has its own README; just open the directory you need.
Most tools have no third-party dependencies and run straight from a clone.

## License

Each sub-project ships its own LICENSE.
