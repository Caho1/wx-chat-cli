# Security model: decryption isolation

The goal: **an agent on the network can read what has already been decrypted, and nothing else.**
It cannot read the key, cannot read the ciphertext, and therefore cannot decrypt — even if it
achieves code execution inside the serving process.

This is a kernel guarantee, not a policy flag.

```
wx serve ─ sandbox-exec -f reader.sb ─┬─ ~/.config/wechat-keys.json       ✗ Operation not permitted
                                      ├─ ~/Library/Containers/com.tencent.xinWeChat  ✗
                                      ├─ ~/Desktop/WeChat.app (debug copy)   ✗
                                      └─ ~/Library/Application Support/wechat-local-vault  ✓
```

SQLCipher needs *both* a per-database key and the encrypted `*.db` bytes. Neither is reachable from
inside the sandbox, so decryption is not a matter of permissions — it is impossible.

## Three layers

| Layer | Mechanism | What it stops |
| --- | --- | --- |
| **L1 kernel** | `reader.sb` denies read on the key file, the WeChat container and the debug copy | decryption (no key, no ciphertext) |
| **L2 code** | children run with `WX_READER=1`; `refresh` and `key` return `READER_FORBIDDEN` before argparse dispatch | any decryption code path, even if L1 were bypassed |
| **L3 server** | read-only allowlist, `--allow-write` for file output only, `CONFIRM_REQUIRED` for `--yes/--force/--capture` | accidental writes and confirm-required actions from a remote agent |

`--allow-write` **never** unlocks decryption. Fresh data requires a human on the host:

```bash
wx refresh          # host only
```

## The profile

Generated at `wx serve` start-up (mode 600):

```lisp
(version 1)
(allow default)
(deny file-read* (subpath "<home>/.config/wechat-keys.json"))
(deny file-read* (subpath "<home>/Library/Containers/com.tencent.xinWeChat"))
(deny file-read* (subpath "<home>/Desktop/WeChat.app"))
```

The container path is derived from `db_base_path` in `~/.config/wechat-local-vault.json`, so a
custom WeChat data location is covered automatically.

**Why a deny list rather than an allow list.** An allow list would have to enumerate the Python
runtime, dyld shared cache, `/usr/lib`, SQLite temp dirs and so on — brittle across OS updates, and
one missing rule silently breaks the tool. The deny list is small, auditable, and targets the two
artefacts that make decryption possible. The trade-off is explicit: it protects the *secret*
(the key) and the *ciphertext*, not the whole filesystem.

## Verify it yourself

```bash
wx security          # on the host
wxc security         # from any device on the tailnet
```

```json
{"checks": [
  {"check": "key file unreadable",   "ok": true, "detail": "Operation not permitted"},
  {"check": "encrypted DBs unreachable", "ok": true},
  {"check": "decrypted vault readable",  "ok": true},
  {"check": "token file 600",  "ok": true, "detail": "0o600"},
  {"check": "audit log 600",   "ok": true, "detail": "0o600"}],
 "passed": 5, "total": 5, "verdict": "isolation holds"}
```

Independent proof, no CLI involved:

```bash
# 1. key file
sandbox-exec -f ~/.config/wechat-local-vault/reader.sb /bin/sh -c 'head -c8 ~/.config/wechat-keys.json'
# → head: Operation not permitted

# 2. run the upstream decryptor inside the same sandbox
cd "$WX_SKILL_DIR"
/usr/bin/sandbox-exec -f ~/.config/wechat-local-vault/reader.sb \
  python3 scripts/decrypt_all_dbs.py --mode incremental
# → PermissionError: '.../wechat-keys.json'
```

## Command capability matrix

| Command | Host | Remote default | Remote `--allow-write` |
| --- | --- | --- | --- |
| `doctor` `describe` `security` `key` | ✅ | ✅ | ✅ |
| `chats` `find` `history` `search` `stats` `pull` | ✅ | ✅ | ✅ |
| `calls` `moments` `favorites` `scan` | ✅ | ✅ | ✅ |
| `export --inline` | ✅ | ✅ | ✅ |
| `export` (file) | ✅ | ❌ `WRITE_FORBIDDEN` | ✅ |
| `digest` `voice` | ✅ | ❌ `WRITE_FORBIDDEN` | ✅ |
| `refresh` (decrypt) | ✅ | ❌ `READER_FORBIDDEN` | ❌ never |
| `key --capture` | ✅ | ❌ `CONFIRM_REQUIRED` | ❌ never |

## Transport and credentials

- Bind address defaults to the Tailscale IPv4; `0.0.0.0` is refused (`UNSAFE_BIND`) unless
  `WX_ALLOW_LAN=1`, so the LAN cannot reach it.
- Everything travels inside the tailnet's WireGuard tunnel.
- Bearer token, 43 chars from `secrets.token_urlsafe(32)`, stored at
  `~/.config/wechat-local-vault/serve-token` (600), compared with `hmac.compare_digest`.
- No token ⇒ HTTP 401 and an audit entry.

## Audit log

`~/.config/wechat-local-vault/serve-audit.log` (600), one JSON line per request:

```json
{"ts":"…","peer":"100.x.y.z","peer_name":"laptop","command":"history","argc":6,"exit":0,"ms":382}
```

Deliberately records **no argument values** — chat names and search keywords would otherwise leak
into a file that people routinely paste into issues. If you need content-level auditing, add it
somewhere you control, and treat that log as sensitive as the vault.

## Residual risks

| Risk | Why it exists | Mitigation |
| --- | --- | --- |
| The decrypted vault is plaintext | it is the product | treat the token as a password; keep the tailnet trusted; kill the server when done |
| Deny list is path-based | WeChat could move its data | profile is regenerated from `db_base_path` on every `wx serve`; `wx security` checks the live path |
| Another local user on the host | the profile constrains only this process | token and audit log are 600, vault dir 700 |
| Token leakage | anyone with it can read everything | rotate by deleting the token file and restarting |
| `--no-sandbox` | someone will try it | prints a warning; L2/L3 still hold, L1 is lost |
| WAL lag | vault is a snapshot | `meta.vault_age_minutes`; human-triggered `wx refresh` |

## Threat model summary

Defended: a compromised or curious **remote agent** (arbitrary `argv`, arbitrary volume) reading
keys, ciphertext, or files outside the vault; triggering decryption; triggering confirm-required
actions; accidentally exfiltrating the crate of data in one oversized response.

Not defended: someone with **local root** on the host; someone holding the token and full tailnet
membership; a malicious build of Python itself; macOS sandbox escapes. If you need those covered,
the vault must live on an isolated machine and this CLI should be the only thing running there.
