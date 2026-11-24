#!/usr/bin/env python3
"""拉取**所有会话**在时间窗内的消息，按会话分组落盘或输出 JSON。

与 `vault_cli.py history` 的区别：那个一次只查一个会话。本脚本用
`session.db` 的最近时间戳筛出活跃会话，再逐会话取消息，最后合并排序。

用法：
  python3 tools/latest_messages.py --hours 24
  python3 tools/latest_messages.py --since "2026-11-23 00:00" --until "2026-11-24 23:59"
  python3 tools/latest_messages.py --hours 24 --format json      # 只打印，不落盘

产物（--format md 时）：
  <out>/all-chats-<YYYYmmdd-HHMM>-<tag>.json   结构化数据
  <out>/all-chats-<YYYYmmdd-HHMM>-<tag>.md     按会话分组的可读时间线
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get("WX_SKILL_DIR") or ".").expanduser().resolve()
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import vault_cli as vc  # noqa: E402

DEFAULT_OUT = Path("~/Documents/wechat-local-vault/exports").expanduser()


def latest_manifest_time(vault_dir: Path) -> str:
    manifests = sorted((vault_dir / "manifests").glob("decrypt-*.json"))
    if not manifests:
        return ""
    try:
        return json.loads(manifests[-1].read_text()).get("created_at", "")
    except Exception:
        return ""


def collect_window(
    decrypted_dir: Path,
    vault_dir: Path,
    start_ts: int,
    end_ts: int,
    limit_per_chat: int = 200,
    min_count: int = 1,
) -> dict:
    """收集时间窗内所有活跃会话的消息，返回 payload（不写文件、不打印）。"""
    sessions = vc.session_rows(decrypted_dir, False, None)
    active = [s for s in sessions if s["timestamp"] and s["timestamp"] >= start_ts]

    chats = []
    truncated = 0
    for session in active:
        chat = {
            "username": session["username"],
            "display_name": session["chat"],
            "is_group": session["is_group"],
        }
        rows = vc.collect_history(decrypted_dir, chat, start_ts, end_ts, limit_per_chat, 0, None, False)
        if len(rows) < min_count:
            continue
        is_truncated = bool(rows) and len(rows) >= limit_per_chat and rows[0]["timestamp"] > start_ts
        if is_truncated:
            truncated += 1
        chats.append({
            "chat": session["chat"],
            "username": session["username"],
            "is_group": session["is_group"],
            "count": len(rows),
            "truncated": is_truncated,
            "first": rows[0]["time"] if rows else "",
            "last": rows[-1]["time"] if rows else "",
            "messages": rows,
        })

    chats.sort(key=lambda c: c["last"], reverse=True)
    total = sum(c["count"] for c in chats)
    groups = sum(1 for c in chats if c["is_group"])
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vault_decrypted_at": latest_manifest_time(vault_dir),
        "window": {
            "start": dt.datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "end": dt.datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "hours": round((end_ts - start_ts) / 3600, 1),
        },
        "totals": {
            "messages": total,
            "chats": len(chats),
            "groups": groups,
            "private": len(chats) - groups,
            "truncated_chats": truncated,
            "active_sessions": len(active),
        },
        "chats": chats,
    }


def render_markdown(payload: dict, window_label: str) -> str:
    totals = payload["totals"]
    chats = payload["chats"]
    lines = [
        f"# 全部会话消息 · {window_label}",
        "",
        f"- 窗口: {payload['window']['start']} ~ {payload['window']['end']}",
        f"- vault 解密于: {payload['vault_decrypted_at']}",
        f"- 合计: {totals['messages']} 条 / {len(chats)} 个会话（群 {totals['groups']}，私聊 {totals['private']}）",
        "",
    ]
    for chat in chats:
        tag = "群" if chat["is_group"] else "私聊"
        lines.append(f"## {chat['chat']} [{tag}] · {chat['count']} 条")
        lines.append("")
        lines.append(f"{chat['first']} ~ {chat['last']}" + ("（已截断，只保留最新部分）" if chat["truncated"] else ""))
        lines.append("")
        for row in chat["messages"]:
            who = f"{row['sender']}: " if row.get("sender") else ""
            content = (row.get("content") or "").replace("\n", " ")
            lines.append(f"- {row['time']} [{row['type']}] {who}{content}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取所有会话在时间窗内的消息")
    parser.add_argument("--hours", type=int, default=24, help="回看小时数，默认 24；给了 --since 时忽略")
    parser.add_argument("--since", help="开始时间 YYYY-MM-DD [HH:MM[:SS]]；覆盖 --hours")
    parser.add_argument("--until", help="结束时间 YYYY-MM-DD [HH:MM[:SS]]；默认现在")
    parser.add_argument("--limit-per-chat", type=int, default=200, help="单会话最多取多少条（取最新的）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--min-count", type=int, default=1, help="消息数低于该值的会话不输出")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="md 落盘；json 只打印")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不打印会话明细")
    args = parser.parse_args()

    config = vc.load_config()
    decrypted_dir = Path(config["decrypted_dir"]).expanduser()
    vault_dir = Path(config.get("vault_dir") or "~/Library/Application Support/wechat-local-vault").expanduser()

    now = dt.datetime.now()
    end_ts = vc.parse_time(args.until, end_of_day=True) if args.until else int(now.timestamp())
    if args.since:
        start_ts = vc.parse_time(args.since)
    else:
        start_ts = end_ts - args.hours * 3600
    window_label = (
        f"{dt.datetime.fromtimestamp(start_ts):%Y-%m-%d %H:%M} ~ "
        f"{dt.datetime.fromtimestamp(end_ts):%Y-%m-%d %H:%M}"
    )
    span_hours = round((end_ts - start_ts) / 3600, 1)

    payload = collect_window(decrypted_dir, vault_dir, start_ts, end_ts, args.limit_per_chat, args.min_count)
    payload["window"]["label"] = window_label

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    totals = payload["totals"]
    chats = payload["chats"]
    stamp = now.strftime("%Y%m%d-%H%M")
    tag_name = (
        f"{dt.datetime.fromtimestamp(start_ts):%m%d}-{dt.datetime.fromtimestamp(end_ts):%m%d}"
        if args.since else f"{args.hours}h"
    )
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"all-chats-{stamp}-{tag_name}.json"
    md_path = out_dir / f"all-chats-{stamp}-{tag_name}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload, window_label), encoding="utf-8")

    print(f"vault: {decrypted_dir}")
    print(f"解密于: {payload['vault_decrypted_at']}")
    print(f"窗口: {window_label}（{span_hours} 小时）")
    print(f"活跃会话: {totals['active_sessions']}")
    print(f"消息合计: {totals['messages']} 条 / {len(chats)} 个会话（群 {totals['groups']}，私聊 {totals['private']}）")
    if totals["truncated_chats"]:
        print(f"注意: {totals['truncated_chats']} 个会话超过单会话上限 {args.limit_per_chat} 条，只保留了最新部分")
    print(f"JSON: {json_path}")
    print(f"MD:   {md_path}")
    if not args.quiet:
        print("")
        for chat in sorted(chats, key=lambda c: -c["count"])[:15]:
            tag = "群" if chat["is_group"] else "私聊"
            print(f"  {chat['count']:5d}  [{tag}] {chat['chat']}  (最后 {chat['last'][5:16]})")


if __name__ == "__main__":
    main()
