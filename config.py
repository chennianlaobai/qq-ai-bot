"""统一配置中心 + 启动自检。

为什么有这个文件
----------------
以前参数散在四个地方（`bot.py` 的端口、`ai_group.py` 的模型三件套和上下文参数、
`long_memory.py` 的记忆参数、`schedule.py` 的群号和课表），改一个值要先想半天它在哪，
写错了也只有跑到那一步才炸。现在统一到这里：

- **来源**：`.env`（凭据和旋钮）+ 仓库根目录的 JSON/Markdown 数据文件（人设、课表、人设红线）
- **校验**：类型不对或缺字段立刻报错，报错信息指明「哪个文件哪一项」
- **自检**：`python check.py` 能在**不起机器人**的情况下把环境查一遍

用法
----
```python
from config import get_config, redact
cfg = get_config()
cfg.ai.api_key          # 模型凭据
cfg.context.history_use # 上下文窗口
cfg.persona.prompt      # 人设 prompt
cfg.schedule.timetable  # 课表
```
改配置后**必须重启后端**；人设 / 红线 / 课表这三类数据文件 checkout 后同样要重启。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:  # tools/ 下的脚本不依赖 dotenv 也能跑出可读报错
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
PERSONA_FILE = ROOT / "persona.md"
PERSONA_RULES_FILE = ROOT / "persona_rules.json"
TIMETABLE_FILE = ROOT / "timetable.json"
EMOJI_DIR = ROOT / "表情包"
MEMORY_DIR = ROOT / "memory"
LOG_DIR = ROOT / "logs"

MOODS = ("开心", "无语", "生气", "卖萌", "得意", "委屈", "震惊")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class ConfigError(RuntimeError):
    """配置读不出来或写错时抛出，说明里带着「去哪儿改」"""


# ------------------------------------------------------------------ 取值helper

def _raw(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _required_str(name: str) -> str:
    v = _raw(name)
    return v


def _as_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"环境变量 {name} 不是整数：{raw!r}（改 .env）") from e
    if lo is not None and v < lo:
        raise ConfigError(f"环境变量 {name}={v} 小于允许的下限 {lo}（改 .env）")
    if hi is not None and v > hi:
        raise ConfigError(f"环境变量 {name}={v} 大于允许的上限 {hi}（改 .env）")
    return v


def _as_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw.strip())
    except ValueError as e:
        raise ConfigError(f"环境变量 {name} 不是数字：{raw!r}（改 .env）") from e
    if lo is not None and v < lo:
        raise ConfigError(f"环境变量 {name}={v} 小于允许的下限 {lo}（改 .env）")
    if hi is not None and v > hi:
        raise ConfigError(f"环境变量 {name}={v} 大于允许的上限 {hi}（改 .env）")
    return v


def _as_bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(f"环境变量 {name}={raw!r} 不是布尔值（写 true/false，改 .env）")


def _as_group_set(name: str) -> set[int]:
    raw = _raw(name)
    return {int(x) for x in re.split(r"[,\s]+", raw) if x} if raw else set()


def _load_json(path: Path, what: str) -> dict:
    if not path.exists():
        raise ConfigError(f"缺少{what}文件：{path.name}（从原文件改回来，或新建一份）")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} 不是合法 JSON：第 {e.lineno} 行 {e.msg}") from e


# ------------------------------------------------------------------ 配置块

@dataclass(frozen=True)
class AIConfig:
    api_key: str
    base_url: str
    model: str
    temperature: float
    timeout: float
    max_retries: int
    price_in: float
    price_out: float
    price_cache: float

    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"


@dataclass(frozen=True)
class ContextConfig:
    history_max: int
    history_use: int
    max_len_per_msg: int
    at_window: int


@dataclass(frozen=True)
class MemoryConfig:
    consolidate_every: int
    archive_max_chars: int
    max_use_chars: int
    pending_keep: int


@dataclass(frozen=True)
class PersonaConfig:
    nickname: str
    prompt: str
    prompt_file: Path
    rules: dict
    rules_file: Path


@dataclass(frozen=True)
class EmojiConfig:
    dir: Path
    moods: tuple[str, ...]
    exts: set[str]

    def count(self, mood: str | None = None) -> int:
        folder = self.dir if mood is None else self.dir / mood
        if not folder.is_dir():
            return 0
        return sum(1 for f in folder.iterdir() if f.suffix.lower() in self.exts and f.is_file())

    def count_all(self) -> int:
        """各情绪子文件夹里的总数（根目录散着的图不算进去）。"""
        return sum(self.count(m) for m in self.moods)


@dataclass(frozen=True)
class ScheduleConfig:
    target_group: int
    remind_before: int
    period_start: dict[int, str]
    timetable: dict[int, list[dict]]
    morning: tuple[int, int]
    night: tuple[int, int]


@dataclass(frozen=True)
class ReplyLogConfig:
    path: Path
    max_bytes: int


@dataclass(frozen=True)
class ReplyConfig:
    """回复怎么发出去。人设要求「祝福语正经一句 + 皮一句要分成两句」，
    所以带换行的回复按行拆成多条发（更像真人打字），行与行之间停一下。

    `mention=False`（默认）= 回复不带 @ 对方——被 @ 才会回，但回的时候不艾特，
    更像群里的人搭话；想退回「@某人 + 内容」的老样子就设 `REPLY_MENTION=true`。
    """

    split_lines: bool
    line_gap: float
    mention: bool


@dataclass(frozen=True)
class NetworkConfig:
    """反向 WS 监听地址：NapCat 里填的对端 URL 必须能对上（`ws://host:port/onebot/v11/ws`）。"""

    host: str
    port: int

    @property
    def ws_url(self) -> str:
        h = self.host if self.host not in ("0.0.0.0", "") else "127.0.0.1"
        return f"ws://{h}:{self.port}/onebot/v11/ws"


@dataclass(frozen=True)
class Config:
    ai: AIConfig
    context: ContextConfig
    memory: MemoryConfig
    persona: PersonaConfig
    emoji: EmojiConfig
    schedule: ScheduleConfig
    reply_log: ReplyLogConfig
    reply: ReplyConfig
    network: NetworkConfig
    allow_groups: frozenset[int] = field(default_factory=frozenset)
    env_file: Path = ENV_FILE

    def usage_cost(self, usage: dict) -> float:
        """按当前单价算一次调用的价钱（元）。"""
        if not usage:
            return 0.0
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        pin = (usage.get("prompt_tokens") or 0) - cached
        pout = usage.get("completion_tokens") or 0
        return (pin * self.ai.price_in + cached * self.ai.price_cache
                + pout * self.ai.price_out) / 1_000_000


# ------------------------------------------------------------------ 加载

def _build_ai() -> AIConfig:
    key = _required_str("AI_API_KEY")
    base = _raw("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = _raw("AI_MODEL", "deepseek-v4-flash-ga-260731")
    return AIConfig(
        api_key=key,
        base_url=base,
        model=model,
        temperature=_as_float("AI_TEMPERATURE", 0.95, 0.0, 2.0),
        timeout=_as_float("AI_TIMEOUT", 90.0, 1.0, 600.0),
        max_retries=_as_int("AI_MAX_RETRIES", 3, 0, 10),
        price_in=_as_float("PRICE_IN", 3.0, 0.0),
        price_out=_as_float("PRICE_OUT", 9.0, 0.0),
        price_cache=_as_float("PRICE_CACHE", 0.1, 0.0),
    )


def _build_context() -> ContextConfig:
    hmax = _as_int("HISTORY_MAX", 20, 1, 500)
    huse = _as_int("HISTORY_USE", 8, 1, 500)
    if huse > hmax:
        raise ConfigError(f"HISTORY_USE({huse}) 不能大于 HISTORY_MAX({hmax})（改 .env）")
    return ContextConfig(
        history_max=hmax,
        history_use=huse,
        max_len_per_msg=_as_int("MAX_LEN_PER_MSG", 120, 10, 2000),
        at_window=_as_int("AT_WINDOW", 20, 0, 3600),
    )


def _build_memory() -> MemoryConfig:
    archive_max = _as_int("MEM_ARCHIVE_MAX_CHARS", 800, 100, 100_000)
    use_max = _as_int("MEM_MAX_USE_CHARS", 1000, 100, 100_000)
    if archive_max > use_max:
        raise ConfigError(
            f"MEM_ARCHIVE_MAX_CHARS({archive_max}) 应不大于 MEM_MAX_USE_CHARS({use_max})（改 .env）"
        )
    return MemoryConfig(
        consolidate_every=_as_int("MEM_CONSOLIDATE_EVERY", 30, 2, 1000),
        archive_max_chars=archive_max,
        max_use_chars=use_max,
        pending_keep=_as_int("MEM_PENDING_KEEP", 200, 10, 100_000),
    )


def _build_persona() -> PersonaConfig:
    nickname = _raw("BOT_NICKNAME", "鲸鱼娘")
    if not PERSONA_FILE.exists():
        raise ConfigError(f"缺少人设文件：{PERSONA_FILE.name}（仓库根目录）")
    prompt = PERSONA_FILE.read_text(encoding="utf-8").strip()
    if len(prompt) < 50:
        raise ConfigError(f"{PERSONA_FILE.name} 内容太短（{len(prompt)} 字），是不是被清空了？")
    if nickname not in prompt:
        raise ConfigError(
            f"{PERSONA_FILE.name} 里没有出现昵称「{nickname}」；"
            f"改了 BOT_NICKNAME 就要同步改人设文件"
        )
    rules = _load_json(PERSONA_RULES_FILE, "人设红线")
    return PersonaConfig(
        nickname=nickname, prompt=prompt, prompt_file=PERSONA_FILE,
        rules=rules, rules_file=PERSONA_RULES_FILE,
    )


def _build_schedule() -> ScheduleConfig:
    data = _load_json(TIMETABLE_FILE, "课表")
    try:
        period_start = {int(k): str(v) for k, v in (data.get("period_start") or {}).items()}
        timetable: dict[int, list[dict]] = {}
        for wd, items in (data.get("timetable") or {}).items():
            w = int(wd)
            if not 1 <= w <= 7:
                raise ValueError(f"星期 {w} 越界（1=周一 … 7=周日）")
            rows = []
            for it in items:
                p = int(it["period"])
                if p not in period_start:
                    raise ValueError(f"星期{w} 的第 {p} 节在 period_start 里没有上课时间")
                if not str(it.get("name", "")).strip():
                    raise ValueError(f"星期{w} 第{p}节 缺课程名")
                rows.append({"period": p, "name": it["name"], "location": it.get("location", "")})
            timetable[w] = rows
        for hhmm in period_start.values():
            if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
                raise ValueError(f"上课时间格式应为 HH:MM，收到 {hhmm}")
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"{TIMETABLE_FILE.name} 内容有问题：{e}") from e

    return ScheduleConfig(
        target_group=_as_int("TARGET_GROUP", 0, 0),
        remind_before=_as_int("REMIND_BEFORE", int(data.get("remind_before", 20)), 0, 240),
        period_start=period_start,
        timetable=timetable,
        morning=(int(data.get("morning_hour", 6)), int(data.get("morning_minute", 0))),
        night=(int(data.get("night_hour", 23)), int(data.get("night_minute", 0))),
    )


def load_config(env_file: Path | None = None, reload: bool = False) -> Config:
    """读 `.env` 和数据文件，做类型/范围/交叉校验，返回不可变 Config。"""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE

    path = env_file or ENV_FILE
    if load_dotenv is not None and path.exists():
        load_dotenv(path, override=reload or _CACHE is None)
    elif not path.exists():
        print(f"⚠️  没有找到 {path.name}（照 .env.example 复制一份填好即可继续）")

    max_bytes = _as_int("REPLY_LOG_MAX_BYTES", 5 * 1024 * 1024, 10_000)
    cfg = Config(
        ai=_build_ai(),
        context=_build_context(),
        memory=_build_memory(),
        persona=_build_persona(),
        emoji=EmojiConfig(dir=EMOJI_DIR, moods=MOODS, exts=IMG_EXTS),
        schedule=_build_schedule(),
        reply_log=ReplyLogConfig(path=LOG_DIR / "replies.jsonl", max_bytes=max_bytes),
        reply=ReplyConfig(split_lines=_as_bool("REPLY_SPLIT_LINES", True),
                          line_gap=_as_float("REPLY_LINE_GAP", 0.6, 0.0, 10.0),
                          mention=_as_bool("REPLY_MENTION", False)),
        network=NetworkConfig(host=_raw("HOST", "127.0.0.1") or "127.0.0.1",
                              port=_as_int("PORT", 8080, 1, 65535)),
        allow_groups=frozenset(_as_group_set("ALLOW_GROUPS")),
        env_file=path,
    )
    _CACHE = cfg
    return cfg


_CACHE: Config | None = None


def get_config() -> Config:
    """第一次调用时加载，之后复用。改了 `.env` 就重启，或用 `reload_config()`。"""
    return _CACHE or load_config()


def reload_config() -> Config:
    return load_config(reload=True)


def redact(text: str, cfg: Config | None = None) -> str:
    """日志/报错发出去之前先把凭据抹掉。任何进 nonebot logger 的模型返回都该过一遍。"""
    if not text:
        return text
    cfg = cfg or get_config()
    out = text
    if cfg.ai.api_key:
        out = out.replace(cfg.ai.api_key, "***")
    return re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1***", out)


# ------------------------------------------------------------------ 自检

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    level: str
    detail: str

    @property
    def icon(self) -> str:
        return {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}[self.level]


def _gitignore_has(token: str) -> bool:
    gi = ROOT / ".gitignore"
    if not gi.exists():
        return False
    return token in gi.read_text(encoding="utf-8")


def check_gitignore() -> Check:
    missing = [t for t in (".env", "memory/", "logs/", ".venv/") if not _gitignore_has(t)]
    return Check(
        "敏感文件已忽略",
        FAIL if missing else OK,
        f"未忽略：{', '.join(missing)} ← 密钥/聊天记录会进版本库" if missing
        else ".env / memory/ / logs/ / .venv/ 都写进了 .gitignore",
    )


def check_api_key(cfg: Config) -> Check:
    key = cfg.ai.api_key
    if not key:
        return Check("AI_API_KEY", FAIL, "没读到。检查 .env 里 AI_API_KEY 有没有填、有没有多打空格")
    if len(key) < 12:
        return Check("AI_API_KEY", FAIL, f"长度只有 {len(key)}，不像有效密钥")
    if re.search(r"(你的|示例|xxx)", key, re.I):
        return Check("AI_API_KEY", FAIL, "还是占位符，没换成真密钥")
    masked = key[:6] + "*" * max(0, len(key) - 10) + key[-4:]
    return Check("AI_API_KEY", OK, f"已加载（{masked}）")


def check_base_url(cfg: Config) -> Check:
    url = cfg.ai.base_url
    if not url.startswith(("http://", "https://")):
        return Check("AI_BASE_URL", FAIL, f"{url} 不是合法 URL")
    if url.startswith("http://"):
        return Check("AI_BASE_URL", WARN, "用的 http，密钥会明文过网")
    return Check("AI_BASE_URL", OK, url)


def check_model(cfg: Config) -> Check:
    model = cfg.ai.model
    if not model:
        return Check("AI_MODEL", FAIL, "空")
    if "volces.com" in cfg.ai.base_url and "deepseek-v4-flash" in model and "-ga-" not in model:
        return Check("AI_MODEL", FAIL, f"{model} 少了 -ga- 段（方舟会报 404）")
    return Check("AI_MODEL", OK, model)


def check_persona(cfg: Config) -> Check:
    try:
        rules = cfg.persona.rules.get("rules", [])
        density = cfg.persona.rules.get("density_rules", [])
        scenes = cfg.persona.rules.get("scene_rules", [])
        bad = []
        for r in list(rules) + list(density):
            try:
                re.compile(r["pattern"])
            except re.error as e:
                bad.append(f"{r.get('id')}: {e}")
        for r in scenes:
            for key in ("trigger", "bless", "rough"):
                try:
                    re.compile(r[key])
                except (KeyError, re.error) as e:
                    bad.append(f"{r.get('id')}.{key}: {e}")
        if bad:
            return Check("人设红线", FAIL, "正则写错：" + "; ".join(bad))
        off = sum(1 for r in rules if str(r.get("level", "")).lower() == "off")
        return Check("人设", OK,
                     f"{cfg.persona.prompt_file.name} {len(cfg.persona.prompt)} 字，"
                     f"{cfg.persona.rules_file.name} 逐句红线 {len(rules) - off} 条"
                     f"（放宽 {off}）+ 密度规则 {len(density)} 条 + 场景规则 {len(scenes)} 条")
    except (KeyError, TypeError) as e:
        return Check("人设红线", FAIL, f"{cfg.persona.rules_file.name} 结构不对：{e}")


def check_emoji(cfg: Config) -> Check:
    if not cfg.emoji.dir.is_dir():
        return Check("表情包目录", FAIL, f"不存在：{cfg.emoji.dir}")
    empty = [m for m in cfg.emoji.moods if cfg.emoji.count(m) == 0]
    total = cfg.emoji.count_all()
    if empty:
        return Check("表情包", WARN, f"共 {total} 张，但这些情绪是空的：{'/'.join(empty)}")
    return Check("表情包", OK, f"{total} 张，{len(cfg.emoji.moods)} 种情绪齐全")


def check_writable(d: Path, label: str) -> Check:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check(f"{label}可写", OK, str(d))
    except OSError as e:
        return Check(f"{label}可写", FAIL, str(e))


def check_timetable(cfg: Config) -> Check:
    tt = cfg.schedule.timetable
    n = sum(len(v) for v in tt.values())
    if not n:
        return Check("课表", WARN, f"{TIMETABLE_FILE.name} 是空的，上课提醒不会触发")
    days = "/".join(f"周{w}" for w in sorted(tt))
    return Check("课表", OK, f"{n} 节课（{days}），提前 {cfg.schedule.remind_before} 分钟提醒")


def check_target_group(cfg: Config) -> Check:
    gid = cfg.schedule.target_group
    if not gid:
        return Check("推送群号 TARGET_GROUP", WARN, "没填，早安/晚安/上课提醒发不出去")
    known: set[int] = set()
    f = MEMORY_DIR / "长期记忆.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            known = {int(k) for k in (data.get("archive") or {})}
        except Exception:  # noqa: BLE001
            known = set()
    if known and gid not in known:
        return Check("推送群号 TARGET_GROUP", WARN,
                     f"{gid}，但长期记忆里出现过的群是 {sorted(known)} —— 群号填错了吗？")
    return Check("推送群号 TARGET_GROUP", OK, str(gid))


def check_port(cfg: Config) -> Check:
    import socket

    host = cfg.network.host if cfg.network.host not in ("0.0.0.0", "") else "127.0.0.1"
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, cfg.network.port))
        return Check("监听端口", OK, f"{host}:{cfg.network.port} 空闲；NapCat 反向 WS 填 {cfg.network.ws_url}")
    except OSError as e:
        return Check("监听端口", FAIL, f"{host}:{cfg.network.port} 起不来（{e.strerror}）——多半是上一个后端没关")
    finally:
        s.close()


def check_deps() -> Check:
    # 用 find_spec 而不是真 import：nonebot_plugin_apscheduler 这种插件模块
    # 一 import 就要取 driver，没 init 会直接抛 ValueError，伪装成"依赖缺失"。
    import importlib.util

    missing = []
    for mod in ("nonebot", "httpx", "dotenv", "nonebot_plugin_apscheduler",
                "nonebot.adapters.onebot.v11"):
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except (ImportError, ValueError) as e:
            missing.append(f"{mod}({e})")
    return Check("依赖已安装", FAIL if missing else OK,
                 f"缺：{', '.join(missing)} → 在 .venv 里 pip install -r requirements.txt" if missing
                 else "nonebot / onebot 适配器 / httpx / dotenv / apscheduler 都在")


def ping_provider(cfg: Config) -> Check:
    """真连一次（GET /models），验证凭据可用。默认不跑，加 --ping。"""
    try:
        import httpx
    except ImportError:
        return Check("模型连通性", WARN, "httpx 没装，跳过联网检查")
    try:
        r = httpx.get(
            f"{cfg.ai.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.ai.api_key}"},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        return Check("模型连通性", FAIL, redact(f"{type(e).__name__}: {e}", cfg))
    if r.status_code == 401:
        return Check("模型连通性", FAIL, "401：密钥不对或没权限")
    if r.status_code >= 400:
        return Check("模型连通性", WARN, f"HTTP {r.status_code}：该厂商可能不支持 /models，忽略即可")
    return Check("模型连通性", OK, f"HTTP {r.status_code}，凭据可用")


def run_checks(ping: bool = False, cfg: Config | None = None) -> list[Check]:
    checks: list[Check] = []
    if cfg is None:
        try:
            cfg = get_config()
        except ConfigError as e:
            return [Check("配置加载", FAIL, str(e))]
    checks.append(Check("配置文件", OK if cfg.env_file.exists() else FAIL,
                        f"已加载 {cfg.env_file.name}" if cfg.env_file.exists()
                        else f"缺 {cfg.env_file.name}"))
    checks.append(check_gitignore())
    checks.append(check_api_key(cfg))
    checks.append(check_base_url(cfg))
    checks.append(check_model(cfg))
    checks.append(check_persona(cfg))
    checks.append(check_emoji(cfg))
    checks.append(check_timetable(cfg))
    checks.append(check_target_group(cfg))
    checks.append(check_writable(MEMORY_DIR, "memory 目录"))
    checks.append(check_writable(LOG_DIR, "logs 目录"))
    checks.append(check_port(cfg))
    checks.append(check_deps())
    if ping:
        checks.append(ping_provider(cfg))
    return checks


def report(checks: Iterable[Check]) -> str:
    items = list(checks)
    lines = [f"{c.icon} {c.name}：{c.detail}" for c in items]
    failed = sum(1 for c in items if c.level == FAIL)
    warned = sum(1 for c in items if c.level == WARN)
    lines.append("")
    lines.append(f"— 失败 {failed} · 警告 {warned} · 通过 {len(items) - failed - warned} "
                 f"@ {datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)
