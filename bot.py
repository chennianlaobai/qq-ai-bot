"""入口：先做一遍自检，再起服务。

    python bot.py            # 正常启动（自检有 FAIL 会拦下来）
    python check.py          # 只想体检、不起服务时用这个

自检不通过就别启动了——起一半再炸，排查成本比这高得多。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter  # noqa: E402

from config import FAIL, get_config, report, run_checks  # noqa: E402


def main() -> int:
    try:
        cfg = get_config()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 配置加载失败：{type(e).__name__}: {e}")
        return 1

    checks = run_checks(cfg=cfg)
    print("===== 启动自检 =====")
    print(report(checks))
    print("===================\n")

    failed = [c for c in checks if c.level == FAIL]
    if failed:
        print("❌ 自检没过，为解决上面的问题后再启动。")
        return 1

    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(ONEBOT_V11Adapter)
    nonebot.load_plugins("plugins")
    print(f"🤖 {cfg.persona.nickname} 起来了：{cfg.network.ws_url}")
    nonebot.run(host=cfg.network.host, port=cfg.network.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
