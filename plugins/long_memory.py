"""长期记忆插件：把群里值得记的事沉淀成「档案」，每次对话带上。

设计要点
--------
1. **档案式**（不是全量历史）：档案 = 若干条结构化事实，体积恒定。
2. **可修正可补充**：合并时允许 ADD / UPDATE / DELETE —— 出现矛盾以新信息为准，
   过时条目直接删掉，不会出现"两条并存互相打架"。
3. **恒定成本**：档案字数有硬上限（ARCHIVE_MAX_CHARS），所以每次对话多带的
   token 数是个常数，不会随使用时间线性膨胀。
4. **省钱的摆法**：档案作为**独立消息插在 system prompt 之后**（位置固定），
   便于命中服务端的「上下文缓存」（缓存命中输入价便宜很多）。

触发时机
--------
- 攒够 CONSOLIDATE_EVERY 条新消息 → 自动合并一次（后台任务，不阻塞聊天）
- 另外由 `plugins/schedule.py` 每天凌晨强制合并一次（兜底）

存储
----
`qq-ai-bot/memory/长期记忆.json`，同时存档案和"待合并"消息（重启不丢）。
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

from config import get_config  # noqa: E402

cfg = get_config()

DATA_DIR = ROOT / "memory"
ARCHIVE_FILE = DATA_DIR / "长期记忆.json"

# ==================== 可调参数（都搬到 config.py / .env 了） ====================
#   MEM_CONSOLIDATE_EVERY   攒够多少条新消息触发一次合并
#   MEM_ARCHIVE_MAX_CHARS   档案总字数上限（逼模型压缩，控住 token）
#   MEM_MAX_USE_CHARS       注入 prompt 时的硬上限
#   MEM_PENDING_KEEP        待合并消息最多留多少条
# 单价也在 .env：PRICE_IN / PRICE_OUT / PRICE_CACHE
CONSOLIDATE_EVERY = cfg.memory.consolidate_every
ARCHIVE_MAX_CHARS = cfg.memory.archive_max_chars
MAX_USE_CHARS = cfg.memory.max_use_chars
PENDING_KEEP = cfg.memory.pending_keep
# ==========================================================================

# 内存态：{群号: [ {id, subject, fact, time}, ... ]}
_archive: dict[str, list[dict]] = {}
# 待合并的新消息：{群号: [ [昵称, 内容, 时间], ... ]}
_pending: dict[str, list[list[str]]] = {}
# 正在合并的群，防止并发重复合并
_busy: set[str] = set()

# 上一次实际调用的 token 用量（供排查/统计用）
last_usage: dict = {}


# ---------------------------------------------------------------- 存储

def _load() -> None:
    global _archive, _pending
    if not ARCHIVE_FILE.exists():
        return
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        _archive = {str(k): list(v) for k, v in (data.get("archive") or {}).items()}
        _pending = {str(k): [list(x) for x in v] for k, v in (data.get("pending") or {}).items()}
        nonebot.logger.info(
            f"[long_memory] 已加载长期记忆：{len(_archive)} 个群，"
            f"{sum(len(v) for v in _archive.values())} 条档案"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[long_memory] 读取失败：{e}")


def _save() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"archive": _archive, "pending": _pending}
        ARCHIVE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[long_memory] 保存失败：{e}")


_load()


# ---------------------------------------------------------------- 读取

def archive_text(gid: int) -> str:
    """把该群的长期记忆档案格式化成一段文字，供拼进 prompt。没有就返回空串。"""
    items = _archive.get(str(gid)) or []
    if not items:
        return ""
    lines = [
        f"[{it.get('id')}] {it.get('subject') or '群友'}：{it.get('fact', '')}"
        for it in items
        if it.get("fact")
    ]
    body = "\n".join(lines)
    if len(body) > MAX_USE_CHARS:
        body = body[:MAX_USE_CHARS] + "\n…"
    return (
        "【长期记忆档案】以下是你（鲸鱼娘）以前记住的、关于这个群和人长期有效的信息。"
        "聊天时自然地用上（比如记得人家的喜好、上次聊过的事），但别刻意背出来、别逐条念。"
        "如果和当前对话冲突，以当前说的为准：\n" + body
    )


def stats(gid: int) -> dict:
    """给日志/测试看的统计。"""
    items = _archive.get(str(gid)) or []
    text = archive_text(gid)
    return {
        "entries": len(items),
        "chars": sum(len(it.get("fact", "")) for it in items),
        "prompt_chars": len(text),
        "pending": len(_pending.get(str(gid)) or []),
    }


# ---------------------------------------------------------------- 写入

def add_pending(gid: int, name: str, text: str) -> None:
    """把一条群消息记入"待合并"缓冲；攒够了就触发合并。"""
    text = (text or "").strip()
    if not text:
        return
    buf = _pending.setdefault(str(gid), [])
    buf.append([name, text[:160], datetime.now().strftime("%m-%d %H:%M")])
    if len(buf) > PENDING_KEEP:
        del buf[: len(buf) - PENDING_KEEP]
    if len(buf) >= CONSOLIDATE_EVERY:
        _trigger(gid)


def _trigger(gid: int) -> None:
    """后台起一个合并任务（不阻塞聊天）。"""
    key = str(gid)
    if key in _busy:
        return
    try:
        asyncio.get_running_loop().create_task(consolidate(gid))
    except RuntimeError:
        pass  # 不在事件循环里（比如测试直接调用），忽略


async def consolidate_all() -> None:
    """把所有有待合并消息的群都合并一遍（每天兜底调用）。"""
    for key in list(_pending.keys()):
        if _pending.get(key):
            await consolidate(int(key))


# ---------------------------------------------------------------- 合并（核心）

MERGE_PROMPT = """你是「记忆整理员」。任务：把一个 QQ 群的【长期记忆档案】和【新增聊天记录】合并。

【要记的】人物身份与昵称对应关系、稳定偏好、习惯、性格、重要约定、长期有效的事实。
【不要记的】日常寒暄（哈哈/晚安/在吗）、一次性话题（今晚吃啥）、玩笑话、没信息的闲扯。

【操作规则】
- 新增 → op="add"
- 与已有条目**矛盾** → op="update"（以新信息为准，**不要两条并存**）
- 已失效/已过时 → op="delete"
- 重复的不要重复记；同一个人同一属性只留一条最新的。
- 每条 fact 不超过 30 字，写清楚"谁 + 什么事"。

【硬性要求】合并后档案总字数不得超过 {max_chars} 字。超了就把最不重要的条目删掉。
只输出 JSON，不要任何解释、不要代码块以外的文字。没有可记的就输出 {{"ops":[]}}。

输出格式：
{{"ops":[{{"op":"add","subject":"张三","fact":"喜欢喝冰美式，讨厌早起"}},{{"op":"update","id":3,"fact":"现在改喝拿铁了"}},{{"op":"delete","id":7}}]}}

========== 已有档案 ==========
{archive}

========== 新增聊天记录 ==========
{new_msgs}
"""


def _extract_json(s: str) -> dict | None:
    if not s:
        return None
    s = re.sub(r"```(?:json)?", "", s).strip().strip("`")
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(s[i : j + 1])
    except Exception:  # noqa: BLE001
        return None


def _archive_lines(gid: int) -> str:
    items = _archive.get(str(gid)) or []
    if not items:
        return "(空)"
    return "\n".join(
        f"[{it['id']}] {it.get('subject') or '群友'}：{it.get('fact', '')}" for it in items
    )


def _norm_fact(subject: str, fact: str) -> tuple[str, str]:
    """规整条目：模型有时会把「张三：」又写进 fact 里，导致显示成「张三：张三：…」。

    - fact 开头是「X：」且 X 与 subject 相同 → 扣掉前缀
    - fact 开头是「X：」而 subject 为空 → 把 X 当作 subject
    """
    m = re.match(r"^([^：:\s]{1,12})[：:]\s*(.+)$", fact, flags=re.S)
    if not m:
        return subject, fact
    prefix, rest = m.group(1), m.group(2).strip()
    if subject and prefix == subject:
        return subject, rest
    if not subject:
        return prefix, rest
    return subject, fact


def _apply_ops(gid: int, ops: list) -> tuple[int, int, int]:
    """执行 ADD/UPDATE/DELETE，返回 (新增, 修改, 删除) 数量。"""
    items = _archive.setdefault(str(gid), [])
    by_id = {it["id"]: it for it in items if isinstance(it.get("id"), int)}
    next_id = max(by_id, default=0) + 1
    a = u = d = 0
    today = datetime.now().strftime("%m-%d")

    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).lower().strip()
        subject, fact = _norm_fact(
            str(op.get("subject", "") or "").strip()[:20],
            str(op.get("fact", "") or "").strip()[:80],
        )

        if kind == "add" and fact:
            if any(it.get("fact") == fact for it in items):
                continue  # 完全重复，跳过
            items.append(
                {
                    "id": next_id,
                    "subject": subject or "群友",
                    "fact": fact,
                    "time": today,
                }
            )
            by_id[next_id] = items[-1]
            next_id += 1
            a += 1

        elif kind == "update":
            try:
                target = by_id.get(int(op.get("id", -1)))
            except (TypeError, ValueError):
                target = None
            if target and fact:
                target["fact"] = fact
                target["time"] = today
                u += 1

        elif kind == "delete":
            try:
                tid = int(op.get("id", -1))
            except (TypeError, ValueError):
                continue
            before = len(items)
            items[:] = [it for it in items if it.get("id") != tid]
            if len(items) < before:
                by_id.pop(tid, None)
                d += 1

    return a, u, d


async def consolidate(gid: int) -> dict:
    """把待合并消息合并进档案（真实的 LLM 调用）。返回本次统计（含 usage / 花费）。"""
    key = str(gid)
    if key in _busy:
        return {}
    buf = _pending.get(key) or []
    if not buf:
        return {}
    _busy.add(key)
    info: dict = {"gid": gid, "input_msgs": len(buf)}
    try:
        from plugins.ai_group import call_ai_raw  # 延迟导入，避免循环依赖

        new_msgs = "\n".join(f"{n}: {t}" for n, t, _ in buf[-120:])
        prompt = MERGE_PROMPT.format(
            max_chars=ARCHIVE_MAX_CHARS,
            archive=_archive_lines(gid),
            new_msgs=new_msgs,
        )
        raw, usage = await call_ai_raw(
            prompt, temperature=0.2, max_tokens=1200
        )
        data = _extract_json(raw)
        ops = (data or {}).get("ops") or []
        a, u, d = _apply_ops(gid, ops)

        # 合并成功 → 清空待合并缓冲；失败则保留（下轮再试）
        if data is not None:
            _pending[key] = []
        _save()

        cost = _usage_cost(usage)
        info.update(
            {
                "added": a, "updated": u, "deleted": d,
                "entries": len(_archive.get(key) or []),
                "usage": usage, "cost": cost, "ops_raw": ops,
            }
        )
        nonebot.logger.info(
            f"[long_memory] 群{gid} 合并完成：+{a} 改{u} 删{d}，"
            f"现有 {len(_archive.get(key) or [])} 条；"
            f"本次输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} tokens"
            f"（约 {cost:.4f} 元）"
        )
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
        nonebot.logger.warning(f"[long_memory] 群{gid} 合并失败：{type(e).__name__}: {e}")
    finally:
        _busy.discard(key)
    return info


# ---------------------------------------------------------------- 计价

# 单价统一在 config.py 里读 .env（PRICE_IN / PRICE_OUT / PRICE_CACHE），
# 这里留个别名是因为过去可能有脚本引用过 _usage_cost。
_usage_cost = cfg.usage_cost
