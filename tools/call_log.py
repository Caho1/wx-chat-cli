#!/usr/bin/env python3
"""导出通话记录（含开始时间、时长、结果、发起方）。

背景：`vault_cli.py history --type call` 会把正文渲染成 `[通话]`，通话摘要其实在
消息表的 `source` 列里，形如 `通话时长 38:59` / `对方已拒绝` / `通话中断 12:04`；
没写到 `source` 的，正文（zstd 压缩的 VoIP XML）里也有 `<msg><![CDATA[...]]>`。

用法：
  python3 tools/call_log.py --chat "<chat>"
  python3 tools/call_log.py --chat "<chat>" --since 2026-09-01 --format csv
  python3 tools/call_log.py --chat "<chat>" --duration-min 60

产物：
  ~/Documents/wechat-local-vault/exports/calls-<chat>-<stamp>.md|csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get("WX_SKILL_DIR") or ".").expanduser().resolve()
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import vault_cli as vc  # noqa: E402

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None

DEFAULT_OUT = Path("~/Documents/wechat-local-vault/exports").expanduser()
CALL_TYPE = 50


def parse_time(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = dt.datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                t = t.replace(hour=23, minute=59, second=59)
            return int(t.timestamp())
        except ValueError:
            pass
    raise SystemExit(f"时间格式不支持: {value}")


def summary_text(source: str | None, content: bytes | None) -> str:
    text = ""
    if source and not source.lstrip().startswith("<msgsource"):
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", source)
        text = (m.group(1) if m else source).strip()
    if not text and content and zstd and isinstance(content, bytes) and content[:4] == bytes.fromhex("28b52ffd"):
        try:
            xml = zstd.ZstdDecompressor().decompress(content, max_output_size=2_000_000).decode("utf-8", "replace")
            m = re.search(r"<!\[CDATA\[(.*?)\]\]>", xml)
            text = (m.group(1) if m else "").strip()
        except Exception:
            text = ""
    return text


def duration_seconds(text: str) -> int | None:
    m = re.search(r"(\d+):(\d+)(?::(\d+))?\s*$", text or "")
    if not m:
        return None
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


def human(sec: int | None) -> str:
    if sec is None:
        return ""
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def outcome_label(text: str) -> str:
    if not text:
        return "未标注"
    if text.startswith("通话时长"):
        return "已接通"
    if text.startswith("通话中断"):
        return "通话中断"
    for key in ("对方已拒绝", "已拒绝", "对方已取消", "已取消", "对方无应答", "未应答",
                "已在其它设备接听", "已在其它设备拒绝", "对方忙线中", "忙线未接听"):
        if key in text:
            return key
    return text[:16]


def self_ids(config: dict) -> set[str]:
    raw = config.get("wxid") or ""
    # 配置里的 wxid 通常来自容器目录名（形如 wxid_xxx_15ee），真实 wxid 不带后缀
    return {raw, re.sub(r"_[0-9a-zA-Z]{2,6}$", "", raw)} - {""}


def collect_calls(
    decrypted_dir: Path,
    config: dict,
    contacts: dict[str, dict],
    chat: dict,
    start_ts: int | None = None,
    end_ts: int | None = None,
    duration_min: int = 0,
    limit: int = 0,
) -> list[dict]:
    """返回通话记录列表（按时间升序）。json 友好，不打印。"""
    table = vc.message_table(chat["username"])
    mine = self_ids(config)

    def person(username: str) -> str:
        if not username:
            return ""
        if username in mine:
            return "我"
        if username == chat["username"]:
            return chat["display_name"]
        return contacts.get(username, {}).get("display_name") or username

    records: list[dict] = []
    for db in sorted((decrypted_dir / "message").glob("message_*.db")):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            if not con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                continue
            # Name2Id 的 rowid 与分片绑定，必须逐库解析，不能跨库复用
            names_db = {row[0]: row[1] for row in con.execute("select rowid, user_name from Name2Id")}
            sql = (
                f"select create_time, source, message_content, real_sender_id from [{table}] "
                "where (local_type & 4294967295) = ?"
            )
            params: list = [CALL_TYPE]
            if start_ts:
                sql += " and create_time >= ?"
                params.append(start_ts)
            if end_ts:
                sql += " and create_time <= ?"
                params.append(end_ts)
            for row in con.execute(sql + " order by create_time", params):
                text = summary_text(row["source"], row["message_content"])
                ts = int(row["create_time"])
                records.append({
                    "timestamp": ts,
                    "time": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    "text": text,
                    "outcome": outcome_label(text),
                    "duration": duration_seconds(text),
                    "duration_human": human(duration_seconds(text)),
                    "initiator": person(names_db.get(row["real_sender_id"], "")),
                })
        finally:
            con.close()

    if duration_min:
        records = [r for r in records if (r["duration"] or 0) >= duration_min * 60]
    records.sort(key=lambda r: r["timestamp"])
    if limit:
        records = records[-limit:]
    return records


def summarize(records: list[dict]) -> dict:
    answered = [r for r in records if r["duration"]]
    total_sec = sum(r["duration"] for r in answered)
    longest = max(answered, key=lambda r: r["duration"]) if answered else None
    outcomes: dict[str, int] = {}
    initiators: dict[str, int] = {}
    for r in records:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        if r["initiator"]:
            initiators[r["initiator"]] = initiators.get(r["initiator"], 0) + 1
    return {
        "count": len(records),
        "answered": len(answered),
        "unanswered": len(records) - len(answered),
        "total_seconds": total_sec,
        "total_hours": round(total_sec / 3600, 2),
        "longest": longest,
        "outcomes": dict(sorted(outcomes.items(), key=lambda x: -x[1])),
        "initiators": dict(sorted(initiators.items(), key=lambda x: -x[1])),
        "first": records[0]["time"] if records else "",
        "last": records[-1]["time"] if records else "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出通话记录")
    parser.add_argument("--chat", required=True, help="会话名或 username")
    parser.add_argument("--since", help="开始时间 YYYY-MM-DD [HH:MM[:SS]]")
    parser.add_argument("--until", help="结束时间")
    parser.add_argument("--duration-min", type=int, default=0, help="只输出时长 ≥ N 分钟的记录")
    parser.add_argument("--format", choices=["md", "csv", "json"], default="md")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--limit", type=int, default=0, help="最多输出多少条（按时间倒序取最新）")
    args = parser.parse_args()

    config = vc.load_config()
    decrypted_dir = Path(config["decrypted_dir"]).expanduser()
    contacts, _ = vc.load_contacts(decrypted_dir)
    chat = vc.resolve_chat(args.chat, contacts)
    if not chat:
        raise SystemExit(f"找不到聊天对象: {args.chat}")

    start_ts = parse_time(args.since)
    end_ts = parse_time(args.until, end_of_day=True)
    records = collect_calls(decrypted_dir, config, contacts, chat, start_ts, end_ts,
                            args.duration_min, args.limit)
    if not records:
        raise SystemExit("没有符合条件的通话记录")

    stats = summarize(records)
    if args.format == "json":
        print(json.dumps({"chat": chat["display_name"], "username": chat["username"],
                          "summary": stats, "calls": records}, ensure_ascii=False, indent=2))
        return

    print(f"会话: {chat['display_name']}（{chat['username']}）")
    print(f"通话记录: {stats['count']} 条 | 已接通: {stats['answered']} 条 | 累计时长: {stats['total_hours']} 小时")
    if stats["longest"]:
        print(f"最长一次: {stats['longest']['time']} · {stats['longest']['duration_human']}")
    print(f"未接通: {stats['unanswered']} 条")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|]", "_", chat["display_name"])[:40]
    out_path = out_dir / f"calls-{safe}-{stamp}.{args.format}"

    if args.format == "csv":
        with out_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["时间", "时长", "秒", "结果", "发起方", "原始摘要"])
            for r in records:
                writer.writerow([r["time"], r["duration_human"], r["duration"] or "",
                                 r["outcome"], r["initiator"], r["text"]])
    else:
        lines = [f"# 通话记录 · {chat['display_name']}", "",
                 f"- 会话: {chat['display_name']}（{chat['username']}）",
                 f"- 条数: {stats['count']}，已接通 {stats['answered']}，累计 {stats['total_hours']} 小时",
                 f"- 导出时间: {dt.datetime.now():%Y-%m-%d %H:%M}", "",
                 "| 时间 | 时长 | 结果 | 发起方 |", "| --- | --- | --- | --- |"]
        for r in records:
            lines.append(f"| {r['time']} | {r['duration_human'] or '-'} | {r['outcome']} | {r['initiator']} |")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"已写出: {out_path}")


if __name__ == "__main__":
    main()
