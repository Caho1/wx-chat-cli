#!/usr/bin/env python3
"""wxc — 远程 `wx` 客户端（单文件，仅用标准库，可直接拷到别的设备）。

用法（不需要 venv、不需要 skill 目录、不需要 cd 到项目）：

    export WX_REMOTE=http://<tailscale-ip>:8765
    export WX_TOKEN=<从服务端 wx serve --token-show 复制>
    wxc doctor
    wxc find 小晚
    wxc history <username> --since 2026-11-01 --limit 50
    wxc describe

等价于在服务端跑同样的命令：参数原样转发（argv 透传），返回服务端的 JSON 封套，
退出码与服务端一致。错误一律输出到 stderr。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = float(os.environ.get("WX_TIMEOUT", "180"))


def token_from_file() -> str:
    path = Path(os.environ.get("WX_TOKEN_FILE") or "~/.config/wechat-local-vault/serve-token").expanduser()
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    remote = os.environ.get("WX_REMOTE", "").rstrip("/")
    token = os.environ.get("WX_TOKEN") or token_from_file()

    # 也支持 wxc --remote URL --token T <args>
    i = 0
    rest: list[str] = []
    while i < len(args):
        a = args[i]
        if a == "--remote" and i + 1 < len(args):
            remote = args[i + 1].rstrip("/")
            i += 2
            continue
        if a.startswith("--remote="):
            remote = a.split("=", 1)[1].rstrip("/")
            i += 1
            continue
        if a == "--token" and i + 1 < len(args):
            token = args[i + 1]
            i += 2
            continue
        if a.startswith("--token="):
            token = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1

    if not remote:
        print("wxc: 需要 WX_REMOTE 或 --remote <http://host:port>", file=sys.stderr)
        return 2
    if not token:
        print("wxc: 需要 WX_TOKEN / WX_TOKEN_FILE / --token", file=sys.stderr)
        return 2
    if not rest:
        print("wxc: 需要子命令，例如 wxc doctor / wxc describe", file=sys.stderr)
        return 2

    url = f"{remote}/v1/exec"
    body = json.dumps({"argv": rest}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "wxc/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"wxc: HTTP {exc.code} {raw[:400]}", file=sys.stderr)
            return 1
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "command": "wxc",
                          "error": {"code": "UNREACHABLE",
                                    "message": f"连不上 {remote}：{exc.reason}",
                                    "hints": ["确认服务端在跑：wx serve",
                                              "确认在同一 tailnet：tailscale status"]}},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 7
    except TimeoutError:
        print(json.dumps({"ok": False, "command": "wxc",
                          "error": {"code": "TIMEOUT", "message": f"超过 {TIMEOUT}s 未返回"}},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    if payload.get("ok"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    err = payload.get("error") or {}
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return {"BAD_ARGUMENT": 2, "CHAT_NOT_FOUND": 3, "CHAT_AMBIGUOUS": 4,
            "VAULT_MISSING": 5, "KEY_MISSING": 5, "DEPENDENCY_MISSING": 6,
            "UNREACHABLE": 7, "UNAUTHORIZED": 8, "WRITE_FORBIDDEN": 9,
            "CONFIRM_REQUIRED": 9, "READER_FORBIDDEN": 9,
            "BUSY": 10, "TIMEOUT": 11}.get(err.get("code", ""), 1)


if __name__ == "__main__":
    raise SystemExit(main())
