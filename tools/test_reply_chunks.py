#!/usr/bin/env python3
"""回复拼装单测：回复会不会被拆成多条、要不要带 @、表情包贴在哪一条。

    python tools/test_reply_chunks.py

改了「回复怎么发」相关的东西（`split_reply` / `build_chunks` / `REPLY_SPLIT_LINES` /
`REPLY_MENTION`）之后跑一下：这里把各种组合的消息段形状打出来，一眼能看出
「分成两句」有没有真的分成两条、@ 有没有去掉、图有没有贴错位置。

不连 NapCat、不调模型，几秒出结果；全通过时退出码 0，可以直接挂 CI。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter, MessageSegment  # noqa: E402

nonebot.init()
nonebot.get_driver().register_adapter(ONEBOT_V11Adapter)

from plugins.ai_group import build_chunks, split_reply  # noqa: E402

IMG = Path("C:/fake/表情包/开心/a.jpg")
AT = [MessageSegment.at(12345)]


def shape(chunks) -> list[list[str]]:
    """把消息段压缩成好读的形状：@ / 文本原文 / [IMG]。"""
    out = []
    for chunk in chunks:
        row = []
        for seg in chunk:
            if seg.type == "at":
                row.append("@")
            elif seg.type == "text":
                row.append(seg.data["text"])
            elif seg.type == "image":
                row.append("[IMG]")
            else:
                row.append(seg.type)
        out.append(row)
    return out


CASES = [
    # 说明, 拆好的行, 表情包, @段, 期望形状
    ("不带@ 两行", ["A", "B"], None, [], [["A"], ["B"]]),
    ("带@ 两行", ["A", "B"], None, AT, [["@", " A"], ["B"]]),
    ("不带@ 单行", ["A"], None, [], [["A"]]),
    ("带@ 单行", ["A"], None, AT, [["@", " A"]]),
    ("不带@ 单行+图", ["A"], IMG, [], [["A", "[IMG]"]]),
    ("不带@ 两行+图", ["A", "B"], IMG, [], [["A"], ["B", "[IMG]"]]),
    ("带@ 两行+图", ["A", "B"], IMG, AT, [["@", " A"], ["B", "[IMG]"]]),
    ("不带@ 只有图", [], IMG, [], [["[IMG]"]]),
    ("带@ 只有图", [], IMG, AT, [["@", "[IMG]"]]),
    ("什么都没有", [], None, [], []),
]


def main() -> int:
    ok = 0
    for name, lines, img, mention, want in CASES:
        got = shape(build_chunks(lines, img, mention))
        good = got == want
        ok += good
        extra = "" if good else f"   期望 {want}"
        print(f"{'PASS' if good else 'FAIL'} {name}: {got}{extra}")

    # 分段函数的回归（换行 → 多条；关掉就拼回一条）
    assert split_reply("A\nB", True) == ["A", "B"]
    assert split_reply("A\nB", False) == ["A B"]
    assert split_reply("  \n ", True) == []

    total = len(CASES)
    print(f"--- {ok}/{total} 通过 ---")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
