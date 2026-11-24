#!/usr/bin/env python3
"""wx serve — 通过 Tailscale 把 `wx` 暴露给其他设备的 Agent（HTTP，只读优先）。

协议（全部 JSON）：
  GET  /v1/health                → {ok, version, vault, decrypted_at, commands, read_only}
  GET  /v1/describe              → 等价 `wx describe`
  POST /v1/exec  {"argv": [...]} → 与本地 CLI 完全一致的 {ok, command, data, error, meta}

鉴权：
  Authorization: Bearer <token>   或   X-WX-Token: <token>
  token 存在 ~/.config/wechat-local-vault/serve-token（600），首次启动自动生成。

安全默认：
  - 只绑定 Tailscale IPv4 或 127.0.0.1；拒绝 0.0.0.0，除非显式 --allow-lan
  - 只读白名单；写命令（refresh / export / digest / voice）需要 --allow-write
  - --limit 类参数被 --max-limit 截断，避免一次拉爆
  - 审计日志只记命令名与耗时，不记消息内容
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_DIR = Path("~/.config/wechat-local-vault").expanduser()
TOKEN_FILE = CONFIG_DIR / "serve-token"
AUDIT_FILE = CONFIG_DIR / "serve-audit.log"
PROFILE_FILE = CONFIG_DIR / "reader.sb"
SERVICE_LABEL = "com.wxchatcli.serve"
SERVICE_PLIST = Path("~/Library/LaunchAgents/com.wxchatcli.serve.plist").expanduser()
SERVICE_LOG_DIR = Path("~/Library/Logs").expanduser()
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MAX_BODY = 1 << 20          # 1 MB 请求体
MAX_RESPONSE = 16 << 20     # 16 MB 响应
LIMIT_KEYS = {"--limit", "--limit-per-chat", "--per-slice", "--max-scan", "--duration-minute"}

READ_ONLY = {"doctor", "describe", "chats", "find", "history", "search", "stats",
             "pull", "calls", "moments", "favorites", "scan", "key", "security"}
WRITE = {"refresh", "export", "digest", "voice"}


def ensure_token() -> tuple[str, bool]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token, False
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token, True


def tailscale_ipv4() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        return ip or None
    except Exception:
        return None


_WHO_CACHE: dict[str, tuple[float, str]] = {}
_WHO_TTL = 60.0


def tailscale_whoami(host: str) -> str:
    """反查对端主机名；带 60s 缓存，避免每个请求都 spawn 一次 tailscale。"""
    if host in ("127.0.0.1", "::1", "localhost"):
        return "localhost"
    now = time.time()
    cached = _WHO_CACHE.get(host)
    if cached and now - cached[0] < _WHO_TTL:
        return cached[1]
    name = _tailscale_lookup(host)
    _WHO_CACHE[host] = (now, name)
    return name


def _tailscale_lookup(host: str) -> str:
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "{}")
        for peer in (data.get("Peer") or {}).values():
            for ip in peer.get("TailscaleIPs") or []:
                if ip == host:
                    return peer.get("HostName") or peer.get("DNSName", "").split(".")[0] or ""
    except Exception:
        pass
    return ""


def build_reader_profile() -> tuple[Path, list[str]]:
    """生成 reader 沙箱 profile：禁读密钥与加密源库，因此**物理上无法解密**。

    用 deny 列表而非 allow 列表：macOS 上 allow-list 会连 python 运行时一起挡住，
    deny-list 更实用，且把两个「解密可必需」的入口全部拔掉：
      1. ~/.config/wechat-keys.json（SQLCipher 密钥）
      2. WeChat 容器目录（加密的 message_*.db 原文件）
    再加可调试副本目录，防止被远程触发抓 key。
    """
    config = {}
    try:
        config = json.loads(Path("~/.config/wechat-local-vault.json").expanduser().read_text())
    except Exception:
        pass
    db_base = config.get("db_base_path") or ""
    container = ""
    if db_base:
        # .../Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage
        idx = db_base.find("/Library/Containers/")
        if idx > 0:
            container = db_base[:idx] + "/Library/Containers/com.tencent.xinWeChat"
    if not container:
        container = str(Path("~/Library/Containers/com.tencent.xinWeChat").expanduser())

    deny_paths = [
        str(Path("~/.config/wechat-keys.json").expanduser()),
        container,
        str(Path("~/Desktop/WeChat.app").expanduser()),
    ]
    lines = ["(version 1)", "(allow default)"]
    for path in deny_paths:
        lines.append("(deny file-read*")
        lines.append(f'  (subpath "{path}"))')
    profile = "\n".join(lines) + "\n"

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(profile)
    os.chmod(PROFILE_FILE, 0o600)
    return PROFILE_FILE, deny_paths


def maybe_reexec_in_sandbox(enabled: bool) -> None:
    """若启用沙箱且当前不在沙箱内，则用 sandbox-exec 重启自己。"""
    if not enabled or os.environ.get("WX_SANDBOXED"):
        return
    if not Path(SANDBOX_EXEC).exists():
        print(json.dumps({
            "ok": False, "command": "serve",
            "error": {"code": "SANDBOX_UNAVAILABLE",
                      "message": f"找不到 {SANDBOX_EXEC}，拒绝以无沙箱方式对外提供数据",
                      "hints": ["确认系统有 /usr/bin/sandbox-exec",
                                "确需无沙箱运行请加 --no-sandbox（不推荐）"]},
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(6)

    profile, deny_paths = build_reader_profile()
    argv = [SANDBOX_EXEC, "-f", str(profile), sys.executable, *sys.argv]
    env = dict(os.environ, WX_SANDBOXED="1", WX_READER="1")
    print(json.dumps({
        "ok": True, "command": "serve",
        "data": {"sandbox": "enabled", "profile": str(profile),
                 "denied_read": deny_paths,
                 "note": "密钥与加密源库对服务进程不可读，解密在结构上不可能"},
    }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
    os.execve(SANDBOX_EXEC, argv, env)


class Handler(BaseHTTPRequestHandler):
    server_version = "wx-serve"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施

    def log_message(self, fmt, *args):  # 静音默认访问日志，用审计日志
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_RESPONSE:
            body = json.dumps({
                "ok": False, "command": "serve",
                "error": {"code": "RESPONSE_TOO_LARGE",
                          "message": f"响应 {len(body)} 字节超过上限 {MAX_RESPONSE}",
                          "hints": ["缩小 --limit", "缩短时间窗"]},
            }, ensure_ascii=False).encode("utf-8")
            code = 413
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _audit(self, command: str, argv: list[str], code: int, ms: int):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            peer = self.client_address[0]
            who = tailscale_whoami(peer) or "-"
            # 只记命令名与参数个数，不记参数值（避免把聊天内容/关键词写进日志）
            with AUDIT_FILE.open("a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "peer": peer, "peer_name": who,
                    "command": command, "argc": len(argv),
                    "exit": code, "ms": ms,
                }, ensure_ascii=False) + "\n")
            os.chmod(AUDIT_FILE, 0o600)
        except Exception:
            pass

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (self.headers.get("X-WX-Token") or "").strip()
        return bool(token) and hmac.compare_digest(token, self.server.token)  # type: ignore[attr-defined]

    def _deny(self, code: str, message: str, hints: list[str] | None = None, status: int = 401):
        self._send(status, {"ok": False, "command": "serve",
                            "error": {"code": code, "message": message, "hints": hints or []}})

    # ---- 路由

    def do_GET(self):
        started = time.time()
        if not self._authorized():
            self._deny("UNAUTHORIZED", "缺少或错误的 token",
                       ["请求头加 Authorization: Bearer <token>", "在本机跑 wx serve --token-show 查看"])
            self._audit("GET " + self.path, [], 401, 0)
            return
        path = self.path.split("?", 1)[0]
        if path in ("/v1/health", "/health", "/"):
            import wx  # 延迟导入，避免循环
            decrypted, vault = wx.vault_paths()
            payload = {
                "ok": True, "command": "health",
                "data": {
                    "version": wx.VERSION,
                    "vault": str(decrypted),
                    "decrypted_at": wx.decrypted_at(vault),
                    "vault_age_minutes": wx.base_meta(started).get("vault_age_minutes"),
                    "wechat_running": wx.wechat_running(),
                    "read_only": not getattr(self.server, "allow_write", False),
                    "max_limit": getattr(self.server, "max_limit", 2000),
                    "max_workers": getattr(self.server, "max_workers", 4),
                    "commands": sorted(READ_ONLY | (WRITE if getattr(self.server, "allow_write", False) else set())),
                },
                "meta": {"took_ms": int((time.time() - started) * 1000)},
            }
            self._send(200, payload)
            self._audit("health", [], 200, int((time.time() - started) * 1000))
            return
        if path in ("/v1/describe", "/describe"):
            code, env, _ = wx.execute(["describe"])
            self._send(200 if code == 0 else 500, env)
            self._audit("describe", [], code, int((time.time() - started) * 1000))
            return
        self._send(404, {"ok": False, "command": "serve",
                         "error": {"code": "NOT_FOUND", "message": f"未知路径 {path}",
                                   "hints": ["可用：GET /v1/health, GET /v1/describe, POST /v1/exec"]}})

    def do_POST(self):
        started = time.time()
        if not self._authorized():
            self._deny("UNAUTHORIZED", "缺少或错误的 token",
                       ["请求头加 Authorization: Bearer <token>", "在本机跑 wx serve --token-show 查看"])
            self._audit("POST " + self.path, [], 401, 0)
            return
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/exec", "/exec"):
            self._send(404, {"ok": False, "command": "serve",
                             "error": {"code": "NOT_FOUND", "message": f"未知路径 {path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._deny("BAD_REQUEST", f"请求体为空或超过 {MAX_BODY} 字节", status=400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._deny("BAD_REQUEST", f"JSON 解析失败：{exc}", status=400)
            return

        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            self._deny("BAD_REQUEST", "需要 {\"argv\": [\"history\", \"<chat>\", ...]}", status=400)
            return

        command = next((a for a in argv if not a.startswith("-")), "argv")
        inline_export = command == "export" and "--inline" in argv
        if command not in READ_ONLY and not inline_export and not getattr(self.server, "allow_write", False):
            self._deny("WRITE_FORBIDDEN",
                       f"服务端为只读模式，不允许 {command}",
                       ["只读模式允许 wx export --inline（不写文件）",
                        "需要落盘能力时在本机启动：wx serve --allow-write"], status=403)
            self._audit(command, argv, 403, 0)
            return

        try:
            argv = self._clamp(argv)
        except PermissionError as exc:
            self._deny("CONFIRM_REQUIRED",
                       f"{exc.args[0]} 需要在本机人工确认，远程不可触发",
                       ["在本机跑该命令", "或改用只读等价命令（如 wx key 不带 --capture）"],
                       status=403)
            self._audit(command, argv, 403, 0)
            return
        # 每个请求跑独立子进程：绕开 GIL 争用、隔离崩溃、与本地 CLI 完全同源
        acquired = self.server.slots.acquire(timeout=1.0)  # type: ignore[attr-defined]
        if not acquired:
            self._deny("BUSY", "服务端并发已满，稍后重试",
                       [f"当前上限 {getattr(self.server, 'max_workers', 0)}"], status=503)
            self._audit(command, argv, 503, 0)
            return
        try:
            code, envelope = self._run_subprocess(argv, started)
        finally:
            self.server.slots.release()  # type: ignore[attr-defined]

        status = 200
        self._send(status, envelope)
        self._audit(command, argv, code, int((time.time() - started) * 1000))

    def _run_subprocess(self, argv: list[str], started: float) -> tuple[int, dict]:
        import wx as wxmod
        script = Path(wxmod.__file__).resolve()
        # 子进程必须只走本地执行：清掉 WX_REMOTE/WX_TOKEN，否则会转发给自己
        env = {k: v for k, v in os.environ.items()
               if k not in ("WX_REMOTE", "WX_TOKEN", "WX_TOKEN_FILE", "WX_TIMEOUT")}
        env["WX_READER"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *argv, "--format", "json", "--compact"],
                capture_output=True, text=True, env=env,
                timeout=getattr(self.server, "timeout", 300),
            )
        except subprocess.TimeoutExpired:
            return 504, {"ok": False, "command": argv[0],
                         "error": {"code": "TIMEOUT",
                                   "message": f"超过 {getattr(self.server, 'timeout', 300)}s",
                                   "hints": ["缩小时间窗", "降低 --limit"]}}
        # CLI 成功写 stdout、失败写 stderr；两端都要尝试解析
        for stream in (proc.stdout, proc.stderr):
            raw = (stream or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and "ok" in payload:
                    return proc.returncode, payload
            except json.JSONDecodeError:
                continue
        err = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-6:]
        return 500, {"ok": False, "command": argv[0],
                     "error": {"code": "INTERNAL", "message": "子进程输出不是 JSON",
                               "detail": err,
                               "hints": ["本机直接跑同一条命令复现"]}}

    # ---- 参数收敛

    def _clamp(self, argv: list[str]) -> list[str]:
        """把 --limit 类参数压到服务端上限，并禁用危险开关。"""
        cap = getattr(self.server, "max_limit", 2000)
        out: list[str] = []
        i = 0
        while i < len(argv):
            a = argv[i]
            if a in LIMIT_KEYS and i + 1 < len(argv):
                try:
                    value = min(int(argv[i + 1]), cap)
                except ValueError:
                    out.append(a)
                    i += 1
                    continue
                out.extend([a, str(value)])
                i += 2
                continue
            if a == "--format":            # 输出格式由服务端统一为 JSON
                i += 2
                continue
            if a.startswith("--format="):
                i += 1
                continue
            if a in ("--compact",):
                i += 1
                continue
            if a in ("--yes", "--force", "--capture"):
                # 远程不允许触发需要人工确认的动作；显式报错，不静默吞掉
                raise PermissionError(a)
            out.append(a)
            i += 1
        return out


def service_state() -> dict:
    """返回常驻服务的当前状态（不修改任何东西）。"""
    loaded = False
    detail = ""
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                             capture_output=True, text=True, timeout=10)
        loaded = out.returncode == 0
        if loaded:
            for line in (out.stdout or "").splitlines():
                if "state = " in line or "pid = " in line:
                    detail += line.strip() + " "
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return {
        "label": SERVICE_LABEL,
        "plist": str(SERVICE_PLIST),
        "installed": SERVICE_PLIST.exists(),
        "loaded": loaded,
        "detail": detail.strip(),
        "logs": {"out": str(SERVICE_LOG_DIR / "wx-serve.out.log"),
                 "err": str(SERVICE_LOG_DIR / "wx-serve.err.log")},
    }


def _service_argv(port: int, allow_write: bool, max_limit: int, max_workers: int,
                  timeout: int, host: str | None, no_sandbox: bool) -> list[str]:
    argv = [sys.executable, str(Path(__file__).resolve().parent / "wx.py"), "serve",
            "--port", str(port), "--max-limit", str(max_limit),
            "--max-workers", str(max_workers), "--timeout", str(timeout)]
    if host:
        argv += ["--host", host]
    if allow_write:
        argv.append("--allow-write")
    if no_sandbox:
        argv.append("--no-sandbox")
    return argv


def install_service(port: int, allow_write: bool, max_limit: int, max_workers: int,
                    timeout: int, host: str | None, no_sandbox: bool) -> dict:
    """写 LaunchAgent 并加载：开机自启、崩溃自动重启。"""
    import wx as wxmod

    argv = _service_argv(port, allow_write, max_limit, max_workers, timeout, host, no_sandbox)
    env = {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path("~").expanduser()),
        "WX_SKILL_DIR": str(wxmod.SKILL_DIR),
        "WX_PYTHON": str(wxmod.VENV_PY),
    }
    env_xml = "\n".join(
        "    <key>%s</key>\n    <string>%s</string>" % (k, v) for k, v in env.items()
    )
    args_xml = "\n".join("    <string>%s</string>" % a for a in argv)
    out_log = SERVICE_LOG_DIR / "wx-serve.out.log"
    err_log = SERVICE_LOG_DIR / "wx-serve.err.log"
    plist = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        '<dict>',
        '  <key>Label</key>',
        '  <string>%s</string>' % SERVICE_LABEL,
        '  <key>ProgramArguments</key>',
        '  <array>',
        args_xml,
        '  </array>',
        '  <key>EnvironmentVariables</key>',
        '  <dict>',
        env_xml,
        '  </dict>',
        '  <key>RunAtLoad</key>',
        '  <true/>',
        '  <key>KeepAlive</key>',
        '  <true/>',
        '  <key>ThrottleInterval</key>',
        '  <integer>10</integer>',
        '  <key>ProcessType</key>',
        '  <string>Background</string>',
        '  <key>StandardOutPath</key>',
        '  <string>%s</string>' % out_log,
        '  <key>StandardErrorPath</key>',
        '  <string>%s</string>' % err_log,
        '</dict>',
        '</plist>',
        '',
    ])
    SERVICE_PLIST.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_PLIST.write_text(plist)
    os.chmod(SERVICE_PLIST, 0o600)

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                   capture_output=True, text=True)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(SERVICE_PLIST)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        proc = subprocess.run(["launchctl", "load", "-w", str(SERVICE_PLIST)],
                              capture_output=True, text=True)
    return {
        "installed": SERVICE_PLIST.exists(),
        "loaded": proc.returncode == 0,
        "plist": str(SERVICE_PLIST),
        "program": argv,
        "logs": {"out": str(out_log), "err": str(err_log)},
        "error": (proc.stderr or "").strip()[:300],
        "next": "lsof -nP -iTCP:%d -sTCP:LISTEN" % port,
    }


def uninstall_service() -> dict:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                   capture_output=True, text=True)
    subprocess.run(["launchctl", "unload", "-w", str(SERVICE_PLIST)],
                   capture_output=True, text=True)
    removed = False
    if SERVICE_PLIST.exists():
        SERVICE_PLIST.unlink()
        removed = True
    return {"removed": removed, "plist": str(SERVICE_PLIST),
            "note": "已卸载；vault 与 token 未受影响"}


def run_server(host: str | None, port: int, allow_write: bool, max_limit: int,
               token: str, max_workers: int = 4, timeout: int = 300,
               sandbox: bool = True) -> int:
    maybe_reexec_in_sandbox(sandbox)
    target = host
    if not target:
        target = tailscale_ipv4() or "127.0.0.1"
    if target == "0.0.0.0" and not os.environ.get("WX_ALLOW_LAN"):
        print(json.dumps({
            "ok": False, "command": "serve",
            "error": {"code": "UNSAFE_BIND", "message": "拒绝绑定 0.0.0.0（会暴露到局域网）",
                      "hints": ["默认绑定 Tailscale IP", "确需如此请设 WX_ALLOW_LAN=1"]},
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    httpd = ThreadingHTTPServer((target, port), Handler)
    httpd.daemon_threads = True
    httpd.token = token         # type: ignore[attr-defined]
    httpd.allow_write = allow_write  # type: ignore[attr-defined]
    httpd.max_limit = max_limit  # type: ignore[attr-defined]
    httpd.max_workers = max_workers  # type: ignore[attr-defined]
    httpd.timeout = timeout  # type: ignore[attr-defined]
    httpd.slots = threading.BoundedSemaphore(max_workers)  # type: ignore[attr-defined]

    print(json.dumps({
        "ok": True, "command": "serve",
        "data": {
            "listen": f"http://{target}:{port}",
            "read_only": not allow_write,
            "max_limit": max_limit,
            "max_workers": max_workers,
            "timeout": timeout,
            "sandbox": bool(os.environ.get("WX_SANDBOXED")) or not sandbox,
            "token_file": str(TOKEN_FILE),
            "audit_file": str(AUDIT_FILE),
            "endpoints": {
                "health": f"http://{target}:{port}/v1/health",
                "describe": f"http://{target}:{port}/v1/describe",
                "exec": f"POST http://{target}:{port}/v1/exec  {{\"argv\": [...]}}",
            },
            "client": f"WX_REMOTE=http://{target}:{port} WX_TOKEN=<token> wxc history <chat>",
            "hint": "Ctrl-C 停止；token 只在本机文件里，用 wx serve --token-show 查看",
        },
    }, ensure_ascii=False, indent=2), flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0
