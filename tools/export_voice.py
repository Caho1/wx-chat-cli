#!/usr/bin/env python3
"""导出微信语音消息：SILK_V3 -> WAV/MP3 + manifest，供外部 ASR 使用。

数据来源：vault 里 message/media_0.db 的 VoiceInfo 表
  chat_name_id  -> Name2Id.rowid      （会话）
  create_time   -> 秒级时间戳
  local_id      -> 与 message_<n>.db 的 local_id 对应，可据此取发送者
  voice_data    -> SILK v3（文件头 \x02#!SILK_V3）

用法：
  python3 tools/export_voice.py --chat "群名" --since 2026-11-01 --out ~/Documents/wechat-voice
  python3 tools/export_voice.py --chat "群名" --limit 5 --dry-run
  python3 tools/export_voice.py --all-chats --since 2026-11-20 --format wav

产物（每群一个目录，manifest 可增量续写）：
  <out>/<群名>/<时间>_<local_id>.mp3
  <out>/manifest.jsonl   每行一条：chat/sender/time/duration/path/sha1...
ASR 结果建议写到同目录 <同名>.asr.json，重跑本脚本不会覆盖。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SKILL_DIR = Path(os.environ.get("WX_SKILL_DIR") or ".").expanduser().resolve()
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import pilk  # noqa: E402
import vault_cli as vc  # noqa: E402

DEFAULT_OUT = Path("~/Documents/wechat-local-vault/voice").expanduser()


def safe_name(text: str) -> str:
    text = re.sub(r"[/\\:\*\?\"<>\|\n\r\t]", "_", text).strip()
    return text[:60] or "unknown"


def parse_day(value: str | None) -> int | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间：{value}")


def sender_index(decrypted_dir: Path, chat: dict, start_ts: int | None, end_ts: int | None) -> dict[tuple[int, int], dict]:
    """(local_id, timestamp) -> 消息行，用于补齐发送者。"""
    try:
        rows = vc.collect_history(decrypted_dir, chat, start_ts, end_ts, 200000, 0, "voice")
    except Exception:
        return {}
    index = {}
    for row in rows:
        index[(row.get("local_id"), row.get("timestamp"))] = row
    return index


def decode_to_audio(blob: bytes, out_path: Path, fmt: str) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        silk = Path(tmp) / "a.silk"
        wav = Path(tmp) / "a.wav"
        silk.write_bytes(blob)
        pilk.silk_to_wav(str(silk), str(wav), 24000)
        if fmt == "wav":
            shutil.copyfile(wav, out_path)
        else:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-ac", "1", "-ar", "16000",
                 "-codec:a", "libmp3lame", "-q:a", "4", str(out_path)],
                check=True,
            )
        with wave.open(str(wav)) as w:
            return w.getnframes() / w.getframerate()


def plan(decrypted_dir: Path, chat: dict | None, start_ts: int | None, end_ts: int | None,
         limit: int = 0) -> dict:
    """收集待导出的语音清单（不解码）。返回 rows / by_chat / id_to_user / media_db。"""
    media_db = decrypted_dir / "message/media_0.db"
    if not media_db.exists():
        raise SystemExit(f"找不到 {media_db}，先跑 decrypt_all_dbs.py")

    con = sqlite3.connect(f"file:{media_db}?mode=ro", uri=True)
    try:
        where, params = [], []
        if chat:
            row = con.execute("SELECT rowid FROM Name2Id WHERE user_name = ?", (chat["username"],)).fetchone()
            if not row:
                raise SystemExit(f"Name2Id 里没有 {chat['username']} 的语音")
            where.append("chat_name_id = ?")
            params.append(row[0])
        if start_ts:
            where.append("create_time >= ?")
            params.append(start_ts)
        if end_ts:
            where.append("create_time <= ?")
            params.append(end_ts)
        sql = "SELECT chat_name_id, create_time, local_id, svr_id, length(voice_data) FROM VoiceInfo"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY create_time"
        rows = con.execute(sql, params).fetchall()
        if limit:
            rows = rows[:limit]
        id_to_user = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
    finally:
        con.close()

    by_chat: dict[str, int] = {}
    for chat_id, _, _, _, _ in rows:
        name = id_to_user.get(chat_id, str(chat_id))
        by_chat[name] = by_chat.get(name, 0) + 1
    return {"rows": rows, "by_chat": by_chat, "id_to_user": id_to_user, "media_db": media_db}


def export_records(
    planned: dict,
    chat: dict | None,
    contacts: dict[str, dict],
    out_root: Path,
    fmt: str = "mp3",
    force: bool = False,
    senders: dict | None = None,
) -> dict:
    """按清单解码并写 manifest。返回汇总（不含明文内容）。"""
    rows = planned["rows"]
    id_to_user = planned["id_to_user"]
    media_db = planned["media_db"]
    senders = senders or {}
    con = sqlite3.connect(f"file:{media_db}?mode=ro", uri=True)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.jsonl"
    done = 0
    skipped = 0
    failed = 0
    total_sec = 0.0
    try:
        with manifest_path.open("a") as manifest:
            for chat_id, create_time, local_id, svr_id, size in rows:
                username = id_to_user.get(chat_id, str(chat_id))
                chat_name = contacts.get(username, {}).get("display_name") or username
                blob_row = con.execute(
                    "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND create_time=? AND local_id=? LIMIT 1",
                    (chat_id, create_time, local_id),
                ).fetchone()
                if not blob_row or not blob_row[0]:
                    failed += 1
                    continue
                stamp = dt.datetime.fromtimestamp(create_time).strftime("%Y%m%d-%H%M%S")
                chat_dir = out_root / safe_name(chat_name)
                chat_dir.mkdir(parents=True, exist_ok=True)
                out_path = chat_dir / f"{stamp}_{local_id}.{fmt}"
                if out_path.exists() and not force:
                    skipped += 1
                    continue
                try:
                    duration = decode_to_audio(blob_row[0], out_path, fmt)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! 解码失败 {chat_name} {stamp}_{local_id}: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                manifest.write(json.dumps({
                    "chat": chat_name,
                    "chat_username": username,
                    "sender": (senders.get((local_id, create_time)) or {}).get("sender"),
                    "sender_username": (senders.get((local_id, create_time)) or {}).get("sender_username"),
                    "time": dt.datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S"),
                    "timestamp": create_time,
                    "local_id": local_id,
                    "svr_id": svr_id,
                    "silk_bytes": size,
                    "duration": round(duration, 2),
                    "sha1": hashlib.sha1(blob_row[0]).hexdigest(),
                    "path": str(out_path),
                }, ensure_ascii=False) + "\n")
                done += 1
                total_sec += duration
                if done % 20 == 0:
                    print(f"  ... 已导出 {done} 条 / 累计 {total_sec/60:.1f} 分钟音频", flush=True)
    finally:
        con.close()
    return {
        "exported": done,
        "skipped_existing": skipped,
        "failed": failed,
        "total_seconds": round(total_sec, 1),
        "total_minutes": round(total_sec / 60, 1),
        "manifest": str(manifest_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出微信语音消息为 WAV/MP3 + manifest")
    parser.add_argument("--chat", help="群名或联系人名；与 --all-chats 二选一")
    parser.add_argument("--all-chats", action="store_true", help="导出所有会话的语音")
    parser.add_argument("--since", help="开始时间 YYYY-MM-DD [HH:MM[:SS]]")
    parser.add_argument("--until", help="结束时间")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少条（0 = 不限）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--format", choices=["mp3", "wav"], default="mp3")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不解码不落盘")
    parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    parser.add_argument("--verbose", action="store_true", help="统计时同时打印 username")
    args = parser.parse_args()

    if not args.chat and not args.all_chats:
        raise SystemExit("需要 --chat 或 --all-chats")

    config = vc.load_config()
    decrypted_dir = Path(config["decrypted_dir"]).expanduser()
    start_ts, end_ts = parse_day(args.since), parse_day(args.until)
    out_root = Path(args.out).expanduser()
    contacts, _ = vc.load_contacts(decrypted_dir)

    chat = None
    if args.chat:
        chat = vc.resolve_chat(args.chat, contacts)
        if not chat:
            raise SystemExit(f"找不到会话：{args.chat}")

    planned = plan(decrypted_dir, chat, start_ts, end_ts, args.limit)
    rows = planned["rows"]
    by_chat = planned["by_chat"]

    print(f"待导出 {len(rows)} 条语音，涉及 {len(by_chat)} 个会话")
    for name, count in sorted(by_chat.items(), key=lambda x: -x[1])[:10]:
        label = contacts.get(name, {}).get("display_name") or name
        suffix = f"  ({name})" if args.verbose else ""
        print(f"  {count:6d}  {label}{suffix}")
    if args.dry_run:
        return

    senders = sender_index(decrypted_dir, chat, start_ts, end_ts) if chat else {}
    summary = export_records(planned, chat, contacts, out_root, args.format, args.force, senders)
    print(f"完成：{summary['exported']} 条，跳过已存在 {summary['skipped_existing']} 条，"
          f"失败 {summary['failed']} 条，累计音频 {summary['total_minutes']} 分钟")
    print(f"清单：{summary['manifest']}")


if __name__ == "__main__":
    main()
