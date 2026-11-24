#!/usr/bin/env python3
"""跨会话扫描某一类消息，并给出「同期总消息量」作为对照。

用途：判断「某类记录在某段时间缺失」到底是**没有发生**，还是**客户端没落库/没迁移**。
做法：不只数目标类型，同时数同期全部消息，二者并列看。

用法：
  # 全账号：通话记录按年分布 + 同期消息总量
  python3 tools/scan_types.py --type call --by year

  # 单会话：语音消息按月
  python3 tools/scan_types.py --type voice --chat "<chat>" --by month

  # 原始类型号（可绕过标签映射）
  python3 tools/scan_types.py --local-type 11000 --by month

  # 找「这类数据还可能存在哪里」：扫描所有库的表名列名
  python3 tools/scan_types.py --find-tables voip

输出：text（默认）或 --format json。只读，不写文件。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import os
import sys
from collections import Counter
from pathlib import Path

SKILL_DIR = Path(os.environ.get("WX_SKILL_DIR") or ".").expanduser().resolve()
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import vault_cli as vc  # noqa: E402

PERIOD_FMT = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}


def shard_paths(decrypted_dir: Path) -> list[Path]:
    return sorted((decrypted_dir / "message").glob("message_*.db"))


def chat_tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]


def username_for(table: str, name2id: dict[int, str] | None = None, contacts: dict[str, dict] | None = None) -> str:
    target = table[4:]
    for username in (contacts or {}):
        if hashlib.md5(username.encode()).hexdigest() == target:
            return username
    for username in (name2id or {}).values():
        if hashlib.md5(str(username).encode()).hexdigest() == target:
            return username
    return ""


def scan(decrypted_dir: Path, base_type: int, by: str, chat: dict | None, start_ts: int | None, end_ts: int | None) -> dict:
    fmt = PERIOD_FMT[by]
    contacts, _ = vc.load_contacts(decrypted_dir)
    target_table = vc.message_table(chat["username"]) if chat else None
    per_period = Counter()
    total_per_period = Counter()
    per_chat = Counter()
    first_ts: int | None = None
    last_ts: int | None = None
    scanned = 0

    for db in shard_paths(decrypted_dir):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = chat_tables(con)
            name2id = {}
            if "Name2Id" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
                name2id = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
            for table in tables:
                if target_table and table != target_table:
                    continue
                scanned += 1
                where, params = [], []
                if start_ts is not None:
                    where.append("create_time >= ?")
                    params.append(start_ts)
                if end_ts is not None:
                    where.append("create_time <= ?")
                    params.append(end_ts)
                clause = f"WHERE {' AND '.join(where)}" if where else ""
                sql = (
                    f"SELECT strftime('{fmt}', create_time, 'unixepoch', 'localtime') AS p, "
                    f"COUNT(*) AS total, "
                    f"SUM(CASE WHEN (local_type & 4294967295) = ? THEN 1 ELSE 0 END) AS hit "
                    f"FROM [{table}] {clause} GROUP BY p"
                )
                try:
                    rows = con.execute(sql, [base_type, *params]).fetchall()
                except sqlite3.Error:
                    continue
                if not target_table:
                    username = username_for(table, name2id, contacts)
                    display = contacts.get(username, {}).get("display_name") or username or table
                for period, total, hit in rows:
                    if not period:
                        continue
                    total_per_period[period] += total
                    if hit:
                        per_period[period] += hit
                        if not target_table:
                            per_chat[display] += hit
                if target_table and not start_ts and not end_ts:
                    row = con.execute(
                        f"SELECT MIN(create_time), MAX(create_time) FROM [{table}] WHERE (local_type & 4294967295) = ?",
                        (base_type,),
                    ).fetchone()
                    if row[0]:
                        first_ts = row[0] if first_ts is None else min(first_ts, row[0])
                        last_ts = row[1] if last_ts is None else max(last_ts, row[1])
                elif not target_table:
                    row = con.execute(
                        f"SELECT MIN(create_time), MAX(create_time) FROM [{table}] WHERE (local_type & 4294967295) = ?"
                        + (" AND create_time >= ?" if start_ts else "")
                        + (" AND create_time <= ?" if end_ts else ""),
                        [base_type, *([start_ts] if start_ts else []), *([end_ts] if end_ts else [])],
                    ).fetchone()
                    if row[0]:
                        first_ts = row[0] if first_ts is None else min(first_ts, row[0])
                        last_ts = row[1] if last_ts is None else max(last_ts, row[1])
        finally:
            con.close()

    periods = sorted(set(total_per_period) | set(per_period))
    return {
        "scope": f"chat:{chat['display_name']}" if chat else "all-chats",
        "base_type": base_type,
        "by": by,
        "tables_scanned": scanned,
        "totals": {
            "matched": sum(per_period.values()),
            "messages_in_scope": sum(total_per_period.values()),
            "first": dt.datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M:%S") if first_ts else "",
            "last": dt.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "",
        },
        "periods": [
            {"period": p, "matched": per_period.get(p, 0), "messages": total_per_period.get(p, 0)}
            for p in periods
        ],
        "top_chats": [{"chat": c, "count": n} for c, n in per_chat.most_common(15)],
    }


def find_tables(decrypted_dir: Path, keyword: str) -> list[dict]:
    hits = []
    kw = keyword.lower()
    for db in sorted(decrypted_dir.rglob("*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if kw in str(name).lower():
                    try:
                        rows = con.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
                    except sqlite3.Error:
                        rows = None
                    hits.append({"db": str(db.relative_to(decrypted_dir)), "table": name, "rows": rows, "matched_on": "table"})
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                try:
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info([{name}])")]
                except sqlite3.Error:
                    continue
                for col in cols:
                    if kw in str(col).lower():
                        hits.append({"db": str(db.relative_to(decrypted_dir)), "table": name, "rows": None, "matched_on": f"column:{col}"})
        finally:
            con.close()
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="跨会话扫描某一类消息 + 同期总量对照")
    parser.add_argument("--type", choices=sorted(vc.MESSAGE_TYPE_FILTERS), help="消息类型名，如 call/voice/image")
    parser.add_argument("--local-type", type=int, help="直接指定 local_type 基础值（与 --type 二选一）")
    parser.add_argument("--chat", help="限定会话（默认全账号）")
    parser.add_argument("--since", help="开始时间 YYYY-MM-DD")
    parser.add_argument("--until", help="结束时间 YYYY-MM-DD")
    parser.add_argument("--by", choices=["year", "month", "day"], default="year", help="时间粒度，默认 year")
    parser.add_argument("--find-tables", metavar="KEYWORD", help="扫描所有库，找表名/列名包含关键词的地方")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    config = vc.load_config()
    decrypted_dir = Path(config["decrypted_dir"]).expanduser()

    if args.find_tables:
        hits = find_tables(decrypted_dir, args.find_tables)
        if args.format == "json":
            print(json.dumps({"keyword": args.find_tables, "hits": hits}, ensure_ascii=False, indent=2))
            return
        print(f"关键词: {args.find_tables} | 命中 {len(hits)} 处")
        for h in hits:
            rows = "?" if h["rows"] is None else h["rows"]
            print(f"  {h['db']}::{h['table']}  rows={rows}  ({h['matched_on']})")
        return

    if args.type is None and args.local_type is None:
        raise SystemExit("需要 --type、--local-type 或 --find-tables")

    base_type = args.local_type if args.local_type is not None else vc.MESSAGE_TYPE_FILTERS[args.type][0]
    label = args.type or f"local_type={base_type}"
    chat = None
    if args.chat:
        contacts, _ = vc.load_contacts(decrypted_dir)
        chat = vc.resolve_chat(args.chat, contacts)
        if not chat:
            raise SystemExit(f"找不到聊天对象: {args.chat}")

    start_ts = vc.parse_time(args.since) if args.since else None
    end_ts = vc.parse_time(args.until, end_of_day=True) if args.until else None
    result = scan(decrypted_dir, base_type, args.by, chat, start_ts, end_ts)

    if args.format == "json":
        result["label"] = label
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    t = result["totals"]
    print(f"类型: {label}（local_type={base_type}）")
    print(f"范围: {result['scope']} | 扫描 {result['tables_scanned']} 张会话表")
    print(f"命中: {t['matched']} 条 | 同期消息总量: {t['messages_in_scope']} 条")
    if t["first"]:
        print(f"最早: {t['first']} | 最晚: {t['last']}")
    print()
    print(f"{'期间':<10} {'命中':>8} {'同期总量':>10}  占比")
    for row in result["periods"]:
        share = f"{row['matched']/row['messages']*100:.2f}%" if row["messages"] else "-"
        flag = "  ← 断档" if row["matched"] == 0 and row["messages"] > 0 else ""
        print(f"{row['period']:<10} {row['matched']:>8} {row['messages']:>10}  {share}{flag}")
    if result["top_chats"]:
        print("\n会话 TOP:")
        for item in result["top_chats"]:
            print(f"  {item['count']:>6}  {item['chat']}")


if __name__ == "__main__":
    main()
