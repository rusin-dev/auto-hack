"""控制台 ANSI 颜色（简单美化）。

非 TTY（管道 / 重定向 / 测试捕获）、``NO_COLOR`` 时自动退回纯文本；
``FORCE_COLOR=1`` 可强制开启（用于抓取带颜色的输出）。

注意：颜色只用于逐行打印的日志与状态文本；进度条帧内部的字段
请使用 rich 的 style 对象，不要塞原始 ANSI（会破坏帧宽测量）。
"""

from __future__ import annotations

import os
import sys

RESET = "\x1b[0m"


def colors_enabled() -> bool:
    """是否应该输出颜色（每次现场判断，重定向 stdout 后立即生效）。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(sys.stdout.isatty())
    except Exception:                                # noqa: BLE001
        return False


def paint(text: str, *codes: int | str) -> str:
    """给 text 包一层 SGR 颜色；关闭颜色时原样返回。"""
    if not codes or not colors_enabled():
        return text
    return "\x1b[" + ";".join(str(code) for code in codes) + "m" + text + RESET


# ---- 语义化配色（克制：标题 / 成功 / 警告 / 错误 / 辅助） ----

def title(text: str) -> str:
    """粗体青色：横幅、章节标题、关键确认行。"""
    return paint(text, 1, 36)


def ok(text: str) -> str:
    """绿色：成功、命中、可用。"""
    return paint(text, 32)


def warn(text: str) -> str:
    """黄色：提示、警告、手动停止。"""
    return paint(text, 33)


def bad(text: str) -> str:
    """红色：错误、中止。"""
    return paint(text, 31)


def dim(text: str) -> str:
    """暗淡：辅助信息、分隔线、标签。"""
    return paint(text, 2)
