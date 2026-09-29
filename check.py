#!/usr/bin/env python3
"""启动自检：不起机器人，也能把环境查一遍。

    python check.py            # 常规自检
    python check.py --ping     # 额外真连一次模型接口，验证密钥能用（会消耗一次调用）
    python check.py --json     # 输出机器可读结果
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ConfigError, get_config, redact, report, run_checks  # noqa: E402


def snapshot_json() -> dict:
    try:
        cfg = get_config()
    except ConfigError as e:
        return {"error": str(e)}
    return {
        "model": cfg.ai.model,
        "base_url": cfg.ai.base_url,
        "api_key": redact(cfg.ai.api_key, cfg),
        "temperature": cfg.ai.temperature,
        "nickname": cfg.persona.nickname,
        "context": {"history_max": cfg.context.history_max,
                    "history_use": cfg.context.history_use,
                    "at_window": cfg.context.at_window},
        "memory": {"consolidate_every": cfg.memory.consolidate_every,
                   "archive_max_chars": cfg.memory.archive_max_chars},
        "emoji": {m: cfg.emoji.count(m) for m in cfg.emoji.moods},
        "target_group": cfg.schedule.target_group,
        "reply_log": str(cfg.reply_log.path),
        "allow_groups": sorted(cfg.allow_groups),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="机器人启动自检")
    ap.add_argument("--ping", action="store_true", help="真连一次模型接口验证凭据")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    checks = run_checks(ping=args.ping)

    if args.json:
        print(json.dumps(
            {"checks": [{"name": c.name, "level": c.level, "detail": c.detail} for c in checks],
             "config": snapshot_json()},
            ensure_ascii=False, indent=2))
    else:
        print(report(checks))
        try:
            snap = snapshot_json()
        except Exception as e:  # noqa: BLE001
            snap = {"error": str(e)}
        if "error" not in snap:
            print("\n配置快照：")
            for k, v in snap.items():
                print(f"  · {k} = {v}")

    return 1 if any(c.level == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
