# netdev — Network Device Debugging Console

[简体中文](README.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) · [Code of Conduct](CODE_OF_CONDUCT.md) · [MIT License](LICENSE)

A macOS terminal for debugging network gear, built around one idea:
**the AI and the human must look at the same screen, through the same door.**

Serial console, SSH and Telnet all converge into a single CLI. That CLI is the
*only* way to touch a device — the AI has no side door. Every guardrail
(blacklist, human approval, forced backup, per-line verification) lives in that
one path, so the AI cannot route around it.

```
┌──────────┐   ┌──────────┐   ┌──────────┐
 │  Human   │   │ Web UI   │   │    AI    │   ← 3 front doors…
 └────┬─────┘   └────┬─────┘   └────┬─────┘
      │             │              │
      └─────────────┼──────────────┘
                    ▼
            ┌───────────────┐
            │  netdev CLI   │          ← …one door. All guardrails live here.
            └───────┬───────┘
                    ▼
        ┌───────────────────────────┐
        │ serial · SSH · Telnet     │
        │ (tmux: 人机同屏 / shared) │
        └───────────────────────────┘
```

## Why

Debugging network equipment means watching a screen that scrolls by, waiting for
a prompt, and being able to scroll back. Doing that through an AI wrapper usually
means the AI is *blind* — it sends a command, gets text back, and the human sees
nothing.

netdev puts both of you in the same tmux pane. The AI can read what's on screen
and type into it; you watch it happen and can take over at any moment.

## Features

- **Three ways in** — Serial Console / SSH / Telnet
- **Shared screen** — tmux-backed; human and AI read and write the same pane, with scrollback
- **Four gates on every write** — blacklist → human approval → forced backup → per-line push with verification
- **Fail-closed** — if the approver can't be reached, the write is *refused*, not allowed
- **Vendor auto-detection** — sends `display version`, recognizes Huawei / H3C / Ruijie / Cisco / Maipu from the banner. You don't fill in a platform code
- **Metrics that admit ignorance** — an unparsed metric shows `—`. It never invents a `0`
- **Learn once, use forever** — a metric command the device rejects is probed once, then cached to `config/cmd-cache.json`
- **AI over a direct connection** — talks straight to any OpenAI-compatible API (DeepSeek / OpenAI / OpenRouter / your own gateway). One API key, no extra CLI, no background process. AI can also be switched off entirely
- **Snapshots** — semantic diff (not raw line-by-line), restore-after-review, recycle bin for deletes
- **Self-check** — `./netdev doctor` tells you what's wrong on this machine
- **A web UI you can start with one command** — `netdev ui`

## Requirements

- **macOS**. This uses `/dev/cu.*` device names, `osascript`, `security`, and tmux.
- Python **3.13**
- `tmux` — `brew install tmux`
- A USB-Console adapter (FTDI / CH340 / CP210x) for serial, if you use serial

## Install

### From source

```bash
git clone <this-repo> netops && cd netops
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./netdev doctor
```

### Build a self-contained bundle

The source repo does **not** contain `payload/` or `runtime/`. To produce an
offline `.tar.gz` for another Mac:

```bash
bash dist/build_bundle.sh --with-python
# → ~/Desktop/workbuddy/<date>_netdev设备工具台_macOS_<arch>_安装包.tar.gz

tar -xzf <that file> && cd <extracted>
bash install.sh --dry-run     # preview
bash install.sh               # install
```

## Quick start

```bash
./netdev doctor                     # what's wrong / what's missing
./netdev list                       # your devices
./netdev shell <device>             # interactive session
./netdev run <device> "display version"      # read-only
./netdev apply <device> --cmd "..."          # write (goes through all four gates)
./netdev snap save <device> --tag before-change
./netdev snap diff <device>         # semantic diff
./netdev ui open                    # start the web UI (background, survives closing the terminal)
```

### The web service

```bash
./netdev ui          # ensure it's running (starts it if not; reports if it is) — idempotent
./netdev ui status   # in run = exit 0, down = exit 1
./netdev ui restart
./netdev ui log -n 40
```

It runs as a proper daemon (double-fork + `setsid`), so closing your terminal —
or the double-clickable launcher window — does not take it down.

## Using it with an AI

The built-in assistant has **exactly one backend: a direct OpenAI-compatible API**.
No AI CLI to install, no daemon, no credential file to babysit — just a key.

Configure it in **Settings → "Direct API key"** (saved to `config/direct.json`,
mode 600, gitignored), or via `NETDEV_DIRECT_API_KEY` + `NETDEV_DIRECT_BASE_URL`.
The badge in the top bar then reads **AI: direct**.

If you'd rather drive it from your own agent, point any MCP-capable agent at the
bundled server:

```bash
./netdev-mcp        # stdio / JSON-RPC MCP server
```

It exposes **13 `netdev_*` tools**, every one of which shells out to the same CLI
a human would use. Generic tools like Bash / Write / Edit are **not** exposed.
The built-in assistant uses that exact same 13-tool set, so write operations go
through the same four guardrails either way.

To let an agent read the shared conventions:

```bash
cp config/AGENTS.workspace.md.example config/AGENTS.workspace.md   # then edit
# or symlink it as the agent's global instruction file:
ln -s "$PWD/config/AGENTS.workspace.md.example" ~/.pi/agent/AGENTS.md
```

## Tests

Everything below runs **offline** against a built-in simulator — no real device,
no network, no credentials. That's deliberate: a project you can't test without
hardware won't get contributions.

```bash
./netdev selftest                                          # end-to-end, against the simulator
./.venv/bin/python tests/test_ai_toolchain_and_cache.py    # 57 checks
./.venv/bin/python tests/test_approval_gates.py            # 19 checks (security-critical)
./.venv/bin/python tests/test_ui_lifecycle.py              # 27 checks
```

CI runs all four on every push.

`test_approval_gates.py` guards the one place that decides whether the AI may
modify a device. Its most important assertion: **even with the self-test bypass
switch on, a real device is still never exempt.** The bypass exists only so
`selftest` can run on a GUI-less CI runner, and only for devices explicitly marked
`sim = true` on a loopback address.

## Security

The service binds `127.0.0.1` and has **no authentication** — do not rebind it
to `0.0.0.0`. See [SECURITY.md](SECURITY.md) for the full trust boundary.

## Platform support

**macOS only** for now. The architecture is portable (the core is pure Python),
but `/dev/cu.*`, `osascript`, and the serial bridge are not.

## License

[MIT](LICENSE)
