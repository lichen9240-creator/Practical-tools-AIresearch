# Autodl-AutoConnect

**English** | [简体中文](README.md)

> One AutoDL developer token, and an agent can grab an idle GPU for you.
> No browser, no page-refreshing, no clicking "rent" by hand.

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](pyproject.toml)

---

## What problem it solves

Popular AutoDL GPUs sit at zero idle capacity all day. You refresh the page for
half an hour, a card appears, and it is gone in thirty seconds. This tool does the
same job without a human watching:

| | Refreshing the page | This tool |
| --- | --- | --- |
| Reaction time | Human, tens of seconds to minutes | Polls every 2 s (±15% jitter) |
| Keep a computer/browser open | Yes | No, any small box will do |
| Can an agent drive it | No | **Yes** (`--json` + meaningful exit codes) |
| Double-charging risk | — | Intent journal + idempotent reconciliation; **never retries an ambiguous order** |
| Spending limits | Willpower | Hard guards: price cap / total budget / balance pre-check |

**In one sentence**: it turns "staring at the AutoDL page waiting for a card" into
a single command an agent can call in a loop.

---

## What 1.1.0 did: make it faster

Grabbing a GPU is a sprint: whoever gets from "a card appeared" to "the order
request reached AutoDL" first wins. 1.1.0 measured every leg of that path and
removed the waste.

### 1) Connection reuse (the big one)

The old client used `urllib`, which performs a **fresh TCP + TLS handshake for
every request**. It now keeps HTTP/1.1 connections alive (one per thread, all
closed on exit):

| 5 balance requests, same machine | #1 | #2 | #3 | #4 | #5 | Total |
| --- | --- | --- | --- | --- | --- | --- |
| 1.0 (reconnect each time) | 3183 ms | 2184 ms | 2299 ms | 3023 ms | 5227 ms | **15.9 s** |
| 1.1 (keep-alive) | 3087 ms | 440 ms | 430 ms | 440 ms | 440 ms | **4.8 s** |

> Measured on a machine where the TCP+TLS handshake to `api.autodl.com` takes
> ~1.9 s (an overseas route), so absolute numbers are high. A domestic
> connection usually handshakes in tens of milliseconds. But "one extra
> handshake per request" holds on any network — the first order sends six
> requests, i.e. six handshakes. Across repeated runs the totals stay between
> 14–16 s vs 4.8–5.3 s.

### 2) One network round-trip before ordering

"Check the balance" and "check whether this machine already hosts one of my
instances" are independent read-only requests. 1.0 sent them sequentially;
1.1 sends them concurrently.

Measured against the bundled fake AutoDL server with a 50 ms artificial delay
per response (simulating RTT):

| | One `grab` | TCP connections opened |
| --- | --- | --- |
| 1.0 | ≈450–510 ms | 6 |
| 1.1 | ≈360–370 ms | 3 |

Ten consecutive polling rounds (steady-state `watch`):

| | 10 rounds | TCP connections opened |
| --- | --- | --- |
| 1.0 | ≈600–650 ms | 10 |
| 1.1 | ≈520–550 ms | **1** |

Poll every 2 s for three hours (≈5400 rounds): the old client performs 5400
handshakes, the new one performs 1.

### 3) No more 12-second wait after ordering

1.0 confirmed a successful order with "up to 6 attempts × 2 seconds" — up to
12 seconds before returning. 1.1 trusts the instance UUID returned by the
server, does one lightweight check, and falls back to the instance list only
if that check fails: about 1 second worst case. `grab` is the command an agent
calls over and over, so this directly sets the loop rate.

### 4) Bugs fixed along the way

- **Exit codes 3/6 were wrong**: `grab`/`watch` reported auth and network
  failures as 7. They now follow the documented 3 / 6 / 7 contract.
- **`doctor` crashed on API errors**: it referenced `AutoDLError` without
  importing it.
- **`cooldown_seconds` was documented but never implemented**: it is honored now.
- **Notification spam**: in `watch` mode a low balance notified every round;
  identical notifications are now suppressed for five minutes.
- **An auth failure during ordering left an unresolved intent**: definite
  failures now write a result record too.
- Added `autodl-ac journal --ack` to clear the "ambiguous result" block after a
  manual check in the AutoDL console.

---

## Why no browser is needed

AutoDL's **official public API only covers balance lookup** — there is no
documented "check stock / place order" endpoint. But the AutoDL console is a web
app, and placing an order calls its own endpoints.

This tool calls the same endpoints with the **same developer token**:

```
Authorization: <your developer token>
```

Key observation (verifiable yourself): the AutoDL console front-end accepts
`?token=xxx` against `api.autodl.com`, which means the developer token and the
web session token are the same credential.

So: **a developer token is all you need** — no password, no captcha, no browser.

See [`docs/API.md`](docs/API.md) for the endpoint list and how it was reverse-engineered.

---

## Install

No dependencies, Python 3.9+:

```bash
git clone <this repo>
cd Autodl-AutoConnect

# Windows
python -m autodl_autoconnect --help

# or install as a command
pip install -e .
autodl-ac --help
```

You can also run it without installing: `python -m autodl_autoconnect`.

---

## Quick start (3 minutes)

### 1. Get a token

AutoDL console → Settings → Developer Token → copy.

### 2. Store it

```bash
autodl-ac login --token <your token>
```

On Windows the token is encrypted with **DPAPI** (only your user can decrypt it).
Other platforms fall back to base64 with `0600` permissions and tell you to use
the `AUTODL_TOKEN` environment variable instead.

### 3. Self-check

```bash
autodl-ac doctor
```

```
✔ Token           configured
✔ Balance API     ¥100.000
✔ Stock API       42 offers
✔ Unresolved order records  none
```

### 4. Look at stock once

```bash
autodl-ac stock --regions westDC2
```

### 5. Grab

```bash
# Notify only, do not order (the default, costs nothing)
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2,beijingDC2

# Actually order (real money)
autodl-ac watch --gpu-models "RTX 4090" --regions westDC2,beijingDC2 --yes
```

---

## For agents

Every command supports `--json` and returns machine-readable output. Exit codes
are meaningful:

| Exit | Meaning | What an agent should do |
| --- | --- | --- |
| 0 | Success (including a stock hit in notify-only mode) | Read `status` in the JSON |
| 2 | Configuration error | Fix the config |
| 3 | Authentication failure | Token expired; ask the user for a new one |
| 4 | No matching machine (also `watch` timeout) | Retry later |
| 5 | **Order result ambiguous** | **Stop and ask a human to check** (the tool stops too) |
| 6 | Network error | Retry later |
| 7 | Business error from the API | Inspect `error_code` / `message` |

The one-shot atomic command (ideal for an agent loop):

```bash
autodl-ac grab --gpu-models "RTX 4090" --regions westDC2 --yes --json
```

```json
{
  "ok": true,
  "matched": true,
  "ordered": true,
  "status": "confirmed",
  "message": "Grabbed: instance inst-xxxx",
  "order": {
    "instance_uuid": "inst-xxxx",
    "machine_id": "b7f3c9a2",
    "gpu_count": 1,
    "price_hourly": 2.5,
    "confirmed": true
  }
}
```

On failure an extra `error_code` (e.g. `AUTH_FAILED` / `NETWORK_ERROR` /
`NoStock`) explains the 3 / 6 / 7 exit codes:

```json
{
  "ok": false,
  "matched": false,
  "ordered": false,
  "status": "error",
  "message": "Stock query failed: network error: ...",
  "error_code": "NETWORK_ERROR"
}
```

A typical agent loop:

```python
import subprocess, json, time

while True:
    r = subprocess.run(
        ["autodl-ac", "grab", "--gpu-models", "RTX 4090",
         "--regions", "westDC2", "--yes", "--json"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        result = json.loads(r.stdout)
        if result["ordered"]:
            print("Got it:", result["order"]["instance_uuid"])
            break
    elif r.returncode == 5:
        print("Ambiguous result; check the AutoDL console manually"); break
    time.sleep(2)
```

---

## Configuration

The config file lives at `~/.autodl-autoconnect/config.json`.
Create it with `autodl-ac config init`, inspect it with `autodl-ac config show`.
Command-line flags override the file.

```json
{
  "gpu_models": ["RTX 4090", "RTX 4090D"],
  "gpu_count": 1,
  "regions": ["westDC2", "beijingDC2"],
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

| Field | Meaning |
| --- | --- |
| `gpu_models` | Wanted models. A shorthand like `"4090"` also matches `RTX 4090D` |
| `gpu_model_regex` | Advanced: regex over model names; mutually exclusive with `gpu_models` |
| `regions` | Regions as official codes (`westDC2`) or Chinese names (`西北B区`) |
| `image_uuid` | Official image UUID. **AutoDL usually rejects orders without it; set it explicitly** |
| `max_price_hourly` | Per-GPU price cap (CNY/hour) |
| `max_total_cost` | Total budget = price × GPUs × `expected_hours` |
| `balance_reserve` | Balance reserve; available balance must exceed estimated cost + reserve |
| `paid_consent` | **Defaults to `false`**: notify only. Set to `true` to place real orders |
| `poll_seconds` | Poll interval, default 2 s, jittered by ±15% |
| `cooldown_seconds` | Cooldown after a failed order (seconds), default 20 |
| `timeout_minutes` | Maximum watch time; 0 means unlimited |

---

## Safety design

This spends real money, so this layer is deliberately careful.

**1. No spending by default.** `paid_consent` is `false` and the command line
also needs `--yes`. Both locks must be open to place an order.

**2. Intent journal.** Before ordering, the intended action is appended (with
fsync) to `~/.autodl-autoconnect/journal.jsonl`; the result is appended after.

**3. Never retry an ambiguous order.** When the order request times out or the
connection drops, the tool cannot tell "not placed" from "placed but the reply
was lost". It does **not** retry. It switches to read-only reconciliation
(listing instances) and **stops all further ordering** until a human checks.
This is the single most important rule against double charging. After checking
the console, `autodl-ac journal --ack` clears the block.

**4. Idempotent.** Before ordering, it checks whether this machine already hosts
one of your instances and skips if so.

**5. If in doubt, don't buy.** Unknown price, machine offline, machine full,
insufficient balance, over budget — any failed check means no order, and the
reason is logged.

**6. No credential leaks.** Tokens never enter logs or exception messages
(redacted centrally); DPAPI on Windows; the server only echoes necessary fields.

**7. Everything stays local.** No third-party servers; notifications go straight
to the endpoints you configure.

See [`docs/SECURITY.md`](docs/SECURITY.md) for details.

---

## Notifications

| Channel | Configuration |
| --- | --- |
| Terminal banner + beep (Windows) | On by default; disable with `--no-desktop` |
| Webhook | `notify.webhook_url`, POSTs JSON, optional `Bearer` auth |
| ServerChan (WeChat push) | `notify.serverchan_key` |
| AutoDL WeChat message | `notify.autodl_wechat_token` (AutoDL console → Settings → WeChat messages) |

Webhook body (a format defined by this project):

```json
{
  "source": "autodl-autoconnect",
  "kind": "order_created",
  "headline": "AutoDL grab succeeded",
  "label": "Autodl-AutoConnect",
  "message": "instance inst-xxxx\nmachine b7f3c9a2\nGPUs 1\nprice 2.5 CNY/h",
  "sent_at": "2026-11-02T162920+0800"
}
```

Headers include `X-Event-Id` (idempotency) and optionally
`Authorization: Bearer <webhook_token>`.

`kind` is one of `stock_found`, `order_created`, `balance_low`, `ambiguous`.
Identical notifications are sent at most once per five minutes (so a low
balance does not spam every polling round).

---

## Commands

| Command | Purpose |
| --- | --- |
| `login` | Save and verify a token |
| `doctor` | Self-check: token, APIs, journal state |
| `balance` | Show balance |
| `stock` | Query stock once |
| `grab` | Check once and order immediately (atomic, best for agents) |
| `watch` | Keep polling and order |
| `instances` | List my instances |
| `power on/off/release <uuid>` | Power on/off or release an instance |
| `journal` | Show the intent journal |
| `journal --ack` | Close unresolved intents after a manual check |
| `config init/show/path` | Configuration management |

---

## Tests

Tests run against a **local fake AutoDL server** (`tests/fake_autodl.py`) that
mirrors the real paths, request bodies and envelope format, so every test is a
real HTTP round-trip: no AutoDL login, no GPU rental, no money spent. The fake
server speaks HTTP/1.1 with keep-alive, which also proves the client really
reuses connections.

```bash
python -m unittest discover -s tests -t . -v
```

```
Ran 78 tests in 11.2s

OK
```

---

## Known limits

- **A developer token is required**; password login is not supported (no
  captcha solving, no password flow).
- **Set `image_uuid` explicitly**: AutoDL usually rejects orders without it.
- The console endpoints are **undocumented** and may change. They all live in
  `ENDPOINTS` in `autodl_autoconnect/api.py`, and the parsing layer accepts
  multiple candidate field names, so a rename rarely requires code changes.
- **Behind a system/environment proxy**, the client falls back to `urllib` to
  preserve proxy, certificate and `no_proxy` behavior; the cost is one handshake
  per request. Direct connections are unaffected.
- This project is not affiliated with AutoDL. Check AutoDL's terms of service
  before use.

---

## Relationship to the reference project

There is a good open-source project in the same space:
[Stop-Manually-Refreshing-AutoDL-GPU](https://github.com/cc2025cheng/Stop-Manually-Refreshing-AutoDL-GPU)
(MIT, by cc2025cheng). This project **borrowed the problem definition and product
ideas** (waiting for stock, guards, intent journal, notifications) but **uses
none of its code**, and the implementation is entirely different:

| | Reference project | This project |
| --- | --- | --- |
| Stack | Node.js 22 + Playwright + local web console | Python 3.9+, **zero runtime dependencies** (stdlib only) |
| Login | Manual login in a dedicated browser, reuse cookies | **Developer token**, no browser/password/captcha |
| Driving | DOM clicks (fragile selectors, slow) | Direct JSON APIs (structured, scriptable) |
| Shape | GUI console for humans | **CLI + `--json`** for humans and agents |
| Deployment | Needs a graphical environment | Containers, small VMs, CI, headless servers |
| Grab action | Click "create and start" | `grab` atomic command: check + order + power on + confirm |
| Transport | Browser connection pool | stdlib `http.client` keep-alive (since 1.1.0) |
| Ambiguous result | Journal → reconcile → stop (idea borrowed) | Same idea, independent implementation: JSONL journal + **cross-process** blocking + machine-id idempotency |
| Token storage | Windows encryption | Windows **DPAPI**; elsewhere base64+0600 or env var |
| Clone to another card | Yes (drives the page) | Not implemented (endpoints located) |
| AI model selection | Yes (reads README/requirements) | Not implemented |
| Official stock API | Not used | Additionally uses `dev/machine/region/gpu_stock` |
| Tests | 334, Playwright + synthetic data | 78, real HTTP round-trips against a fake AutoDL server |

In short: **same problem, different code.** The name, webhook payload, config
format, journal format and exit-code semantics are all defined by this project.

### Not implemented yet

- **Same-region clone to another card**: endpoints located
  (`POST /api/v1/order/instance/clone/payg`,
  `GET /api/v1/instance/clone/get_clone_code`), not wired up.
- **AI model selection** (derive feasible GPUs from a project README).

---

## Changelog

### 1.1.0

- HTTP layer now reuses keep-alive connections (one per thread), removing one
  TCP+TLS handshake per request.
- Balance check and duplicate-order check now run concurrently.
- Order confirmation reduced from up to 12 s to about 1 s.
- Default poll interval 3 s → 2 s, with ±15% jitter.
- Fixed exit codes 3/6 being reported as 7.
- Fixed `doctor` raising `NameError` on API failures.
- Implemented `cooldown_seconds`; deduplicated notifications.
- Business/auth failures during ordering now write a result record instead of
  leaving an ambiguous intent.
- Added `journal --ack`.
- `--json` output gained `error_code`; candidate list capped at 20.
- Tests 69 → 78.

### 1.0.0

- Initial release.

---

## Credits

- [Stop-Manually-Refreshing-AutoDL-GPU](https://github.com/cc2025cheng/Stop-Manually-Refreshing-AutoDL-GPU) — an open-source answer to the same problem; the product ideas here were inspired by it.
- [AutoDL docs](https://www.autodl.com/docs/common_api/) — developer API and region codes.
- Not affiliated with AutoDL.

## License

[MIT](LICENSE)
