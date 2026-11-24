# wx-chat-cli

An **agent-first CLI** over your locally decrypted WeChat (macOS) vault: one command, one JSON
envelope, predictable exit codes — plus an optional Tailscale HTTP bridge with a kernel-level
sandbox that makes **remote decryption physically impossible**.

> Reads **your own** data on **your own** machine. No upload, no third-party API, no telemetry.
>
> **Upstream dependency:** the decryption backend is 逸尘 (Yichen)'s
> [`yichen-wechat-local-vault`](https://github.com/mcncarl/yichen-skills/tree/main/yichen-wechat-local-vault),
> installed separately. This repo is the reader layer on top of it and ships **no**
> decryption or key-extraction code — see [Backend](#backend).

```
Agent ──▶ wx <command>  ──▶  JSON {ok, command, data, meta}
                            └─▶ vault/*.db (already decrypted, read-only)
```

## Why another CLI

Most WeChat export tools are interactive scripts. Agents need:

| Requirement | How this repo answers it |
| --- | --- |
| Parseable output | Single envelope `{ok, command, data, meta}`; `meta` always carries `decrypted_at` + `vault_age_minutes` |
| Branchable failures | `error.code` + matching process exit code (`2/3/4/5/6/9`) — never parse Chinese error strings |
| No silent wrong answers | Ambiguous names return `CHAT_AMBIGUOUS` with candidates instead of picking the first fuzzy hit |
| No silent data loss | `search` slices by time and reports `partial: true` when the scan cap is hit |
| Self-discovery | `wx describe` returns a machine-readable manifest of every command |
| Remote access without trust | Read-only HTTP bridge; the serving process cannot read keys or encrypted DBs |

## Install

```bash
git clone https://github.com/<you>/wx-chat-cli.git
cd wx-chat-cli

# Point at your backend (see below) — either export once or let auto-discovery find it
export WX_SKILL_DIR="$HOME/code/yichen-skills/yichen-wechat-local-vault"

# Optional: a venv with pycryptodome (needed by `wx key`), zstandard (compressed messages),
# pilk + ffmpeg (only for `wx voice`)
python3 -m venv .venv && .venv/bin/pip install pycryptodome zstandard
export WX_PYTHON="$PWD/.venv/bin/python"     # launcher uses this for `wx refresh` too

ln -sf "$PWD/bin/wx"  ~/.local/bin/wx
ln -sf "$PWD/bin/wxc" ~/.local/bin/wxc

wx doctor
```

Requirements: macOS (uses `sandbox-exec` for the remote bridge), Python ≥ 3.9.
Linux runs the read path fine; the sandboxed bridge is macOS-only by design.

<a id="backend"></a>

## Backend: 逸尘 (Yichen)'s yichen-wechat-local-vault

This CLI is a **reader**. All key handling and decryption live in the upstream project:

| | |
| --- | --- |
| Author | **逸尘 (Yichen)** — WeChat `yichen365ai` |
| Repo | [`mcncarl/yichen-skills`](https://github.com/mcncarl/yichen-skills) |
| Skill | [`yichen-wechat-local-vault`](https://github.com/mcncarl/yichen-skills/tree/main/yichen-wechat-local-vault) |
| Licence | [Personal learning / non-commercial](https://github.com/mcncarl/yichen-skills/blob/main/LICENSE) — **not** MIT |

Install it first, then point this CLI at it:

```bash
git clone https://github.com/mcncarl/yichen-skills.git ~/code/yichen-skills
export WX_SKILL_DIR="$HOME/code/yichen-skills/yichen-wechat-local-vault"
```

What it must provide:

| Need | Path |
| --- | --- |
| Query backend | `<WX_SKILL_DIR>/scripts/vault_cli.py` (required) |
| Decrypt entry | `<WX_SKILL_DIR>/scripts/decrypt_all_dbs.py` (required for `wx refresh`) |
| Decrypted vault | `~/Library/Application Support/wechat-local-vault/decrypted/current` |
| Path config | `~/.config/wechat-local-vault.json` (`wxid`, `db_base_path`, `decrypted_dir`, …) |
| Keys | `~/.config/wechat-keys.json` — produced by the upstream tooling, read only by `wx key` on the host |

### Why nothing is bundled

The upstream licence forbids redistributing the project **or derivative works**. So this repo
contains no hooking code, no SQLCipher body decryption, and no copy of upstream files — only a
generic page-0 validity check in `wx key` (decrypt 4096 bytes, assert the SQLite page size).

Practical consequences:

- `wx refresh`, `wx key --capture` and first-time setup are **upstream's job**, not this repo's.
- `wx key --capture` will explain this and exit `DEPENDENCY_MISSING` unless you drop your own
  `grab_keys.py` next to `wx.py`.
- If you need a self-contained decryptor, ask Yichen for written permission first.

### Known upstream issue on recent macOS

If `extract_keys.py` reports *0 PBKDF2 calls*, the hook never installed: on newer macOS
`CCKeyDerivationPBKDF` lives in a dyld shared-cache library whose export table is not returned by
`Module.enumerateExports()`. Resolve it through the global export table instead —
[PR #33](https://github.com/mcncarl/yichen-skills/pull/33) describes the one-function change.
Also note `frida` ≥ 18 removed `Module.getGlobalExportByName`; pin `frida<18` if you rely on it.
See [docs/BACKEND.md](docs/BACKEND.md) for details.

## Commands (19)

```bash
wx doctor                     # self-check: vault, keys, shards, freshness
wx describe [--verify]        # machine-readable capability manifest
wx key                        # validate stored keys (usable / stale / missing source)
wx security                   # audit the decryption-isolation guarantee
wx refresh [--full]           # LOCAL ONLY — decrypt incrementally / fully

wx chats [--unread] [--limit N]
wx find <query>               # name → username, with candidates (never guesses)
wx history <chat> [--since --until --hours --type --limit --offset --media]
wx search <keyword> [--chat ...] [--type --since --until --slice-days]
wx stats <chat> [--since --until --hours]
wx export <chat> [--doc-format markdown|txt] [--out] [--inline] [--limit]
wx pull [--hours 24 | --since --until] [--limit-per-chat] [--save]

wx calls <chat> [--since --duration-min N]     # duration / outcome / initiator
wx voice <chat>|--all-chats [--since --dry-run] [--audio-format mp3|wav]   # for external ASR
wx moments --name <person> | --username <id>
wx favorites [--type text|image|article|card|video] [--query]
wx scan --type <type> [--by year|month|day]  |  wx scan --find-tables <word>

wx serve [--port N] [--allow-write] [--token-show] [--no-sandbox]   # Tailscale bridge
```

## Output contract

Success:

```json
{"ok": true, "command": "history",
 "data": {"chat": "…", "count": 2, "messages": [{"time": "…", "sender": "…", "content": "…"}]},
 "meta": {"vault": "…", "decrypted_at": "2026-01-01T00:00:00",
          "vault_age_minutes": 3.5, "wechat_running": true, "took_ms": 412}}
```

Failure (stderr, still JSON):

```json
{"ok": false, "command": "history",
 "error": {"code": "CHAT_AMBIGUOUS", "message": "…",
           "hints": ["…"], "candidates": [{"display_name": "…", "username": "…"}]}}
```

| Exit | `error.code` | Action |
| --- | --- | --- |
| 0 | — | read `data` |
| 2 | `BAD_ARGUMENT` | read `error.hints` |
| 3 | `CHAT_NOT_FOUND` | `wx find <shorter query>` |
| 4 | `CHAT_AMBIGUOUS` | pick a `candidates[].username` |
| 5 | `VAULT_MISSING` / `KEY_MISSING` | `wx refresh --full` / `wx key` |
| 6 | `DEPENDENCY_MISSING` | install deps / set `WX_PYTHON` |
| 9 | `WRITE_FORBIDDEN` / `READER_FORBIDDEN` | remote read-only, or decryption — do it locally |

`--format text` renders for humans; `--compact` prints single-line JSON.

## Remote access (Tailscale)

Agent on another device, no venv, no clone — just `wxc.py`:

```bash
# host
wx serve                    # binds the Tailscale IPv4:8765, read-only
wx serve --token-show       # prints token + a copy-paste export block

# other device
scp wx-chat-cli/wxc.py user@<host>:/usr/local/bin/wxc.py && chmod +x /usr/local/bin/wxc.py
export WX_REMOTE=http://<tailscale-ip>:8765
export WX_TOKEN=<token>
wxc doctor && wxc describe
wxc history <username> --since 2026-01-01
wxc export <username> --limit 200 --inline
```

Raw HTTP works too — any language, no client needed:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"argv":["search","keyword","--since","2026-01-01"]}' \
  http://<tailscale-ip>:8765/v1/exec
```

Endpoints: `GET /v1/health`, `GET /v1/describe`, `POST /v1/exec {"argv": [...]}`.
Details: [docs/REMOTE.md](docs/REMOTE.md).

## Security model

Decryption stays local — enforced by the kernel, not by an `if`:

```
wx serve ─ sandbox-exec + reader.sb ─┬─ ~/.config/wechat-keys.json   ✗ unreadable
                                     ├─ WeChat container (encrypted)  ✗ unreadable
                                     └─ decrypted vault               ✓ readable
```

| Layer | Mechanism | Guarantee |
| --- | --- | --- |
| L1 kernel | `reader.sb` denies key file + container + debug copy | no key and no ciphertext ⇒ decryption impossible |
| L2 code | child processes run with `WX_READER=1`; `refresh`/`key` return `READER_FORBIDDEN` | no decryption code path reachable |
| L3 server | read-only allowlist; `--allow-write` only permits **writing files**; `--yes/--force/--capture` refused remotely | no remote trigger of destructive/confirm-required actions |

`--allow-write` never unlocks decryption. To get fresh data, a human runs `wx refresh` on the host.

Verify any time:

```bash
wx security      # host  : 5/5 checks
wxc security     # remote: let the other agent verify itself
```

Manual proof:

```bash
sandbox-exec -f ~/.config/wechat-local-vault/reader.sb /bin/sh -c 'head -c8 ~/.config/wechat-keys.json'
# → Operation not permitted
```

See [docs/SECURITY.md](docs/SECURITY.md) for the full matrix and residual risks.

## Canned scripts

These wrap the same backend and are also reachable through `wx`:

| Script | Purpose |
| --- | --- |
| `tools/latest_messages.py` | all chats within a window, grouped (`wx pull`) |
| `tools/call_log.py` | call records: time / duration / outcome / initiator (`wx calls`) |
| `tools/scan_types.py` | one message type across all chats **plus total volume** — proves "missing data" vs "never recorded" (`wx scan`) |
| `tools/export_voice.py` | SILK → MP3/WAV + `manifest.jsonl` for external ASR (`wx voice`) |

## Known behaviours worth knowing

- **Snapshot, not a stream.** WeChat writes to WAL; the vault only reflects checkpointed pages.
  Expect minutes of lag, and check `meta.vault_age_minutes`.
- **`search` needs a time window** for correctness. It slices by `--slice-days` (default 7) and
  reports `partial: true` at the scan cap.
- **`--media` needs the encrypted container**, so it is downgraded on the sandboxed server
  (`meta.media_hints_disabled`).
- **`Name2Id` rowids are per-shard.** Never join sender ids across shards — `wx calls` builds the
  mapping per database file.
- **Newsletters have no body.** Official-account posts never enter `Msg_*` tables; only session
  summaries exist.

## Licence

MIT for the code in this repo. The backend is separately licensed (non-commercial) — see above.
No decryption or key-extraction code is included here, by design.

## Disclaimer

Personal data tooling. Use it only on accounts and machines you own, and respect local law and
the WeChat terms of service. The authors are not responsible for misuse.
