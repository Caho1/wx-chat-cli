# Remote access over Tailscale

Expose the CLI to agents on other devices without giving them the vault, the keys, or a way to
decrypt anything.

```
device A (agent)  ──tailnet, WireGuard──  http://<host>:8765  ──  wx serve (host, sandboxed)
   wxc / curl                              POST /v1/exec              │
                                                                      └─ wx <argv> (child, WX_READER=1)
                                                                              └─ decrypted vault (read-only)
```

Each request spawns one `wx` child process. The server does no data shaping, so remote and local
semantics are identical: same envelope, same error codes, same ambiguity handling, same slicing.

## Start the server

```bash
wx serve                    # binds $(tailscale ip -4):8765, read-only
wx serve --token-show       # prints token + export block
wx serve --port 8765 --max-workers 4 --max-limit 2000 --timeout 300
wx serve --allow-write      # additionally permit export/digest/voice to write files
wx serve --no-sandbox       # discouraged: the process could then read the key file
```

Startup output is JSON:

```json
{"ok": true, "command": "serve",
 "data": {"listen": "http://100.x.y.z:8765", "read_only": true,
          "max_limit": 2000, "max_workers": 4, "timeout": 300, "sandbox": true,
          "token_file": "~/.config/wechat-local-vault/serve-token",
          "audit_file": "~/.config/wechat-local-vault/serve-audit.log",
          "endpoints": {"health": "…", "describe": "…", "exec": "POST … {\"argv\": [...]}"}}}
```

Refuses to bind `0.0.0.0` (LAN exposure) unless `WX_ALLOW_LAN=1`.

## Client

`wxc.py` is a single file using only the standard library. Copy it anywhere:

```bash
scp wxc.py user@<device>:/usr/local/bin/wxc.py
chmod +x /usr/local/bin/wxc.py && ln -sf /usr/local/bin/wxc.py /usr/local/bin/wxc

export WX_REMOTE=http://<host>:8765
export WX_TOKEN=<token>          # or WX_TOKEN_FILE=/path/to/token
wxc history <username> --since 2026-01-01 --limit 50
```

`wxc --remote URL --token T <command>` also works. Envelope is printed verbatim; exit codes mirror
the server's:

| Exit | `error.code` | Meaning |
| --- | --- | --- |
| 0 | — | ok |
| 2 / 3 / 4 | `BAD_ARGUMENT` / `CHAT_NOT_FOUND` / `CHAT_AMBIGUOUS` | same as local |
| 5 / 6 | `VAULT_MISSING` / `KEY_MISSING` / `DEPENDENCY_MISSING` | server-side data problem |
| 7 | `UNREACHABLE` | server down, or not on the same tailnet |
| 8 | `UNAUTHORIZED` | token missing/wrong |
| 9 | `WRITE_FORBIDDEN` / `CONFIRM_REQUIRED` / `READER_FORBIDDEN` | refused by policy |
| 10 | `BUSY` | concurrency cap reached |
| 11 | `TIMEOUT` | exceeded `WX_TIMEOUT` (default 180 s) |

`wx --remote <url> --token <t> <command>` produces the same call without `wxc`.
`WX_REMOTE` is ignored by `wx serve` itself (it always runs locally).

## Protocol

| Endpoint | Method | Body | Response |
| --- | --- | --- | --- |
| `/v1/health` | GET | — | `version, vault, decrypted_at, vault_age_minutes, read_only, max_limit, max_workers, commands[]` |
| `/v1/describe` | GET | — | same payload as `wx describe` |
| `/v1/exec` | POST | `{"argv": ["history", "<chat>", "--since", "2026-01-01"]}` | same envelope as the local CLI |

Auth header: `Authorization: Bearer <token>` or `X-WX-Token: <token>`.
Request cap 1 MB, response cap 16 MB (`RESPONSE_TOO_LARGE`, HTTP 413).

Because `argv` is passed straight to the CLI parser, **any** local subcommand works remotely —
subject to the policy table below. That is intentional: one contract, two transports.

## Policy

| Command | Local | Remote (default) | Remote + `--allow-write` |
| --- | --- | --- | --- |
| `doctor` / `describe` / `security` / `key`* | ✅ | ✅ | ✅ |
| `chats` / `find` / `history` / `search` / `stats` / `pull` | ✅ | ✅ | ✅ |
| `calls` / `moments` / `favorites` / `scan` | ✅ | ✅ | ✅ |
| `export --inline` | ✅ | ✅ | ✅ |
| `export` (to file) | ✅ | ❌ `WRITE_FORBIDDEN` | ✅ |
| `digest` / `voice` | ✅ | ❌ | ✅ |
| `refresh` (decrypt) | ✅ | ❌ `READER_FORBIDDEN` | ❌ **never** |
| `key --capture` | ✅ | ❌ `CONFIRM_REQUIRED` | ❌ **never** |

\* `wx key` is allowed remotely but reads nothing sensitive inside the sandbox — it reports
`usable/unusable` from the decrypted side only when the key file is readable locally.

Arguments are clamped server-side: `--limit`, `--limit-per-chat`, `--per-slice`, `--max-scan`
are capped at `--max-limit` (default 2000). `--yes`, `--force`, `--capture` are refused with
`CONFIRM_REQUIRED` rather than silently stripped, so an agent cannot believe it succeeded.

## Operations

```bash
# background
(nohup wx serve > ~/Library/Logs/wx-serve.log 2>&1 &)

# confirm binding
lsof -nP -iTCP:8765 -sTCP:LISTEN

# who queried what (metadata only — never message content)
tail -20 ~/.config/wechat-local-vault/serve-audit.log

# stop
lsof -ti tcp:8765 | xargs kill
```

Audit line:

```json
{"ts":"2026-01-01T12:00:00","peer":"100.x.y.z","peer_name":"laptop","command":"history","argc":6,"exit":0,"ms":382}
```

`argc` is recorded, argument **values** are not — so keywords and chat names never reach the log.
Peer names are resolved via `tailscale status` and cached for 60 s.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `UNREACHABLE` / connection refused | server not running, or bound to the Tailscale IP while you called `127.0.0.1` | read `data.listen` from startup output and use that address |
| 401 `UNAUTHORIZED` | token mismatch | `wx serve --token-show`, re-export |
| `BUSY` | concurrency cap | raise `--max-workers`, or serialize calls |
| `RESPONSE_TOO_LARGE` | too much in one page | lower `--limit`, narrow `--since/--until` |
| `INTERNAL` with `detail` from a child | child produced no JSON | run the same argv locally to reproduce |
| `UNSAFE_BIND` | asked for `0.0.0.0` | bind the Tailscale IP, or set `WX_ALLOW_LAN=1` deliberately |
| `SANDBOX_UNAVAILABLE` | `/usr/bin/sandbox-exec` missing | non-macOS host: pass `--no-sandbox` knowingly, or use SSH instead |

## Concurrency note

The server runs each request in a **subprocess** rather than a thread. Threads share the GIL, which
made four concurrent `stats` calls take 32 s versus 0.4 s for one; separate processes brought four
concurrent calls to ~0.55 s total. It also isolates crashes and guarantees the child sees exactly
the same code path as a local invocation.
