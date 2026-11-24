# Backend contract

`wx-chat-cli` is a reader. Everything that touches keys or ciphertext lives in the backend you
install separately.

## What the CLI expects

| Need | Path / setting |
| --- | --- |
| Backend module | `<WX_SKILL_DIR>/scripts/vault_cli.py` (required) |
| Decrypt entry | `<WX_SKILL_DIR>/scripts/decrypt_all_dbs.py` (required for `wx refresh`) |
| Decrypted vault | `~/Library/Application Support/wechat-local-vault/decrypted/current`, overridable via `decrypted_dir` |
| Path config | `~/.config/wechat-local-vault.json` → `wxid`, `db_base_path`, `vault_dir`, `decrypted_dir`, `exports_dir` |
| Keys (host only) | `~/.config/wechat-keys.json` — read by `wx key`, never by the sandboxed server |

`WX_SKILL_DIR` may be omitted if the backend sits in one of the auto-discovered locations
(`./yichen-skills/yichen-wechat-local-vault`, `../yichen-wechat-local-vault`, `~/yichen-wechat-local-vault`, …).
Run `wx doctor` and inspect `skill_dir` to confirm what was picked.

## Why no decryption code here

The backend's licence forbids redistribution of the project or derivative works. Rather than
vendor it, this repo depends on your installation and ships zero cryptography:

- no key extraction / hooking code
- no SQLCipher page decryption for message bodies
- the only crypto is a **page-0 validity check** in `wx key` (decrypt the first 4096-byte page and
  assert the SQLite page size), which is generic SQLCipher behaviour, not backend code.

## If `extract_keys.py` reports 0 PBKDF2 calls

On recent macOS the symbol lives in a dyld shared-cache library whose export table is not returned
by `Module.enumerateExports()`, so the hook never installs. Resolve it through the global export
table instead:

```js
const resolve = Module.getGlobalExportByName || Module.findGlobalExportByName;
const addr = resolve('CCKeyDerivationPBKDF');
```

Upstream reference: [mcncarl/yichen-skills#33](https://github.com/mcncarl/yichen-skills/pull/33).

Two practical notes from real use:

1. Hooks must be installed **before** the signed copy logs in — the keys are derived at login.
2. `frida` ≥ 18 removed `Module.getGlobalExportByName`; pin `frida<18` if you rely on it.

## Health checks

```bash
wx doctor            # vault_ok, keys.readable, message_shards, vault_age_minutes
wx key               # per-database page-0 validation
wx security          # sandbox isolation audit
```

`doctor` reports `keys.readable: false` inside the sandboxed server — that is expected, and means
the isolation is working, not that keys are missing.
