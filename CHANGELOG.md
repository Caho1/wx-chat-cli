# Changelog

## 1.0.0

First public release.

- Agent-first CLI: 19 subcommands, single JSON envelope, exit codes mirroring `error.code`
- `find` returns candidates instead of guessing; `search` slices by time and reports `partial`
- Canned integrations: `pull` (all chats in a window), `calls` (duration/outcome/initiator),
  `scan` (one message type vs total volume), `voice` (SILK → MP3/WAV + manifest for external ASR)
- Tailscale bridge: `wx serve` + single-file client `wxc.py`, raw HTTP for any language
- Decryption isolation: `sandbox-exec` profile denying the key file and the encrypted container,
  reader-mode code guard, read-only allowlist
- `wx security`: machine-verifiable audit of the isolation guarantee
- Per-request subprocess execution (threads shared the GIL: 4 concurrent calls went 32 s → 0.55 s)
