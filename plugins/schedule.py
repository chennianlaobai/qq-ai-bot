"""定时任务插件：早安 / 晚安 / 上课提醒。

依赖：nonebot-plugin-apscheduler（已在 .venv 安装）
文案由大模型按人设实时生成（非固定语句）。

配置都在别处了，别在这里改：
- 推送群号 `TARGET_GROUP`、提前提醒分钟 `REMIND_BEFORE` → `.env`
- 推送时刻、节次时间、课表 → 仓库根目录 `timetable.json`
"""
import asyncio
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot import require  # noqa: E402

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from config import get_config  # noqa: E402
from plugins.ai_group import call_ai, pick_emoji, split_reply  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import reply_log  # noqa: E402

cfg = get_config()

# ==================== 配置（来自 config.py） ====================

TARGET_GROUP = cfg.schedule.target_group   # 群号（发早安/晚安/上课提醒到这个群）
REMIND_BEFORE = cfg.schedule.remind_before  # 课前提前多少分钟提醒

# 节次 → 上课时间（在 timetable.json 里改）
PERIOD_START = cfg.schedule.period_start

# 课表：星期(1=周一 ... 7=周日) → {节次: (课程名, 地点)}
TIMETABLE = {
    weekday: {row["period"]: (row["name"], row["location"]) for row in rows}
    for weekday, rows in cfg.schedule.timetable.items()
}

MORNING_H, MORNING_M = cfg.schedule.morning
NIGHT_H, NIGHT_M = cfg.schedule.night

# ===============================================================

WEEK = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]


async def _push(text: str, tag: str = "推送") -> None:
    """把生成好的文案发到目标群（若含 [表情:情绪] 标记，则附一张表情包）。

    带换行的文案按行拆成多条发，和群聊回复一个规则（见 `cfg.reply.split_lines`）。
    """
    if not TARGET_GROUP:
        nonebot.logger.warning("[schedule] 还没填 TARGET_GROUP 群号，跳过发送")
        return
    mood = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", text)
    if m:
        mood = m.group(1).strip()
        text = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", text).strip()

    lines = split_reply(text, cfg.reply.split_lines)
    chunks: list[Message] = [Message(ln) for ln in lines] or [Message("")]
    if mood:
        img = pick_emoji(mood)
        if img:
            chunks[-1].append(MessageSegment.image(img))

    try:
        bot = nonebot.get_bot()
        for i, msg in enumerate(chunks):
            await bot.send_group_msg(group_id=TARGET_GROUP, message=msg)
            if i < len(chunks) - 1:
                await asyncio.sleep(cfg.reply.line_gap)
        reply_log.record(group_id=TARGET_GROUP, speaker="", user_msg=tag, reply=text,
                         mood=mood, kind="scheduled")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[schedule] 发送失败：{e}")
        reply_log.record(group_id=TARGET_GROUP, speaker="", user_msg=tag, reply=text,
                         mood=mood, kind="scheduled",
                         error=f"{type(e).__name__}: {e}"[:200])


@scheduler.scheduled_job("cron", hour=MORNING_H, minute=MORNING_M, id="daily_morning")
async def morning() -> None:
    text = await call_ai(
        "（现在是早上 6 点，群里刚醒。请你以群里沙雕群友的口吻发一句早安，"
        "随口扯一句就行，1~2 句话，别用固定模板、别喊口号）"
    )
    await _push(text, "早安")


@scheduler.scheduled_job("cron", hour=NIGHT_H, minute=NIGHT_M, id="daily_night")
async def night() -> None:
    text = await call_ai(
        "（现在是晚上 11 点，该睡了。请你以群里沙雕群友的口吻跟大伙说声晚安，"
        "随口扯一句，1~2 句话，别用固定模板）"
    )
    await _push(text, "晚安")


@scheduler.scheduled_job("cron", hour=4, minute=30, id="daily_memory")
async def daily_memory() -> None:
    """每天凌晨 4:30 把群里攒的消息合并进「长期记忆档案」（兜底）。

    平时攒够 MEM_CONSOLIDATE_EVERY 条就会自动合并一次，这里是保底，防止消息一直没攒够。
    """
    await ltm.consolidate_all()


@scheduler.scheduled_job("cron", minute="*", id="class_reminder")
async def class_reminder() -> None:
    now = datetime.datetime.now()
    today = TIMETABLE.get(now.isoweekday(), {})
    hm = now.strftime("%H:%M")
    for period, (name, loc) in today.items():
        start = PERIOD_START.get(period)
        if not start:
            continue
        sh, sm = map(int, start.split(":"))
        remind_at = datetime.datetime(now.year, now.month, now.day, sh, sm) - datetime.timedelta(
            minutes=REMIND_BEFORE
        )
        if remind_at.strftime("%H:%M") == hm:
            where = f"，地点在{loc}" if loc else ""
            text = await call_ai(
                f"（提醒群里的同学：还有 {REMIND_BEFORE} 分钟要上「{name}」{where}。"
                "请你以群里沙雕群友的口吻提醒一句，1~2 句话，别太正经）"
            )
            await _push(text, f"上课提醒·{name}")
