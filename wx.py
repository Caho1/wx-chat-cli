#!/usr/bin/env python3
"""wx — 微信本地聊天记录 CLI（面向 Agent 的统一入口）

设计目标：让任何 Agent 在任意工作目录下，用一条命令拿到**机器可解析**的结果。

统一契约：
  - 成功：{"ok": true, "command": "...", "data": {...}, "meta": {...}}
  - 失败：{"ok": false, "command": "...", "error": {"code", "message", "hints", ...}}
  - 退出码：0 成功 / 2 参数错 / 3 找不到 / 4 有歧义 / 5 vault 不可用 / 6 依赖缺失 / 1 其他

所有命令都只读（refresh / export / digest / voice 会在仓库外写文件）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_DIR = SCRIPT_DIR / "tools"


def _discover_backend() -> Path:
    """定位已安装的 yichen-wechat-local-vault（提供 scripts/vault_cli.py）。

    环境变量 WX_SKILL_DIR 优先，其次在当前目录及常见位置里找。
    """
    env = os.environ.get("WX_SKILL_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    here = Path.cwd()
    candidates += [
        here / "yichen-wechat-local-vault",
        here.parent / "yichen-wechat-local-vault",
        here / "yichen-skills" / "yichen-wechat-local-vault",
        Path("~/yichen-wechat-local-vault").expanduser(),
        Path("~/Desktop/pythonproject/yichen-skills/yichen-wechat-local-vault").expanduser(),
        Path("~/code/yichen-skills/yichen-wechat-local-vault").expanduser(),
    ]
    for candidate in candidates:
        if (candidate / "scripts" / "vault_cli.py").exists():
            return candidate
    raise SystemExit(
        "找不到后端 vault_cli.py。请设置 WX_SKILL_DIR 指向 yichen-wechat-local-vault 目录。\n"
        "例如： export WX_SKILL_DIR=$HOME/code/yichen-skills/yichen-wechat-local-vault"
    )


SKILL_DIR = _discover_backend()
# 「本机解释器」：refresh 等后端脚本用它；默认沿用当前解释器，可用 WX_PYTHON 覆盖
VENV_PY = Path(os.environ.get("WX_PYTHON") or sys.executable)

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

try:
    import vault_cli as vc  # noqa: E402
    import call_log as cl  # noqa: E402
    import latest_messages as lm  # noqa: E402
    import scan_types as st  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(json.dumps({
        "ok": False,
        "command": "startup",
        "error": {"code": "IMPORT_FAILED", "message": str(exc),
                  "hints": [f"确认 SKILL_DIR 存在：{SKILL_DIR}", f"确认 venv：{VENV_PY}"]},
    }, ensure_ascii=False, indent=2))
    raise SystemExit(6)

VERSION = "1.0.0"

# ── reader 模式 ─────────────────────────────────────────────────────────
# WX_READER=1 时（由 `wx serve` 的沙箱进程设置），本进程**只读已解密的 vault**：
#   - 不碰 wechat-keys.json、不碰加密源库、不调用任何解密脚本
#   - 相关子命令在 argparse 之前就被拒绝，返回 READER_FORBIDDEN
# 这是沙箱之外的第二道防线（沙箱负责让它物理上读不到密钥）。
READER_MODE = os.environ.get("WX_READER") == "1"
# 只拦「解密域」：refresh 与 key。其余写盘命令交给服务端 --allow-write 决定，
# 这样 --allow-write 仍然有意义，而解密在任何配置下都不可远程触发。
READER_FORBIDDEN = {
    "refresh": "解密（decrypt_all_dbs.py）只允许在本地人工执行",
    "key": "密钥校验/抓取需要读 wechat-keys.json 与微信容器",
}
DEFAULT_EXPORTS = Path("~/Documents/wechat-local-vault/exports").expanduser()


# ---------------------------------------------------------------- 错误与信封

class WxError(Exception):
    codes = {
        "BAD_ARGUMENT": 2,
        "CHAT_NOT_FOUND": 3,
        "CHAT_AMBIGUOUS": 4,
        "VAULT_MISSING": 5,
        "KEY_MISSING": 5,
        "DEPENDENCY_MISSING": 6,
        "NO_RESULT": 3,
        "INTERNAL": 1,
    }

    def __init__(self, code: str, message: str, hints: list[str] | None = None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints or []
        self.extra = extra
        self.exit_code = self.codes.get(code, 1)

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.hints:
            payload["hints"] = self.hints
        payload.update(self.extra)
        return payload


class Parser(argparse.ArgumentParser):
    """把 argparse 的 SystemExit 变成结构化错误，Agent 才能稳定解析。"""

    def error(self, message):  # type: ignore[override]
        raise WxError("BAD_ARGUMENT", message,
                      [f"看可用命令：wx describe", f"看某命令参数：wx {self.prog.split()[1] if ' ' in self.prog else ''} --help"])


# ---------------------------------------------------------------- 上下文

def vault_paths() -> tuple[Path, Path]:
    config = vc.load_config()
    decrypted = Path(config.get("decrypted_dir") or vc.DEFAULT_DECRYPTED_DIR).expanduser()
    vault = Path(config.get("vault_dir") or vc.DEFAULT_VAULT_DIR).expanduser()
    return decrypted, vault


def decrypted_at(vault: Path) -> str:
    manifests = sorted((vault / "manifests").glob("decrypt-*.json"))
    if not manifests:
        return ""
    try:
        return json.loads(manifests[-1].read_text()).get("created_at", "")
    except Exception:
        return ""


def wechat_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "/Applications/WeChat.app/Contents/MacOS/WeChat$"],
                             capture_output=True, text=True, timeout=5)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def base_meta(started: float) -> dict:
    decrypted, vault = vault_paths()
    stamp = decrypted_at(vault)
    meta = {
        "vault": str(decrypted),
        "decrypted_at": stamp,
        "wechat_running": wechat_running(),
        "took_ms": int((time.time() - started) * 1000),
        "cli_version": VERSION,
    }
    if stamp:
        try:
            age = dt.datetime.now() - dt.datetime.fromisoformat(stamp)
            meta["vault_age_minutes"] = round(age.total_seconds() / 60, 1)
        except ValueError:
            pass
    return meta


def ensure_vault() -> Path:
    decrypted, vault = vault_paths()
    if not decrypted.exists():
        raise WxError("VAULT_MISSING", f"明文 vault 不存在：{decrypted}",
                      ["先跑：wx refresh --full", "检查解密清单：ls ~/Library/Application\\ Support/wechat-local-vault/manifests"],
                      decrypted_dir=str(decrypted))
    if not (decrypted / "message").glob("message_*.db"):
        raise WxError("VAULT_MISSING", f"vault 里没有消息库：{decrypted}/message",
                      ["解密可能只完成了一部分；跑：wx refresh --full"])
    return decrypted


def resolve(query: str, decrypted: Path) -> dict:
    """解析会话名。候选唯一才返回；否则抛 CHAT_AMBIGUOUS（带候选）。"""
    contacts, _ = vc.load_contacts(decrypted)
    if query in contacts:
        return contacts[query]
    if query.startswith(("wxid_", "gh_")) or "@chatroom" in query:
        hit = contacts.get(query)
        if hit:
            return hit
        return {"username": query, "display_name": query, "is_group": "@chatroom" in query}
    q = query.lower()
    exact = [c for c in contacts.values() if q == c["display_name"].lower()]
    if len(exact) == 1:
        return exact[0]
    pool = exact or [
        c for c in contacts.values()
        if any(q in str(c.get(f) or "").lower() for f in ("display_name", "remark", "nick_name", "alias", "username"))
    ]
    if not pool:
        raise WxError("CHAT_NOT_FOUND", f"找不到会话：{query}",
                      ["用 wx find <关键词> 先看候选", "或用 username 精确定位"])
    if len(pool) > 1:
        raise WxError(
            "CHAT_AMBIGUOUS",
            f"「{query}」匹配到 {len(pool)} 个会话，请用 username 精确定位",
            ["把 candidates[].username 之一作为参数重试"],
            candidates=[{"display_name": c["display_name"], "username": c["username"],
                         "is_group": c["is_group"], "remark": c.get("remark", "")} for c in pool[:20]],
        )
    return pool[0]


def parse_time(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    try:
        return vc.parse_time(value, end_of_day=end_of_day)
    except SystemExit as exc:
        raise WxError("BAD_ARGUMENT", str(exc),
                      ["时间格式只接受 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS"]) from None


def window(start: str | None, until: str | None, hours: int | None) -> tuple[int | None, int | None]:
    end_ts = parse_time(until, end_of_day=True)
    if start:
        return parse_time(start), end_ts
    if hours:
        end = end_ts or int(time.time())
        return end - hours * 3600, end_ts
    return None, end_ts


def out_envelope(command: str, data, started: float, extra_meta: dict | None = None) -> dict:
    meta = base_meta(started)
    if extra_meta:
        meta.update(extra_meta)
    return {"ok": True, "command": command, "data": data, "meta": meta}


# ---------------------------------------------------------------- 文本渲染（给人看）

def render_text(command: str, data: dict) -> str:
    lines: list[str] = []
    if command == "doctor":
        lines.append(f"vault: {data['vault']}")
        lines.append(f"状态: {'可用' if data['vault_ok'] else '不可用'}")
        lines.append(f"解密于: {data['decrypted_at'] or '（无清单）'}"
                     f"{'  过期 %.0f 分钟' % data['vault_age_minutes'] if data.get('vault_age_minutes') else ''}")
        lines.append(f"微信运行: {'是' if data['wechat_running'] else '否'}")
        lines.append(f"密钥: {data['keys']['count']} 个")
        lines.append(f"消息库: {data['databases']['present']}/{data['databases']['expected']}")
    elif command in ("chats",):
        for row in data["chats"]:
            tag = "群" if row["is_group"] else "私聊"
            unread = f" ({row['unread']}未读)" if row["unread"] else ""
            lines.append(f"[{row['time']}] [{tag}] {row['chat']}{unread}\n    {row['last_message'][:80]}")
    elif command == "find":
        for c in data["candidates"]:
            tag = "群" if c["is_group"] else "联系人"
            lines.append(f"[{tag}] {c['display_name']}  →  {c['username']}")
    elif command in ("history", "search"):
        for m in data["messages"]:
            who = f"{m['sender']}: " if m.get("sender") else ""
            chat = f"[{m.get('chat','')}] " if m.get("chat") else ""
            lines.append(f"[{m['time']}] {chat}{who}{m['content']}")
    elif command == "stats":
        lines.append(f"{data['chat']}  共 {data['total']} 条")
        for k, v in data["type_breakdown"].items():
            lines.append(f"  {k}: {v}")
    elif command == "calls":
        s = data["summary"]
        lines.append(f"{data['chat']}  通话 {s['count']} 条，已接通 {s['answered']}，累计 {s['total_hours']} 小时")
        for c in data["calls"]:
            lines.append(f"  {c['time']}  {c['duration_human'] or '-':>9}  {c['outcome']}  {c['initiator']}")
    elif command == "key":
        if data.get("captured"):
            lines.append("已抓取；下一步：wx refresh --full")
        else:
            lines.append(f"密钥 {data['total']} 个，可用 {data['usable']}，失效 {len(data['unusable'])}，"
                         f"源库缺失 {len(data['source_missing'])}")
            if data.get("advice"):
                lines.append(data["advice"])
    elif command == "security":
        lines.append(f"profile: {data['profile']}")
        for c in data["checks"]:
            lines.append(f"  {'✓' if c['ok'] else '✗'} {c['check']:<22} {c['detail'][:50]}")
        lines.append(f"{data['passed']}/{data['total']} — {data['verdict']}")
    elif command == "export":
        lines.append(f"已写出 {data['count']} 条 → {data['path']}（{data['bytes']} 字节）")
    elif command == "digest":
        lines.append(f"素材包: {data.get('folder', '')}")
        lines.append(f"消息数: {data.get('message_count', 0)}")
        lines.append(f"JSON: {data.get('source_json', '')}")
    elif command == "voice":
        lines.append(f"待导出 {data['pending']} 条")
        for c in data["by_chat"][:10]:
            lines.append(f"  {c['count']:6d}  {c['chat']}")
        if data.get("result"):
            r = data["result"]
            lines.append(f"完成 {r['exported']} 条，跳过 {r['skipped_existing']}，失败 {r['failed']}，"
                         f"音频 {r['total_minutes']} 分钟")
            lines.append(f"manifest: {r['manifest']}")
    elif command == "favorites":
        for f in data["favorites"]:
            lines.append(f"[{f['time']}] [{f['type']}] {f['summary']}")
    elif command == "moments":
        for m in data["moments"]:
            lines.append(f"[{m['time']}] {m.get('display_name') or m['username']}\n  {m['content']}")
    elif command == "refresh":
        lines.append(f"模式 {data['mode']}，退出码 {data['returncode']}")
        for line in data["tail"]:
            lines.append(f"  {line}")
    elif command == "pull":
        t = data["totals"]
        lines.append(f"窗口 {data['window']['start']} ~ {data['window']['end']}：{t['messages']} 条 / {t['chats']} 会话")
        for c in data["chats"][:15]:
            lines.append(f"  {c['count']:6d}  {c['chat']}")
    elif command == "scan":
        if "periods" in data:
            lines.append(f"{data['label']}  命中 {data['totals']['matched']} / 同期 {data['totals']['messages_in_scope']}")
            for p in data["periods"]:
                flag = "  ← 断档" if p["matched"] == 0 and p["messages"] > 0 else ""
                lines.append(f"  {p['period']:<10} {p['matched']:>7} / {p['messages']:>9}{flag}")
        else:
            for h in data["hits"]:
                lines.append(f"  {h['db']}::{h['table']} rows={h['rows']} ({h['matched_on']})")
    else:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return "\n".join(lines)


# ---------------------------------------------------------------- 命令实现

def cmd_doctor(args) -> tuple[dict, dict]:
    decrypted, vault = vault_paths()
    started = time.time()
    expected = ["contact/contact.db", "session/session.db", "sns/sns.db",
                "favorite/favorite.db", "message/message_resource.db", "message/media_0.db"]
    present = [rel for rel in expected if (decrypted / rel).exists()]
    shards = sorted((decrypted / "message").glob("message_*.db")) if decrypted.exists() else []
    keys_file = Path("~/.config/wechat-keys.json").expanduser()
    keys: dict = {}
    keys_readable = True
    keys_exists = True
    try:
        keys_exists = keys_file.exists()
    except PermissionError:
        keys_exists = False
    if keys_exists:
        try:
            keys = json.loads(keys_file.read_text())
        except PermissionError:
            keys_readable = False
        except Exception:
            keys = {}
    else:
        keys_readable = False if READER_MODE else keys_exists
    contacts = 0
    try:
        contacts = len(vc.load_contacts(decrypted)[0])
    except Exception:
        pass
    stamp = decrypted_at(vault)
    age_minutes = None
    if stamp:
        try:
            age_minutes = round((dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds() / 60, 1)
        except ValueError:
            pass
    data = {
        "vault": str(decrypted),
        "vault_ok": bool(shards),
        "decrypted_at": stamp,
        "vault_age_minutes": age_minutes,
        "stale": (age_minutes is not None and age_minutes > 30),
        "wechat_running": wechat_running(),
        "keys": {"file": str(keys_file), "exists": keys_exists,
                 "readable": keys_readable, "count": len(keys),
                 "note": "" if keys_readable else "reader 沙箱内不可读，属预期（解密由本地负责）"},
        "reader_mode": READER_MODE,
        "sandboxed": os.environ.get("WX_SANDBOXED") == "1",
        "databases": {"present": len(present) + len(shards), "expected": len(expected) + len(shards) or len(expected),
                      "missing": [rel for rel in expected if rel not in present]},
        "message_shards": [p.name for p in shards],
        "contacts_loaded": contacts,
        "python": sys.executable,
        "skill_dir": str(SKILL_DIR),
    }
    return data, {}


def find_db_base(config: dict) -> Path:
    path = config.get("db_base_path")
    if path:
        return Path(path).expanduser()
    wxid = config.get("wxid")
    if not wxid:
        raise WxError("BAD_ARGUMENT", "配置里没有 db_base_path/wxid",
                      ["在 ~/.config/wechat-local-vault.json 里补上"])
    return (Path("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files").expanduser()
            / wxid / "db_storage")


def try_key(db_path: Path, key_hex: str) -> bool:
    """用密钥解第 0 页，page size 正确即视为有效（SQLCipher 通用做法）。"""
    from Crypto.Cipher import AES  # 仅校验时需要

    with db_path.open("rb") as f:
        page = f.read(4096)
    if len(page) < 4096:
        return False
    iv = page[4096 - 80:4096 - 64]
    plain = AES.new(bytes.fromhex(key_hex[:64]), AES.MODE_CBC, iv).decrypt(page[16:4096 - 80])
    return plain[:2] == b"\x10\x00"


def key_missing_error() -> WxError:
    return WxError("KEY_MISSING", "没有可用密钥，无法解密",
                   ["跑 wx key（需要人工扫码登录）", "或先确认 ~/.config/wechat-keys.json 存在"])


def cmd_refresh(args) -> tuple[dict, dict]:
    decrypted, vault = vault_paths()
    mode = "full" if args.full else "incremental"
    script = SKILL_DIR / "scripts/decrypt_all_dbs.py"
    if not script.exists():
        raise WxError("DEPENDENCY_MISSING", f"找不到解密脚本：{script}")
    proc = subprocess.run([str(VENV_PY), str(script), "--mode", mode],
                          capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    data = {
        "mode": mode,
        "returncode": proc.returncode,
        "tail": tail,
        "decrypted_at": decrypted_at(vault),
        "vault": str(decrypted),
    }
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()[-5:]
        raise WxError("INTERNAL", "解密失败", ["看 stderr_tail", "可能需要重新抓 key：wx key"],
                      stderr_tail=stderr, stdout_tail=tail)
    return data, {}


def cmd_chats(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    rows = vc.session_rows(decrypted, args.unread, args.limit)
    start_ts, end_ts = window(args.since, args.until, args.hours)
    if start_ts is not None:
        rows = [r for r in rows if r["timestamp"] >= start_ts]
    if end_ts is not None:
        rows = [r for r in rows if r["timestamp"] <= end_ts]
    return {"count": len(rows), "chats": rows}, {}


def cmd_find(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    contacts, _ = vc.load_contacts(decrypted)
    q = args.query.lower()
    pool = [
        c for c in contacts.values()
        if any(q in str(c.get(f) or "").lower() for f in ("display_name", "remark", "nick_name", "alias", "username"))
    ]
    pool.sort(key=lambda c: (not c["is_group"], c["display_name"]))
    items = [{"display_name": c["display_name"], "username": c["username"], "is_group": c["is_group"],
              "remark": c.get("remark", ""), "nick_name": c.get("nick_name", "")} for c in pool[: args.limit]]
    if not items:
        raise WxError("CHAT_NOT_FOUND", f"没有匹配「{args.query}」的会话",
                      ["换更短的关键词", "或 wx chats 看最近会话"])
    return {"query": args.query, "count": len(items), "ambiguous": len(items) > 1,
            "candidates": items}, {}


def cmd_history(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    chat = resolve(args.chat, decrypted)
    start_ts, end_ts = window(args.since, args.until, args.hours)
    media_disabled = READER_MODE and getattr(args, "media", False)
    rows = vc.collect_history(decrypted, chat, start_ts, end_ts, args.limit, args.offset,
                              args.type, args.media and not media_disabled)
    data = {
        "chat": chat["display_name"],
        "username": chat["username"],
        "is_group": chat["is_group"],
        "count": len(rows),
        "messages": rows,
    }
    meta = {"window": {"start": args.since or "", "end": args.until or "", "hours": args.hours}}
    if media_disabled:
        meta["media_hints_disabled"] = "reader 沙箱不可读微信容器；附件路径提示已关闭"
    return data, meta


def _search_one_chat(decrypted: Path, chat: dict, keyword: str, start_ts: int | None, end_ts: int | None,
                     type_name: str | None, per_slice: int) -> list[dict]:
    rows = vc.collect_history(decrypted, chat, start_ts, end_ts, per_slice, 0, type_name, False)
    kw = keyword.lower()
    return [r for r in rows if kw in (r.get("content") or "").lower()]


def _search_all(decrypted: Path, keyword: str, start_ts: int | None, end_ts: int | None,
                type_name: str | None, per_slice: int, max_scan: int) -> tuple[list[dict], int, bool]:
    contacts, _ = vc.load_contacts(decrypted)
    kw = keyword.lower()
    found: list[dict] = []
    scanned = 0
    partial = False
    for db in sorted((decrypted / "message").glob("message_*.db")):
        con = vc.connect(db)
        try:
            tables = [r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            name2id = vc.load_name2id(con)
            for table in tables:
                if scanned >= max_scan:
                    partial = True
                    break
                username = vc.username_for_table(table, con) or vc.username_for_table_from_contacts(table, contacts)
                chat = {"username": username,
                        "display_name": vc.display_name(username, contacts) if username else table,
                        "is_group": "@chatroom" in username}
                cols = vc.message_columns(con, table)
                sql, params = vc.build_select_sql(table, cols, start_ts, end_ts, None, type_name, per_slice, 0)
                try:
                    batch = con.execute(sql, params)
                except Exception:
                    continue
                for row in batch:
                    scanned += 1
                    msg = vc.row_to_message(row, db.name, table, chat, contacts, name2id)
                    if kw in (msg.get("content") or "").lower():
                        msg["chat"] = chat["display_name"]
                        msg["chat_username"] = chat["username"]
                        found.append(msg)
        finally:
            con.close()
        if partial:
            break
    return found, scanned, partial


def cmd_search(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    start_ts, end_ts = window(args.since, args.until, args.hours)
    if start_ts is None and end_ts is None:
        start_ts = int(time.time()) - 7 * 86400
        auto_window = True
    else:
        auto_window = False

    # 按期切片，绕开「先取候选再过滤」导致的漏检
    slice_days = max(1, args.slice_days)
    slices: list[tuple[int, int]] = []
    cur = start_ts
    stop = end_ts or int(time.time())
    while cur < stop:
        nxt = min(cur + slice_days * 86400, stop)
        slices.append((cur, nxt))
        cur = nxt

    results: list[dict] = []
    scanned = 0
    partial = False
    targets = [resolve(q, decrypted) for q in args.chat] if args.chat else None

    for lo, hi in slices:
        if targets:
            for chat in targets:
                results.extend(_search_one_chat(decrypted, chat, args.keyword, lo, hi, args.type, args.per_slice))
        else:
            found, n, part = _search_all(decrypted, args.keyword, lo, hi, args.type, args.per_slice, args.max_scan)
            results.extend(found)
            scanned += n
            partial = partial or part

    # 同一消息可能被多个切片/多次命中，按 (chat_username, db, local_id) 去重
    seen = set()
    deduped = []
    for m in results:
        key = (m.get("chat_username") or m.get("username"), m.get("db"), m.get("local_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    deduped.sort(key=lambda m: m["timestamp"], reverse=True)
    page = deduped[: args.limit]
    page.sort(key=lambda m: m["timestamp"])

    data = {
        "keyword": args.keyword,
        "count": len(page),
        "total_matched": len(deduped),
        "partial": partial,
        "messages": page,
    }
    meta = {
        "window": {"start": dt.datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
                   "end": dt.datetime.fromtimestamp(stop).isoformat(timespec="seconds"),
                   "slices": len(slices), "slice_days": slice_days, "auto_7d": auto_window},
        "scanned_messages": scanned,
        "note": "按时间切片检索；partial=true 表示触发扫描上限，结果不完整",
    }
    return data, meta


def cmd_stats(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    chat = resolve(args.chat, decrypted)
    start_ts, end_ts = window(args.since, args.until, args.hours)
    stats = vc.collect_stats(decrypted, chat, start_ts, end_ts)
    data = {"chat": chat["display_name"], "username": chat["username"],
            "is_group": chat["is_group"], **stats}
    return data, {}


def cmd_export(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    chat = resolve(args.chat, decrypted)
    start_ts, end_ts = window(args.since, args.until, args.hours)
    rows = vc.collect_history(decrypted, chat, start_ts, end_ts, args.limit, 0, args.type,
                              args.media and not READER_MODE)
    if args.doc_format == "markdown":
        body = vc.render_export_markdown(chat, rows, args.since, args.until)
        suffix = "md"
    else:
        body = vc.render_messages_text(rows)
        suffix = "txt"
    text = body.rstrip() + "\n"
    if args.inline:
        return {"chat": chat["display_name"], "count": len(rows),
                "doc_format": args.doc_format, "content": text}, {"truncated_by_limit": len(rows) >= args.limit}
    out = Path(args.out).expanduser() if args.out else (
        DEFAULT_EXPORTS / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{vc.safe_name(chat['display_name'])}.{suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"chat": chat["display_name"], "count": len(rows), "path": str(out),
            "bytes": out.stat().st_size}, {"truncated_by_limit": len(rows) >= args.limit}


def cmd_pull(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    _, vault = vault_paths()
    end_ts = parse_time(args.until, end_of_day=True) or int(time.time())
    start_ts = parse_time(args.since) or (end_ts - args.hours * 3600)
    payload = lm.collect_window(decrypted, vault, start_ts, end_ts, args.limit_per_chat, args.min_count)
    payload["window"]["label"] = (
        f"{dt.datetime.fromtimestamp(start_ts):%Y-%m-%d %H:%M} ~ "
        f"{dt.datetime.fromtimestamp(end_ts):%Y-%m-%d %H:%M}")
    if args.save:
        out_dir = Path(args.out).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{dt.datetime.fromtimestamp(start_ts):%m%d}-{dt.datetime.fromtimestamp(end_ts):%m%d}"
        json_path = out_dir / f"all-chats-{dt.datetime.now():%Y%m%d-%H%M}-{tag}.json"
        md_path = json_path.with_suffix(".md")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(lm.render_markdown(payload, payload["window"]["label"]), encoding="utf-8")
        payload["saved"] = {"json": str(json_path), "markdown": str(md_path)}
    return payload, {}


def cmd_calls(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    config = vc.load_config()
    contacts, _ = vc.load_contacts(decrypted)
    chat = resolve(args.chat, decrypted)
    start_ts, end_ts = window(args.since, args.until, args.hours)
    records = cl.collect_calls(decrypted, config, contacts, chat, start_ts, end_ts,
                               args.duration_min, args.limit)
    data = {"chat": chat["display_name"], "username": chat["username"],
            "summary": cl.summarize(records), "calls": records}
    return data, {}


def cmd_voice(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    import export_voice as ev
    contacts, _ = vc.load_contacts(decrypted)
    chat = None
    if args.chat:
        chat = resolve(args.chat, decrypted)
    elif not args.all_chats:
        raise WxError("BAD_ARGUMENT", "需要 --chat 或 --all-chats")
    start_ts = ev.parse_day(args.since)
    end_ts = ev.parse_day(args.until)
    planned = ev.plan(decrypted, chat, start_ts, end_ts, args.limit)
    by_chat = [{"chat": contacts.get(u, {}).get("display_name") or u, "username": u, "count": n}
               for u, n in sorted(planned["by_chat"].items(), key=lambda x: -x[1])]
    data = {"pending": len(planned["rows"]), "by_chat": by_chat[:20], "dry_run": args.dry_run}
    if not args.dry_run:
        out_root = Path(args.out).expanduser()
        senders = ev.sender_index(decrypted, chat, start_ts, end_ts) if chat else {}
        summary = ev.export_records(planned, chat, contacts, out_root, args.audio_format, args.force, senders)
        data["result"] = summary
    return data, {"audio_dir": str(Path(args.out).expanduser())}


def cmd_digest(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    chat = resolve(args.chat, decrypted)
    if not chat["is_group"]:
        raise WxError("BAD_ARGUMENT", f"{chat['display_name']} 不是群聊，digest 只支持群",
                      ["用 wx find <关键词> 确认是群", "单人会话请用 wx history"])
    if not args.data_root:
        raise WxError("BAD_ARGUMENT", "必须显式传 --data-root（不允许落在项目目录内）",
                      ["例：--data-root ~/Documents/wechat-digests"])
    script = SKILL_DIR / "scripts/vault_cli.py"
    cmd = [str(VENV_PY), str(script), "digest-source", chat["username"],
           "--data-root", str(Path(args.data_root).expanduser()), "--format", "json"]
    if args.start:
        cmd += ["--start", args.start]
    if args.end:
        cmd += ["--end", args.end]
    if args.since_last:
        cmd += ["--since-last"]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WxError("INTERNAL", "digest-source 失败",
                      ["看 detail"], detail=(proc.stderr or proc.stdout or "")[-800:])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise WxError("INTERNAL", "digest-source 输出不是 JSON", detail=proc.stdout[-500:]) from None
    return payload, {}


def cmd_moments(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    contacts, _ = vc.load_contacts(decrypted)
    usernames = set(args.username or [])
    if args.name:
        q = args.name.lower()
        for c in contacts.values():
            if any(q in str(c.get(f) or "").lower() for f in ("username", "display_name", "remark", "nick_name", "alias")):
                usernames.add(c["username"])
    if not usernames:
        raise WxError("BAD_ARGUMENT", "需要 --name 或 --username",
                      ["先 wx find <人名> 拿 username，避免同名误判"])
    sns_db = decrypted / "sns/sns.db"
    if not sns_db.exists():
        raise WxError("VAULT_MISSING", f"找不到 {sns_db}", ["wx refresh --full"])
    start_ts, end_ts = window(args.since, args.until, None)
    posts = []
    con = vc.connect(sns_db)
    try:
        placeholders = ",".join("?" for _ in usernames)
        for row in con.execute(f"SELECT tid, user_name, content FROM SnsTimeLine WHERE user_name IN ({placeholders})",
                               tuple(usernames)):
            if not row["content"]:
                continue
            try:
                post = vc.parse_moment(row["content"], str(row["tid"]), str(row["user_name"]))
            except Exception:
                continue
            if start_ts and (not post["timestamp"] or post["timestamp"] < start_ts):
                continue
            if end_ts and (not post["timestamp"] or post["timestamp"] > end_ts):
                continue
            if args.keyword and args.keyword.lower() not in json.dumps(post, ensure_ascii=False).lower():
                continue
            post["display_name"] = vc.display_name(post["username"], contacts)
            posts.append(post)
    finally:
        con.close()
    posts.sort(key=lambda p: p["timestamp"], reverse=True)
    return {"count": len(posts[: args.limit]), "moments": posts[: args.limit]}, {}


def cmd_favorites(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    fav_db = decrypted / "favorite/favorite.db"
    if not fav_db.exists():
        raise WxError("VAULT_MISSING", f"找不到 {fav_db}", ["wx refresh --full"])
    contacts, _ = vc.load_contacts(decrypted)
    where, params = [], []
    if args.type:
        where.append("type = ?")
        params.append(vc.FAVORITE_TYPE_FILTERS[args.type])
    if args.query:
        where.append("content LIKE ?")
        params.append(f"%{args.query}%")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = []
    con = vc.connect(fav_db)
    try:
        if not vc.table_exists(con, "fav_db_item"):
            raise WxError("VAULT_MISSING", "favorite.db 没有 fav_db_item 表")
        for row in con.execute(
            f"SELECT local_id, type, update_time, content, fromusr, realchatname FROM fav_db_item "
            f"{where_sql} ORDER BY update_time DESC LIMIT ?", (*params, args.limit)
        ):
            ts = int(row["update_time"] or 0)
            fav_type = int(row["type"] or 0)
            rows.append({
                "id": row["local_id"],
                "type": vc.FAVORITE_TYPE_MAP.get(fav_type, f"type={fav_type}"),
                "time": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                "summary": vc.parse_favorite(row["content"] or "", fav_type),
                "from": vc.display_name(str(row["fromusr"] or ""), contacts) if row["fromusr"] else "",
                "source_chat": vc.display_name(str(row["realchatname"] or ""), contacts) if row["realchatname"] else "",
            })
    finally:
        con.close()
    return {"count": len(rows), "favorites": rows}, {}


def cmd_scan(args) -> tuple[dict, dict]:
    decrypted = ensure_vault()
    if args.find_tables:
        hits = st.find_tables(decrypted, args.find_tables)
        return {"keyword": args.find_tables, "count": len(hits), "hits": hits}, {}
    if not args.type and args.local_type is None:
        raise WxError("BAD_ARGUMENT", "需要 --type / --local-type / --find-tables")
    base_type = args.local_type if args.local_type is not None else vc.MESSAGE_TYPE_FILTERS[args.type][0]
    chat = resolve(args.chat, decrypted) if args.chat else None
    start_ts, end_ts = window(args.since, args.until, None)
    result = st.scan(decrypted, base_type, args.by, chat, start_ts, end_ts)
    result["label"] = args.type or f"local_type={base_type}"
    return result, {}


def cmd_key(args) -> tuple[dict, dict]:
    """检查密钥有效性；--capture 时调用本机的 grab_keys.py（可选，需要人工扫码）。"""
    keys_file = Path("~/.config/wechat-keys.json").expanduser()
    keys = {}
    if keys_file.exists():
        try:
            keys = json.loads(keys_file.read_text())
        except Exception:
            raise WxError("INTERNAL", f"密钥文件不是合法 JSON：{keys_file}",
                          ["重新抓一次：wx key --capture"]) from None

    if args.capture:
        grab = SCRIPT_DIR / "grab_keys.py"
        if not grab.exists():
            raise WxError("DEPENDENCY_MISSING",
                          "本仓库不含抓密钥脚本（它依赖上游实现，不随本项目分发）",
                          ["用你信任的工具抓出 ~/.config/wechat-keys.json",
                           "或按 docs/BACKEND.md 修复上游 extract_keys.py 后直接用它"])
        if not args.yes:
            raise WxError("BAD_ARGUMENT",
                          "抓 key 需要人工扫码登录副本，不能自动执行",
                          ["确认微信已退出后重试并加 --yes",
                           "流程：killall WeChat → bash 工具/微信本地库/prepare-copy.sh → wx key --capture --yes",
                           "看到副本窗口后扫码登录，并在登录后点开朋友圈与收藏"])
        copy = Path("~/Desktop/WeChat.app").expanduser()
        steps = []
        if not copy.exists():
            prep = SCRIPT_DIR / "prepare-copy.sh"
            proc = subprocess.run(["bash", str(prep)], capture_output=True, text=True)
            steps.append({"step": "prepare-copy", "returncode": proc.returncode,
                          "tail": (proc.stdout or "").strip().splitlines()[-2:]})
            if proc.returncode != 0:
                raise WxError("INTERNAL", "复制/重签名副本失败", ["看 steps"], steps=steps)
        proc = subprocess.run([str(VENV_PY), str(grab),
                               "--duration", str(args.duration)],
                              capture_output=True, text=True)
        steps.append({"step": "capture", "returncode": proc.returncode,
                      "tail": (proc.stdout or "").strip().splitlines()[-6:]})
        if proc.returncode != 0:
            raise WxError("INTERNAL", "抓 key 失败", ["看 steps", "确认副本已扫码登录"],
                          steps=steps, stderr_tail=(proc.stderr or "").strip().splitlines()[-5:])
        return {"captured": True, "steps": steps,
                "next": "wx refresh --full"}, {}

    db_base = find_db_base(vc.load_config())
    alias = {
        "message_0": "message/message_0.db", "message_1": "message/message_1.db",
        "message_2": "message/message_2.db", "message_3": "message/message_3.db",
        "message_fts": "message/message_fts.db", "message_resource": "message/message_resource.db",
        "contact": "contact/contact.db", "session": "session/session.db",
        "sns": "sns/sns.db", "favorite": "favorite/favorite.db",
    }
    valid, invalid, missing = [], [], []
    for name, key_hex in keys.items():
        rel = alias.get(name) if name in alias else (name if name.endswith(".db") else None)
        if not rel:
            continue
        src = db_base / rel
        if not src.exists():
            missing.append(rel)
            continue
        try:
            (valid if try_key(src, key_hex) else invalid).append(rel)
        except Exception:
            invalid.append(rel)
    data = {
        "keys_file": str(keys_file),
        "total": len(keys),
        "usable": len(valid),
        "unusable": invalid,
        "source_missing": missing,
        "db_base": str(db_base),
    }
    if invalid:
        data["advice"] = "有密钥失效（常见于微信升级）；跑 wx key --capture --yes 重抓"
    return data, {}


def cmd_serve(args) -> tuple[dict, dict]:
    import wx_serve

    if args.token_show:
        token, _ = wx_serve.ensure_token()
        ip = wx_serve.tailscale_ipv4() or "127.0.0.1"
        return {
            "token": token,
            "token_file": str(wx_serve.TOKEN_FILE),
            "remote": f"http://{ip}:{args.port}",
            "client_env": f'export WX_REMOTE=http://{ip}:{args.port}\nexport WX_TOKEN={token}',
        }, {}
    token, created = wx_serve.ensure_token()
    if created:
        print(f"[wx] 首次启动，已生成访问 token（600）：{wx_serve.TOKEN_FILE}", file=sys.stderr)
    # 前台阻塞运行；正常退出时不返回信封
    code = wx_serve.run_server(args.host, args.port, args.allow_write, args.max_limit,
                               token, args.max_workers, args.timeout,
                               sandbox=not args.no_sandbox)
    raise SystemExit(code)


def cmd_security(args) -> tuple[dict, dict]:
    """审计解密隔离是否真的成立（逐项可复验）。"""
    import wx_serve

    checks: list[dict] = []

    def run(cmd: list[str], expect_fail: bool) -> tuple[bool, str]:
        proc = subprocess.run([str(wx_serve.SANDBOX_EXEC), "-f", str(args.profile)] + cmd,
                              capture_output=True, text=True)
        blocked = proc.returncode != 0 or "Operation not permitted" in (proc.stderr or "")
        return (blocked if expect_fail else not blocked), (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] and (proc.stderr or proc.stdout).strip().splitlines()[-1] or ""

    profile = Path(args.profile).expanduser()
    if not profile.exists():
        raise WxError("BAD_ARGUMENT", f"profile 不存在：{profile}",
                      ["先跑一次 wx serve 生成它，或用 --profile 指定"])

    keys_file = Path("~/.config/wechat-keys.json").expanduser()
    ok, detail = run(["/bin/ls", str(keys_file)], expect_fail=True)
    checks.append({"check": "密钥文件不可读", "ok": ok, "detail": detail,
                   "why": "无密钥即无法解出任何库"})

    config = {}
    try:
        config = json.loads(Path("~/.config/wechat-local-vault.json").expanduser().read_text())
    except Exception:
        pass
    db_base = Path(config.get("db_base_path") or "").expanduser()
    if db_base.parts:
        ok, detail = run(["/bin/ls", str(db_base)], expect_fail=True)
        checks.append({"check": "加密源库不可读", "ok": ok, "detail": detail,
                       "why": "拿不到加密 message_*.db 原文"})

    decrypted, _ = vault_paths()
    ok, detail = run(["/bin/ls", str(decrypted / "session")], expect_fail=False)
    checks.append({"check": "已解密 vault 可读", "ok": ok, "detail": detail,
                   "why": "读能力必需"})

    for path, label in ((Path("~/.config/wechat-local-vault/serve-token").expanduser(), "token 文件 600"),
                        (Path("~/.config/wechat-local-vault/serve-audit.log").expanduser(), "审计日志 600")):
        if not path.exists():
            checks.append({"check": label, "ok": True, "detail": "尚未创建", "why": "-"})
            continue
        mode = oct(path.stat().st_mode & 0o777)
        checks.append({"check": label, "ok": mode == "0o600", "detail": mode, "why": "避免被同机其他用户读取"})

    passed = sum(1 for c in checks if c["ok"])
    return {
        "profile": str(profile),
        "profile_rules": [l for l in profile.read_text().splitlines() if "deny" in l or "subpath" in l][:12],
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "verdict": "解密隔离成立" if passed == len(checks) else "存在未通过项，见 checks",
    }, {}


def cmd_describe(args) -> tuple[dict, dict]:
    data = {
        "name": "wx",
        "version": VERSION,
        "envelope": {
            "ok": {"ok": True, "command": "<name>", "data": "...", "meta": "{vault, decrypted_at, wechat_running, took_ms, vault_age_minutes}"},
            "error": {"ok": False, "command": "<name>", "error": {"code": "<CODE>", "message": "...", "hints": ["..."]}},
        },
        "exit_codes": {"0": "ok", "2": "BAD_ARGUMENT", "3": "CHAT_NOT_FOUND / NO_RESULT",
                       "4": "CHAT_AMBIGUOUS", "5": "VAULT_MISSING / KEY_MISSING",
                       "6": "DEPENDENCY_MISSING", "1": "INTERNAL"},
        "error_codes": ["BAD_ARGUMENT", "CHAT_NOT_FOUND", "CHAT_AMBIGUOUS", "VAULT_MISSING",
                        "KEY_MISSING", "DEPENDENCY_MISSING", "NO_RESULT", "INTERNAL"],
        "message_types": sorted(vc.MESSAGE_TYPE_FILTERS),
        "time_format": ["YYYY-MM-DD", "YYYY-MM-DD HH:MM", "YYYY-MM-DD HH:MM:SS"],
        "commands": COMMANDS,
        "recommended_flow": [
            "wx doctor",
            "wx find <关键词>        # 拿 username，别直接用中文名",
            "wx history <username> --since 2026-11-01 --limit 100",
            "wx stats <username>",
            "wx pull --hours 24",
        ],
        "guardrails": [
            "vault 是快照；meta.vault_age_minutes > 30 时先 wx refresh",
            "export / digest / voice 一律写到仓库外",
            "不要输出 key / salt / 完整 wxid",
        ],
    }
    if args.verify:
        parser_commands = set(SUBCOMMANDS.keys())
        documented = {c["name"] for c in COMMANDS}
        data["verify"] = {
            "missing_in_cli": sorted(documented - parser_commands),
            "undocumented": sorted(parser_commands - documented - {"describe"}),
        }
    return data, {}


# ---------------------------------------------------------------- 命令清单（describe 用）

COMMANDS = [
    {"name": "doctor", "summary": "自检：vault、密钥、分片、新鲜度",
     "args": [], "returns": "vault_ok, decrypted_at, vault_age_minutes, keys, databases"},
    {"name": "key", "summary": "校验密钥有效性；--capture 走抓取流程（需人工扫码）",
     "args": ["--capture", "--yes", "--duration"],
     "returns": "total, usable, unusable[], source_missing[] | captured, steps[]"},
    {"name": "refresh", "summary": "增量/全量解密（会写 vault）",
     "args": ["--full"], "returns": "mode, decrypted_at, tail"},
    {"name": "chats", "summary": "最近会话列表（等价 sessions/unread）",
     "args": ["--unread", "--limit", "--hours", "--since", "--until"],
     "returns": "chats[{chat,username,is_group,unread,last_message,msg_type,sender,time,timestamp}]"},
    {"name": "find", "summary": "按关键词找会话，返回候选与 username（消歧用）",
     "args": ["<query>", "--limit"], "returns": "candidates[{display_name,username,is_group,remark}], ambiguous"},
    {"name": "history", "summary": "单会话消息，按时间窗/类型过滤",
     "args": ["<chat>", "--since", "--until", "--hours", "--type", "--limit", "--offset", "--media"],
     "returns": "messages[{db,table,local_id,type,sender,timestamp,time,content}]"},
    {"name": "search", "summary": "关键词检索，自动按时间切片避免漏检",
     "args": ["<keyword>", "--chat(可重复)", "--type", "--since", "--until", "--hours",
              "--limit", "--slice-days", "--per-slice", "--max-scan"],
     "returns": "messages[]（含 chat/chat_username）, total_matched, partial"},
    {"name": "stats", "summary": "会话统计：总量/类型/发言排行/24 小时分布",
     "args": ["<chat>", "--since", "--until", "--hours"],
     "returns": "total, type_breakdown, top_senders, hourly"},
    {"name": "export", "summary": "导出会话为 markdown/txt（写文件）",
     "args": ["<chat>", "--doc-format markdown|txt", "--out", "--inline", "--since", "--until", "--type", "--limit"],
     "returns": "path, count, bytes"},
    {"name": "pull", "summary": "所有会话在时间窗内的消息（一次拿全网）",
     "args": ["--hours", "--since", "--until", "--limit-per-chat", "--min-count", "--save", "--out"],
     "returns": "window, totals, chats[{chat,count,messages[]}]"},
    {"name": "calls", "summary": "通话记录：时间/时长/结果/发起方",
     "args": ["<chat>", "--since", "--until", "--hours", "--duration-min", "--limit"],
     "returns": "summary{count,answered,total_hours,outcomes,initiators}, calls[]"},
    {"name": "voice", "summary": "语音导出为 MP3/WAV + manifest（写文件，供外部 ASR）",
     "args": ["<chat> | --all-chats", "--since", "--until", "--limit", "--out", "--audio-format mp3|wav", "--dry-run", "--force"],
     "returns": "pending, by_chat[], result{exported,skipped_existing,failed,manifest}"},
    {"name": "digest", "summary": "群聊摘要素材包（必须 --data-root）",
     "args": ["<chat>", "--start", "--end", "--since-last", "--data-root", "--limit"],
     "returns": "folder, source_json, source_markdown, message_count"},
    {"name": "moments", "summary": "朋友圈",
     "args": ["--name", "--username", "--since", "--until", "--keyword", "--limit"],
     "returns": "moments[{time,content,media[],links[]}]"},
    {"name": "favorites", "summary": "收藏夹",
     "args": ["--type text|image|article|card|video", "--query", "--limit"],
     "returns": "favorites[{id,type,time,summary,from,source_chat}]"},
    {"name": "scan", "summary": "跨会话扫某类消息 + 同期总量对照；或扫表名/列名",
     "args": ["--type", "--local-type", "--chat", "--by year|month|day", "--find-tables"],
     "returns": "periods[{period,matched,messages}], totals, top_chats"},
    {"name": "serve", "summary": "通过 Tailscale 暴露 HTTP 接口给其他设备（只读默认）",
     "args": ["--host", "--port", "--allow-write", "--max-limit", "--token-show"],
     "returns": "listen, read_only, token_file, endpoints"},
    {"name": "security", "summary": "审计解密隔离是否成立（沙箱逐项验证）",
     "args": ["--profile"], "returns": "checks[{check,ok,detail}], passed, total, verdict"},
    {"name": "describe", "summary": "机器可读的能力清单（Agent 自发现入口）",
     "args": ["--verify"], "returns": "commands[], envelope, exit_codes, guardrails"},
]

SUBCOMMANDS: dict[str, object] = {}


# ---------------------------------------------------------------- 参数解析

def build_parser() -> Parser:
    p = Parser(prog="wx", description="微信本地聊天记录 CLI（Agent 优先，默认 JSON）",
               epilog="先跑 wx describe 获取机器可读的能力清单。")
    p.add_argument("--format", choices=["json", "text"], default="json", help="默认 json")
    p.add_argument("--compact", action="store_true", help="JSON 不缩进")
    p.add_argument("--version", action="version", version=f"wx {VERSION}")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add(name: str, func):
        sp = sub.add_parser(name)
        # 与全局 --format 同 dest：写在子命令后面也生效；默认 SUPPRESS 以保留全局值
        sp.add_argument("--format", choices=["json", "text"], default=argparse.SUPPRESS,
                        help="输出格式（同全局 --format）")
        sp.set_defaults(func=func, command=name)
        SUBCOMMANDS[name] = sp
        return sp

    add("doctor", cmd_doctor)

    sp = add("key", cmd_key)
    sp.add_argument("--capture", action="store_true", help="抓 key（需人工扫码）")
    sp.add_argument("--yes", action="store_true", help="确认已退出微信，允许复制并重签名副本")
    sp.add_argument("--duration", type=int, default=900, help="抓取秒数")

    sp = add("refresh", cmd_refresh)
    sp.add_argument("--full", action="store_true", help="全量解密（默认增量）")

    sp = add("chats", cmd_chats)
    sp.add_argument("--unread", action="store_true")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--hours", type=int)
    sp.add_argument("--since")
    sp.add_argument("--until")

    sp = add("find", cmd_find)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)

    sp = add("history", cmd_history)
    sp.add_argument("chat")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--hours", type=int)
    sp.add_argument("--type", choices=sorted(vc.MESSAGE_TYPE_FILTERS))
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--media", action="store_true", help="附带本地文件路径提示")

    sp = add("search", cmd_search)
    sp.add_argument("keyword")
    sp.add_argument("--chat", action="append", help="限定会话，可重复")
    sp.add_argument("--type", choices=sorted(vc.MESSAGE_TYPE_FILTERS))
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--hours", type=int)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--slice-days", type=int, default=7, help="切片天数，默认 7")
    sp.add_argument("--per-slice", type=int, default=5000, help="每个切片每会话候选上限")
    sp.add_argument("--max-scan", type=int, default=300000, help="全库检索扫描上限（触发则 partial=true）")

    sp = add("stats", cmd_stats)
    sp.add_argument("chat")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--hours", type=int)

    sp = add("export", cmd_export)
    sp.add_argument("chat")
    sp.add_argument("--doc-format", choices=["markdown", "txt"], default="markdown",
                    help="导出文件格式（全局 --format 控制 JSON/text 输出）")
    sp.add_argument("--out")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--hours", type=int)
    sp.add_argument("--type", choices=sorted(vc.MESSAGE_TYPE_FILTERS))
    sp.add_argument("--limit", type=int, default=5000)
    sp.add_argument("--media", action="store_true")
    sp.add_argument("--inline", action="store_true",
                    help="不写文件，直接把正文放进 data.content（远程场景用）")

    sp = add("pull", cmd_pull)
    sp.add_argument("--hours", type=int, default=24)
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--limit-per-chat", type=int, default=200)
    sp.add_argument("--min-count", type=int, default=1)
    sp.add_argument("--save", action="store_true", help="额外落盘 json+md")
    sp.add_argument("--out", default=str(DEFAULT_EXPORTS))

    sp = add("calls", cmd_calls)
    sp.add_argument("chat")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--hours", type=int)
    sp.add_argument("--duration-min", type=int, default=0)
    sp.add_argument("--limit", type=int, default=0)

    sp = add("voice", cmd_voice)
    sp.add_argument("chat", nargs="?")
    sp.add_argument("--all-chats", action="store_true")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--out", default=str(Path("~/Documents/wechat-local-vault/voice").expanduser()))
    sp.add_argument("--audio-format", choices=["mp3", "wav"], default="mp3",
                    help="音频格式（全局 --format 控制 JSON/text 输出）")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")

    sp = add("digest", cmd_digest)
    sp.add_argument("chat")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--since-last", action="store_true")
    sp.add_argument("--data-root")
    sp.add_argument("--limit", type=int, default=5000)

    sp = add("moments", cmd_moments)
    sp.add_argument("--name")
    sp.add_argument("--username", action="append")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--keyword")
    sp.add_argument("--limit", type=int, default=50)

    sp = add("favorites", cmd_favorites)
    sp.add_argument("--type", choices=sorted(vc.FAVORITE_TYPE_FILTERS))
    sp.add_argument("--query")
    sp.add_argument("--limit", type=int, default=20)

    sp = add("scan", cmd_scan)
    sp.add_argument("--type", choices=sorted(vc.MESSAGE_TYPE_FILTERS))
    sp.add_argument("--local-type", type=int)
    sp.add_argument("--chat")
    sp.add_argument("--by", choices=["year", "month", "day"], default="year")
    sp.add_argument("--find-tables", metavar="KEYWORD")
    sp.add_argument("--since")
    sp.add_argument("--until")

    sp = add("serve", cmd_serve)
    sp.add_argument("--host", help="绑定地址；默认自动用 Tailscale IPv4，取不到则 127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--allow-write", action="store_true", help="允许 refresh/export/digest/voice")
    sp.add_argument("--max-limit", type=int, default=2000, help="--limit 类参数的服务端上限")
    sp.add_argument("--token-show", action="store_true", help="打印当前 token 后退出")
    sp.add_argument("--max-workers", type=int, default=4, help="并发上限（子进程数）")
    sp.add_argument("--no-sandbox", action="store_true",
                    help="关闭 reader 沙箱（不推荐：会让服务进程可读密钥）")
    sp.add_argument("--timeout", type=int, default=300, help="单请求超时秒数")

    sp = add("security", cmd_security)
    sp.add_argument("--profile",
                    default=str(Path("~/.config/wechat-local-vault/reader.sb").expanduser()))

    sp = add("describe", cmd_describe)
    sp.add_argument("--verify", action="store_true", help="检查文档与 CLI 是否一致")
    return p


def execute(argv: list[str]) -> tuple[int, dict, str]:
    """执行 argv，返回 (exit_code, envelope, format_hint)。不打印任何东西。"""
    started = time.time()
    parser = build_parser()
    command = next((a for a in argv if not a.startswith("-")), "argv")

    if READER_MODE:
        reason = READER_FORBIDDEN.get(command)
        if reason:
            return 9, {"ok": False, "command": command,
                       "error": {"code": "READER_FORBIDDEN", "message": reason,
                                 "hints": ["在本机终端执行该命令",
                                           "远程可用：history/search/stats/pull/calls/export --inline"]},
                       "meta": base_meta(started)}, "json"
    try:
        args = parser.parse_args(argv)
        command = args.command
        data, extra_meta = args.func(args)
        return 0, out_envelope(command, data, started, extra_meta), getattr(args, "format", "json")
    except WxError as err:
        return err.exit_code, {"ok": False, "command": command, "error": err.to_dict(),
                               "meta": base_meta(started)}, "json"
    except KeyboardInterrupt:
        return 130, {"ok": False, "command": command,
                     "error": {"code": "INTERRUPTED", "message": "用户中断"}}, "json"
    except SystemExit as exc:  # argparse --help / --version
        code = exc.code if isinstance(exc.code, int) else 0
        return code, {"ok": code == 0, "command": command,
                      "data": {"note": "help/version 只在本地终端输出"}}, "json"
    except Exception as exc:  # noqa: BLE001
        return 1, {"ok": False, "command": command,
                   "error": {"code": "INTERNAL", "message": str(exc),
                             "hints": ["加 --format text 看是否可读", "检查 vault 完整性：wx doctor"],
                             "traceback": traceback.format_exc().splitlines()[-6:]},
                   "meta": base_meta(started)}, "json"


def split_transport(argv: list[str]) -> tuple[str, str, list[str]]:
    """从 argv 里摘出 --remote / --token，其余原样保留。

    `serve` 必须在本机运行：即使设置了 WX_REMOTE 也不能把自己转发给自己。
    """
    first = next((a for a in argv if not a.startswith("-")), "")
    remote = "" if first == "serve" else os.environ.get("WX_REMOTE", "").rstrip("/")
    token = os.environ.get("WX_TOKEN", "") if remote else ""
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--remote" and i + 1 < len(argv):
            remote = argv[i + 1].rstrip("/")
            i += 2
        elif a.startswith("--remote="):
            remote = a.split("=", 1)[1].rstrip("/")
            i += 1
        elif a == "--token" and i + 1 < len(argv):
            token = argv[i + 1]
            i += 2
        elif a.startswith("--token="):
            token = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return remote, token, rest


def print_envelope(envelope: dict, fmt: str, compact: bool) -> None:
    stream = sys.stdout if envelope.get("ok") else sys.stderr
    if fmt == "text" and envelope.get("ok"):
        print(render_text(envelope["command"], envelope["data"]))
        meta = envelope["meta"]
        print(f"\n— vault 解密于 {meta.get('decrypted_at') or '未知'}；{meta['took_ms']}ms",
              file=sys.stderr)
    else:
        print(json.dumps(envelope, ensure_ascii=False, indent=None if compact else 2), file=stream)


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    argv = list(sys.argv[1:] if argv is None else argv)
    remote, token, rest = split_transport(argv)
    compact = "--compact" in rest
    rest = [a for a in rest if a != "--compact"]

    command = next((a for a in rest if not a.startswith("-")), "")
    if command == "serve":
        rest = [a for a in rest if a not in ("--remote", "--token")]
        code, envelope, fmt = execute(rest)
        print_envelope(envelope, fmt, compact)
        return code

    if remote:
        import wxc
        return wxc.main(["--remote", remote] + (["--token", token] if token else []) + rest)

    code, envelope, fmt = execute(rest)
    print_envelope(envelope, fmt, compact)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
