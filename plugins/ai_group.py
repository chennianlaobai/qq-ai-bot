import asyncio
import random
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from nonebot import on_message  # noqa: E402
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment  # noqa: E402

from config import get_config, redact  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import reply_log  # noqa: E402

cfg = get_config()

# —— 群聊上下文缓存（让机器人能"记住"刚才群里在聊什么）——
# 参数都在 config.py / .env 里（HISTORY_MAX / HISTORY_USE / MAX_LEN_PER_MSG / AT_WINDOW）
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=cfg.context.history_max))

# 每个群"最近一次被 @"的时间戳：@ 之后 AT_WINDOW 秒内发的图，也当成是发给它的
_last_at: dict[int, float] = {}

# 看图时随机挑一个"吐槽角度"注入，逼它每次换个说法（不然模型爱复读同一个梗）
IMAGE_ANGLES = [
    "只挑图里最搞笑、最怪的一个细节说",
    "阴阳怪气一下发图的人",
    "把图里的东西跟群友（或者你自己）对比",
    "假装一时没看懂，随便糊弄一句",
    "夸张地夸上天，或者夸张地嫌弃",
    "联想到一个完全不相关的梗",
    "就发个怪叫或者两三个字（比如「？」「草」「好家伙」「逆天」）",
    "用一个很欠揍的语气评价这张图",
    "假装这图是你自己发的，然后开始瞎解释",
    "从图里挑个东西胡乱开个脑洞",
]

# 人设 prompt 在仓库根目录的 persona.md 里（改完要重启后端）
SYSTEM_PROMPT = cfg.persona.prompt
NICKNAME = cfg.persona.nickname

ai = on_message(priority=10, block=False)
collector = on_message(priority=1, block=False)

# 上游表示"服务繁忙/限流"的错误码，遇到这些值得自动重试
BUSY_CODES = {"1301", "1302", "1303", "1304", "1305"}

# 上一次实际调用的 token 用量（排查 / 统计用）
LAST_USAGE: dict = {}


# ------------------------------------------------------------------ 底层调用

async def _chat(
    messages: list,
    temperature: float | None = None,
    max_tokens: int | None = None,
    retries: int | None = None,
) -> tuple[str, dict]:
    """底层调用：返回 (回复文本, usage)。带限流退避重试。

    报错文本统一过 `redact()` —— 万一上游把请求回显了，密钥也不会写进日志。
    """
    temperature = cfg.ai.temperature if temperature is None else temperature
    retries = cfg.ai.max_retries if retries is None else retries
    last_err = ""
    for attempt in range(retries):
        try:
            payload = {
                "model": cfg.ai.model,
                "messages": messages,
                "thinking": {"type": "disabled"},
                "temperature": temperature,
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
            async with httpx.AsyncClient(timeout=cfg.ai.timeout) as c:
                r = await c.post(
                    cfg.ai.endpoint(),
                    headers={
                        "Authorization": f"Bearer {cfg.ai.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            data = r.json()

            if data.get("choices"):
                usage = data.get("usage") or {}
                LAST_USAGE.clear()
                LAST_USAGE.update(usage)
                return data["choices"][0]["message"]["content"].strip(), usage

            err = data.get("error", {}) or {}
            code = str(err.get("code", ""))
            last_err = redact(f"HTTP {r.status_code} code={code} {err.get('message') or str(data)[:300]}")

            if (code in BUSY_CODES or r.status_code >= 500) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return f"[AI 出错] {last_err}", {}

        except Exception as e:
            last_err = redact(f"{type(e).__name__}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

    return f"[AI 调用失败] {last_err}", {}


async def call_ai_raw(prompt: str, temperature: float = 0.95, max_tokens: int | None = None):
    """给内部工具（如长期记忆整理）用的简化调用：单条 user 消息。返回 (文本, usage)。"""
    return await _chat([{"role": "user", "content": prompt}], temperature, max_tokens)


async def call_ai(
    user_msg: str,
    context: str = "",
    images: list[str] | None = None,
    long_term: str = "",
    retries: int | None = None,
) -> str:
    """调用大模型。

    - context：最近群聊记录
    - images：本次消息里的图片 URL（多模态看图）
    - long_term：长期记忆档案（**紧跟 system prompt**，位置固定便于命中上下文缓存）
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if long_term:
        # 固定位置：system prompt 之后立刻放，且内容变动不频繁 → 有利于服务端前缀缓存
        messages.append({"role": "user", "content": long_term})
    if context:
        messages.append(
            {
                "role": "user",
                "content": "【以下是群里最近的聊天记录，仅供你理解上下文，不用逐条回应】：\n" + context,
            }
        )
    if images:
        content: list = [{"type": "text", "text": user_msg or "（我发了张图，你瞅瞅）"}]
        for u in images:
            content.append({"type": "image_url", "image_url": {"url": u}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_msg})

    text, _ = await _chat(messages, retries=retries)
    return text


def split_reply(text: str, split: bool = True) -> list[str]:
    """把回复正文切成「要分开发的那几段」。

    人设里「祝福语正经一句 + 皮一句要分成两句」这类要求靠换行表达，
    所以带换行的回复按行拆成多条发（像真人打字）；单行回复 = 一段，行为不变。
    `split=False`（`.env` 的 `REPLY_SPLIT_LINES=false`）时拼回一段，退回旧行为。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if split:
        return lines
    return [" ".join(lines)] if lines else []


def build_chunks(lines: list[str], img: Path | None, mention: list) -> list[list]:
    """把「分好的行 + 可选表情包 + 可选 @」拼成要逐条发出去的消息段列表。

    - `mention` 传空列表（默认）= 回复不带 @；传 `[MessageSegment.at(...)]` 则首行加 @。
    - 带 @ 时正文前补一个空格跟 @ 隔开；不带 @ 就直接发正文。
    - 图片（表情包）永远贴在**最后一条**上；只有图没文字时单独发一条。
    """
    chunks: list[list] = []
    if lines:
        head = MessageSegment.text((" " + lines[0]) if mention else lines[0])
        chunks.append(mention + [head])
        chunks += [[MessageSegment.text(ln)] for ln in lines[1:]]
    if img:
        if chunks:
            chunks[-1].append(MessageSegment.image(img))
        else:
            chunks.append(mention + [MessageSegment.image(img)])   # 只有图，没文字
    return chunks


def pick_emoji(mood: str) -> Path | None:
    """按情绪挑一张表情包；对应文件夹没有就退而从「表情包」根目录随机挑。"""
    emoji = cfg.emoji
    if not emoji.dir.is_dir():
        return None
    for folder in (emoji.dir / mood, emoji.dir):
        if folder.is_dir():
            files = [f for f in folder.iterdir() if f.suffix.lower() in emoji.exts and f.is_file()]
            if files:
                return random.choice(files)
    return None


# ------------------------------------------------------------------ 消息处理

@collector.handle()
async def _(event: GroupMessageEvent):
    # 群白名单：ALLOW_GROUPS 留空 = 所有群都理
    if cfg.allow_groups and event.group_id not in cfg.allow_groups:
        return

    # 缓存群里每一条消息（不回复），供 AI 参考上下文
    text = event.get_plaintext().strip()
    has_img = any(seg.type == "image" for seg in event.get_message())
    if not text and not has_img:
        return
    name = event.sender.card or event.sender.nickname or str(event.user_id)
    _history[event.group_id].append((name, text or "[发了一张图片]", False))

    # 同时丢进长期记忆的"待合并"缓冲（攒够了后台自动合并成档案）
    ltm.add_pending(event.group_id, name, text or "[发了一张图片]")


@ai.handle()
async def _(event: GroupMessageEvent):
    if cfg.allow_groups and event.group_id not in cfg.allow_groups:
        return

    gid = event.group_id
    now = time.time()
    started = time.perf_counter()

    user_msg = event.get_plaintext().strip()
    # 收集本条消息里的图片
    images = [
        seg.data.get("url") or seg.data.get("file")
        for seg in event.get_message()
        if seg.type == "image"
    ]
    images = [u for u in images if u]

    if event.is_tome():
        _last_at[gid] = now

    # 回应条件：①被 @ 了；②或没被 @，但刚被 @ 过（AT_WINDOW 秒内）又发了图
    recent_at = (now - _last_at.get(gid, 0)) < cfg.context.at_window
    if not (event.is_tome() or (images and recent_at)):
        return
    if not user_msg and not images:
        return

    speaker = event.sender.card or event.sender.nickname or str(event.user_id)
    raw_msg = user_msg

    # 取该群最近记录，排除最后一条（就是当前这条消息），再截取最近 HISTORY_USE 条
    hist = list(_history[gid])[:-1][-cfg.context.history_use:]
    context = "\n".join(
        f"{f'{NICKNAME}(我自己)' if is_bot else name}: {text[:cfg.context.max_len_per_msg]}"
        for name, text, is_bot in hist
    )

    # 把"当前是谁在说话"标清楚，免得它认错人、前言不搭后语
    if images:
        angle = random.choice(IMAGE_ANGLES)
        cur = f"（{speaker}发了张图。本次吐槽角度：{angle}）"
        if user_msg:
            cur += f"\n{speaker}还说：{user_msg}"
        user_msg = cur
    else:
        user_msg = f"{speaker}说：{user_msg}"

    reply = await call_ai(user_msg, context, images, ltm.archive_text(gid))

    # 提取并移除回复里的 [表情:xxx] 标记
    mood = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", reply)
    if m:
        mood = m.group(1).strip()
        reply = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", reply).strip()

    img = pick_emoji(mood) if mood else None
    if not reply and not img:  # 既没文字也没图 → 别发空消息
        reply_log.record(
            group_id=gid, speaker=speaker, user_msg=raw_msg, reply="", mood=mood,
            images=len(images), latency_ms=int((time.perf_counter() - started) * 1000),
            error="模型没给出可用内容",
        )
        return

    # 分成几条发出去：
    #   人设里「祝福语必须正经一句 + 皮一句分成两句」这类要求，靠换行表达；
    #   带换行时按行拆成多条，中间停一下，像真人打字分两条发（两段塞一条里很出戏）。
    #   默认不带 @（`REPLY_MENTION=false`）——被 @ 才会回，但回的时候不艾特对方，更像群友搭话。
    mention: list = [MessageSegment.at(event.user_id)] if cfg.reply.mention else []
    chunks = build_chunks(split_reply(reply, cfg.reply.split_lines), img, mention)

    for i, seg in enumerate(chunks):
        await ai.send(Message(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(cfg.reply.line_gap)   # 别刷屏式连发

    # 把自己的回复也存进上下文
    _history[gid].append(("", reply or "[只发了一张表情包]", True))

    # 落一份记录，给「人格一致性检查」用
    bad: list[str] = []
    if reply.startswith("[AI "):
        bad.append("API_ERROR")
    reply_log.record(
        group_id=gid, speaker=speaker, user_msg=raw_msg, reply=reply, mood=mood,
        images=len(images), latency_ms=int((time.perf_counter() - started) * 1000),
        error=",".join(bad),
    )
