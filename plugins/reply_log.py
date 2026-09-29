"""回复记录：把每一轮「谁说了啥 → 机器人回了啥」写成 logs/replies.jsonl。

给 `tools/check_persona.py` 当输入用（顺带给以后的「群聊日报」留素材）。
每条一行 JSON，字段：

    ts          时间（本地）
    kind        reply=被@回复 / scheduled=定时任务推送
    group_id    群号
    speaker     触发这条回复的人
    user_msg    对方说的话（截断 300 字）
    reply       机器人的回复
    mood        解析出的表情包情绪（没有则空）
    images      这条消息带了几张图
    latency_ms  从收到到发出的毫秒数
    error       出错时的摘要（已脱敏，绝不含密钥）

写日志失败绝不能影响聊天，所以异常一律吞掉只告警。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import get_config


def _log_dir_file() -> tuple[Path, Path, int]:
    cfg = get_config()
    return cfg.reply_log.path.parent, cfg.reply_log.path, cfg.reply_log.max_bytes


def record(
    *,
    group_id: int,
    speaker: str,
    user_msg: str,
    reply: str,
    mood: str = "",
    images: int = 0,
    latency_ms: int | None = None,
    kind: str = "reply",
    error: str = "",
) -> None:
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "group_id": int(group_id),
        "speaker": speaker or "",
        "user_msg": (user_msg or "")[:300],
        "reply": reply or "",
        "mood": mood,
        "images": images,
        "latency_ms": latency_ms,
        "error": error or "",
    }
    try:
        dirpath, filepath, max_bytes = _log_dir_file()
        dirpath.mkdir(parents=True, exist_ok=True)
        if filepath.exists() and filepath.stat().st_size > max_bytes:
            filepath.rename(dirpath / f"replies-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
        with filepath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        try:
            import nonebot

            nonebot.logger.warning(f"[reply_log] 写回复日志失败：{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


def read_replies(path: Path | None = None, limit: int = 50, group_id: int | None = None) -> list[dict]:
    """读最后 limit 条记录（可按群过滤）。"""
    dirpath, filepath, _ = _log_dir_file()
    target = path or filepath
    if not target.exists():
        return []
    rows: list[dict] = []
    with target.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if group_id is not None:
        rows = [r for r in rows if r.get("group_id") == group_id]
    return rows[-limit:]
