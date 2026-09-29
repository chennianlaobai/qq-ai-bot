#!/usr/bin/env python3
"""导入冒烟测试：不起服务，只验证所有插件能被 NoneBot 正常加载。

    python tools/smoke_import.py

改完插件代码先跑这个——比起一半再炸，这里几秒就能知道是 ImportError、
语法错，还是某个上层配置没配好。通过后跑 `python check.py` 看环境，
都没问题了再 `python bot.py`。

注意：它要把当前目录当项目根跑（sys.path 里得有 config.py），
所以在仓库根目录执行。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter  # noqa: E402

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(ONEBOT_V11Adapter)
nonebot.load_plugins("plugins")
print("✅ 所有插件导入通过（不代表能连上 NapCat，那只靠 python bot.py 验证）")
