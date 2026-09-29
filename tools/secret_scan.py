#!/usr/bin/env python3
"""全仓敏感信息扫描器（`config.py` 自带的脱敏»之外的第二道闸）。

干什么
------
在**代码、文档、配置文件**里找可能泄漏的凭据与隐私：明文 API Key、Bearer token、
硬编码口令、疑似 QQ 号 / 手机号……输出时匹配内容一律打码，报告本身不再成为泄漏源。

怎么用
------
    python tools/secret_scan.py                 # 扫当前仓库
    python tools/secret_scan.py --wide          # 连历史里已登记的半敏感项（群号）也扫
    python tools/secret_scan.py --json          # 机器可读输出，方便接 CI
    python tools/secret_scan.py --allow  TOOLS_ # 额外放行含某串的行

退出码：命中 ERROR 级返回 1；否则 0。
可通过的前提：本项目根目录下一次 satisfactory 的运行不需要任何第三方库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", "node_modules", ".idea", ".vscode"}
SKIP_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".exe", ".dll", ".zip", ".pdf"}

# 占位符：这些出现在同一个匹配里就不算真密钥
PLACEHOLDER = re.compile(r"你的|your[_-]?key|xxx+|xxxx+|placeholder|example|示例|替换为|填[这里入]|<.*>", re.I)


@dataclass(frozen=True)
class Rule:
    rid: str
    desc: str
    pattern: re.Pattern
    level: str  # ERROR / WARN


def _compile(expr: str) -> re.Pattern:
    return re.compile(expr)


# 说明：模式刻意写宽一点，宁可多报几个 WARN 让你看一眼，也不要漏掉真密钥。
RULES: list[Rule] = [
    Rule("api-key-volc", "火山方舟 Key（ark-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）",
         _compile(r"ark-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "ERROR"),
    Rule("api-key-openai", "OpenAI 风格 Key（sk-…）", _compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "ERROR"),
    Rule("api-key-generic", "疑似通用 API Key（32 位以上随机串）",
         _compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "WARN"),
    Rule("bearer", "写死的 Bearer token", _compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}"), "ERROR"),
    Rule("secret-assign", "代码里硬编码的密钥赋值",
         _compile(r"(?i)(api[_-]?key|secret|password|passwd|access[_-]?token|token)\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"), "ERROR"),
    Rule("qq-group", "QQ 群号 / QQ 号（>= 5 位纯数字，可能是隐私）",
         _compile(r"(?<![0-9A-Za-z\-_.])[1-9][0-9]{5,10}(?![0-9A-Za-z\-_.])"), "WARN"),
    Rule("phone", "疑似手机号", _compile(r"(?<![0-9A-Za-z\-_.])1[3-9][0-9]{9}(?![0-9A-Za-z\-_.])"), "WARN"),
    Rule("private-key", "私钥文件头", _compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "ERROR"),
]

# 这些地方允许出现说明性密钥/编号：一是文档示例，二是运行时数据（已 gitignore，
# 但真密钥落进去仍要报 ERROR）。
NOISY_PARTS = ("docs/维护日志.md", ".env.example", "memory/", "logs/",
               "tools/sample_replies.jsonl")


def iter_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel_path = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel_path.parts):
            continue
        if p.suffix.lower() in SKIP_EXTS:
            continue
        yield p, str(rel_path).replace("\\", "/")


def mask(s: str, keep: int = 4) -> str:
    """把命中串打码：保留前 keep 位，其余用 * 代替。"""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


@dataclass
class Hit:
    file: str
    line: int
    rule: str
    desc: str
    level: str
    masked: str


def scan_file(path: Path, rel: str, allow: str | None) -> list[Hit]:
    if path.name == ".env":  # .env 本来就该放密钥，逐项列没意义；是否被 git 跟交由 check_tracked 管
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits: list[Hit] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if allow and allow in line:
            continue
        for rule in RULES:
            m = rule.pattern.search(line)
            if not m:
                continue
            frag = m.group(0)
            if PLACEHOLDER.search(frag) or PLACEHOLDER.search(line):
                continue
            if any(n in rel for n in NOISY_PARTS) and rule.level == "WARN":
                continue
            hits.append(Hit(rel, lineno, rule.rid, rule.desc, rule.level, mask(frag)))
    return hits


def check_tracked(root: Path) -> list[Hit]:
    """真·致命情况：秘密已经被 git 跟踪了。"""
    if not (root / ".git").exists():
        return []
    import subprocess

    hits: list[Hit] = []
    for path in (".env", "memory", "logs"):
        try:
            r = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                               cwd=root, capture_output=True, text=True)
        except OSError:
            return []
        if r.returncode == 0 and r.stdout.strip():
            hits.append(Hit(path, 0, "git-tracked", "已被 git 跟踪，历史里会一直留着", "ERROR", "-"))
    return hits


def check_gitignore(root: Path) -> list[Hit]:
    """反过来查一层：易漏目录有没有写进 .gitignore。"""
    gi = root / ".gitignore"
    content = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [d for d in (".env", "memory/", "logs/", ".venv/") if d not in content]
    return [Hit(".gitignore", 0, "gitignore-missing", f"未忽略：{', '.join(missing)}", "ERROR", "-")] if missing else []


def main() -> int:
    ap = argparse.ArgumentParser(description="全仓敏感信息扫描")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--allow", default=None, help="含该子串的行直接放行")
    ap.add_argument("--wide", action="store_true", help="连 WARN 级（群号、手机号的疑似命中）也算失败")
    args = ap.parse_args()

    hits: list[Hit] = []
    for f, rel in iter_files(ROOT):
        hits.extend(scan_file(f, rel, args.allow))
    hits.extend(check_gitignore(ROOT))
    hits.extend(check_tracked(ROOT))

    errors = [h for h in hits if h.level == "ERROR"]
    warns = [h for h in hits if h.level == "WARN"]

    if args.json:
        print(json.dumps([asdict(h) for h in hits], ensure_ascii=False, indent=2))
    else:
        print(f"扫描范围：{ROOT}\n")
        if not hits:
            print("✅ 没扫到可疑项")
        else:
            for h in sorted(hits, key=lambda x: (x.level != "ERROR", x.file, x.line)):
                icon = "❌" if h.level == "ERROR" else "⚠️ "
                loc = f"{h.file}:{h.line}" if h.line else h.file
                print(f"{icon} [{h.rule}] {loc}\n     {h.desc}\n     命中（已打码）：{h.masked}")
            print(f"\n合计：ERROR {len(errors)} / WARN {len(warns)}")

    failed = len(errors) > 0 or (args.wide and warns)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
