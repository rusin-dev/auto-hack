"""单窗口实时进度条（基于 rich）：轮数 / 速度 / 预计剩余 / 上一组耗时与内存。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------

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


def _stat_brief(stats: dict) -> str:
    bits = [f"{key.upper()}={stats[key]}"
            for key in ("wa", "tle", "mle", "re") if stats.get(key)]
    return " (" + " ".join(bits) + ")" if bits else ""


# --------------------------------------------------------------------------
# 进度条
# --------------------------------------------------------------------------

class ProgressReporter:
    """用 rich 渲染单行实时进度，可与普通日志混排。

    ``enabled=False`` 时退化成纯文本日志（非 TTY / ``--no-bar``）。
    rich 自己按 ``refresh_per_second`` 在后台重绘，这里每轮只更新数据。
    """

    def __init__(self, total: int = 0, enabled: bool = True,
                 stop_hint: str = "Ctrl+C 停止",
                 console: Console | None = None):
        self.total = max(0, int(total or 0))       # 0 = 无休止
        self.enabled = enabled
        self.stop_hint = stop_hint
        self.started = time.perf_counter()
        self.round = 0
        self.hits = 0
        self.stats: dict = {}
        self.last: RoundInfo | None = None
        self._live = False
        self._task_id = None
        self._progress: Progress | None = None
        if enabled:
            self._progress = self._build(console)

    # ---------------- 构建 ----------------

    def _build(self, console: Console | None) -> Progress:
        unlimited = self.total <= 0
        columns = []
        if unlimited:
            columns.append(SpinnerColumn())              # 无休止模式：转圈提示
        columns += [
            BarColumn(bar_width=24),
            TextColumn("{task.description}"),
        ]
        if not unlimited:
            columns.append(TimeRemainingColumn())
        columns += [
            TimeElapsedColumn(),
            TextColumn("{task.fields[info]}"),
        ]
        return Progress(*columns, console=console,
                        auto_refresh=True, refresh_per_second=10,
                        transient=False)

    # ---------------- 状态 ----------------

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def speed(self) -> float:
        elapsed = self.elapsed
        if elapsed < 0.3 or self.round <= 0:
            return 0.0
        return self.round / elapsed

    def _stage(self) -> str:
        if self.total > 0:
            fraction = min(1.0, self.round / self.total)
            return f"{self.round}/{self.total}  {fraction * 100:5.1f}%"
        return f"{self.round} 轮  无休止"

    def _last_detail(self) -> str:
        info = self.last
        if info is None:
            return "上组 --"
        reason = ("+".join(r.split(" ")[0] for r in info.reasons)
                  if info.reasons else "OK")
        return (f"上组 std {fmt_ms(info.std_ms)}·hack {fmt_ms(info.hack_ms)}"
                f"·内存 {fmt_mb(info.peak_mb)}·{reason}")

    def _info(self) -> str:
        parts = [
            f"{self.speed:5.1f} 轮/秒",
            f"命中 {self.hits}" + _stat_brief(self.stats),
            self._last_detail(),
            self.stop_hint,
        ]
        return "  ".join(part for part in parts if part)

    # ---------------- 输出 ----------------

    def print(self, text: str = "") -> None:
        """打印一行日志；进度条运行时交给 rich 与进度行自动交错。"""
        if self._live and self._progress is not None:
            # 日志里可能有 [第 1 轮]、[0..n) 这类方括号，必须关掉 markup
            self._progress.console.print(text, markup=False, highlight=False)
            return
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(text.encode("utf-8", "replace").decode(enc, "replace"),
                  flush=True)

    def update(self, info: RoundInfo, force: bool = False) -> None:
        """每跑完一轮调用一次；``force`` 保留兼容，重绘交给 rich 的后台刷新。"""
        self.round = info.index
        self.hits = info.hits
        self.stats = info.stats
        self.last = info
        if not self.enabled or self._progress is None:
            return
        if not self._live:
            self._task_id = self._progress.add_task(
                self._stage(), total=self.total or None, info=self._info())
            self._progress.start()
            self._live = True
        self._progress.update(
            self._task_id,
            completed=self.round,
            description=self._stage(),
            info=self._info(),
        )

    def finish(self) -> None:
        """定格最后一帧进度（rich 的 transient=False 会把它留在屏幕上）。"""
        if self._live and self._progress is not None:
            self._progress.update(
                self._task_id,
                completed=self.round,
                description=self._stage(),
                info=self._info(),
            )
            self._progress.stop()
        self._live = False
