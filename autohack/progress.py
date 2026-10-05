"""单窗口实时进度条：轮数 / 速度 / 预计剩余 / 上一组耗时与内存。"""

from __future__ import annotations

import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field

FULL = "█"
EMPTY = "─"        # GBK 控制台无法编码 ░，这里用 GBK 安全的字符


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------

def dwidth(text: str) -> int:
    """按终端显示宽度计算字符数（中文/全角算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def cut(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > max_width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "--"
    if value >= 10000:
        return f"{value / 1000:.1f}s"
    if value >= 1:
        return f"{value:.1f}ms"
    return f"{value:.2f}ms"


def fmt_mb(value: float | None) -> str:
    if value is None:
        return "--"
    if value >= 1024:
        return f"{value / 1024:.2f}GB"
    if value >= 1:
        return f"{value:.1f}MB"
    return f"{value * 1024:.0f}KB"


# --------------------------------------------------------------------------
# 单轮信息
# --------------------------------------------------------------------------

@dataclass
class RoundInfo:
    index: int
    hits: int
    std_ms: float
    hack_ms: float
    peak_mb: float | None
    reasons: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    unlimited: bool = False


# --------------------------------------------------------------------------
# 进度条
# --------------------------------------------------------------------------

class ProgressReporter:
    """用 ``\\r`` 覆盖同一行来显示进度，可与普通日志混排。"""

    def __init__(self, total: int = 0, enabled: bool = True,
                 stop_hint: str = "Ctrl+C 停止"):
        self.total = max(0, int(total or 0))       # 0 = 无休止
        self.enabled = enabled
        self.stop_hint = stop_hint
        self.started = time.perf_counter()
        self.round = 0
        self.hits = 0
        self.stats: dict = {}
        self.last: RoundInfo | None = None
        self._printed = 0
        self._last_paint = 0.0

    # ---------------- 基础输出 ----------------
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def speed(self) -> float:
        elapsed = self.elapsed
        if elapsed < 0.3 or self.round <= 0:
            return 0.0
        return self.round / elapsed

    def print(self, text: str = "") -> None:
        """打印一行日志（先擦掉进度条，避免串行）。"""
        self.clear()
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(text.encode("utf-8", "replace").decode(enc, "replace"), flush=True)

    def clear(self) -> None:
        if not self.enabled or self._printed <= 0:
            return
        sys.stdout.write("\r" + " " * self._printed + "\r")
        sys.stdout.flush()
        self._printed = 0

    def _write(self, text: str) -> None:
        if not self.enabled:
            return
        width = shutil.get_terminal_size((120, 30)).columns
        text = cut(text, max(1, width - 1))
        pad = max(0, self._printed - dwidth(text))
        payload = "\r" + text + " " * pad
        self._emit(payload)
        self._printed = dwidth(text)

    @staticmethod
    def _emit(payload: str) -> None:
        try:
            sys.stdout.write(payload)
        except UnicodeEncodeError:                    # 兜底：编码不支持时降级输出
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            sys.stdout.write(payload.encode("utf-8", "replace")
                             .decode(encoding, "replace"))
        sys.stdout.flush()

    # ---------------- 进度条形状 ----------------
    @staticmethod
    def _fixed_bar(fraction: float, width: int) -> str:
        width = max(4, width)
        filled = round(fraction * width)
        return FULL * filled + EMPTY * (width - filled)

    @staticmethod
    def _sweep_bar(now: float, width: int) -> str:
        """无休止模式下的往复动画条。"""
        width = max(4, width)
        span = width * 2
        pos = int(now * 10) % span
        pos = pos if pos < width else span - pos
        return FULL * pos + EMPTY * (width - pos)

    # ---------------- 渲染 ----------------
    def update(self, info: RoundInfo, force: bool = False) -> None:
        self.round = info.index
        self.hits = info.hits
        self.stats = info.stats
        self.last = info

        now = time.perf_counter()
        if not force and now - self._last_paint < 0.1:
            return
        self._last_paint = now
        self._write(self._line(now))

    def _line(self, now: float) -> str:
        elapsed = now - self.started
        speed = self.speed
        unlimited = self.total <= 0

        if unlimited:
            bar = self._sweep_bar(elapsed, 24)
            stage = f"{self.round} 轮"
            mode = "无休止"
        else:
            fraction = min(1.0, self.round / self.total)
            bar = self._fixed_bar(fraction, 24)
            stage = f"{self.round}/{self.total}"
            mode = f"{fraction * 100:5.1f}%"

        eta = (self.total - self.round) / speed if (not unlimited and speed > 0) else 0.0

        # (丢弃优先级, 显示顺序)  —— 数字越大越先被丢掉，0 表示绝不丢弃
        segments = [
            (0, f"[{bar}]"),
            (3, mode),
            (0, stage),
            (1, f"{speed:5.1f} 轮/秒"),
            (3, f"用时 {fmt_duration(elapsed)}"),
            (5, "" if unlimited else f"剩余 {fmt_duration(eta)}"),
            (1, f"命中 {self.hits}" + _stat_brief(self.stats)),
            (0, self._last_detail()),
            (6, self.stop_hint),
        ]
        return _compose([s for s in segments if s[1]])

    def _last_detail(self) -> str:
        info = self.last
        if info is None:
            return "上组 --"
        reason = "+".join(r.split(" ")[0] for r in info.reasons) if info.reasons else "OK"
        return (f"上组 std {fmt_ms(info.std_ms)}·hack {fmt_ms(info.hack_ms)}"
                f"·内存 {fmt_mb(info.peak_mb)}·{reason}")

    def finish(self) -> None:
        """定格最后一帧进度（不擦除），让结束后仍能看到最终状态。"""
        if not self.enabled or self.round <= 0:
            self._printed = 0
            return
        self._last_paint = 0.0
        self._write(self._line(time.perf_counter()))
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._printed = 0


def _stat_brief(stats: dict) -> str:
    bits = [f"{key.upper()}={stats[key]}"
            for key in ("wa", "tle", "mle", "re") if stats.get(key)]
    return " (" + " ".join(bits) + ")" if bits else ""


def _compose(segments: list[tuple[int, str]],
             max_width: int | None = None) -> str:
    """按终端宽度拼装进度行，放不下时从低优先级的片段开始丢。"""
    if max_width is None:
        max_width = max(24, shutil.get_terminal_size((120, 30)).columns - 1)

    items = list(segments)
    while True:
        text = "  ".join(part for _priority, part in items)
        if dwidth(text) <= max_width:
            return text
        droppable = [i for i, (priority, _part) in enumerate(items) if priority > 0]
        if not droppable:
            return cut(text, max_width)
        target = max(droppable, key=lambda i: (items[i][0], -i))
        items.pop(target)
