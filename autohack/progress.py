"""单窗口实时进度条（基于 rich）：轮数 / 速度 / 预计剩余 / 上一组耗时与内存。"""

from __future__ import annotations

import sys
import time
from collections import deque
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
        self._last_size = None                    # 上次已处理的终端尺寸
        self._log_tail: deque[str] = deque(maxlen=200)   # 最近的日志，resize 后重放
        self._cand = None                         # 最近观察到的候选尺寸
        self._cand_since = 0.0                    # 该候选尺寸首次观察到的时刻
        self._dirty = False                       # 尺寸变过、尚未稳定重锚
        self._settle = 0.2                        # 稳定窗口（秒）：拖动停止后才重锚
        if enabled:
            self._progress = self._build(console)
            self._install_resize_guard()

    # ---------------- 构建 ----------------

    def _build(self, console: Console | None) -> Progress:
        unlimited = self.total <= 0
        columns = []
        if unlimited:
            columns.append(SpinnerColumn())              # 无休止模式：转圈提示
        columns += [
            # 留余量，避免帧顶满行宽；进度条主体走绿色（rich style，
            # 帧宽按 segment 计量，不会像裸 ANSI 那样破坏宽度测量）
            BarColumn(bar_width=20, complete_style="green",
                      finished_style="bold green"),
            TextColumn("{task.description}", style="bold cyan"),
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

    # ---------------- 窗口尺寸变化 ----------------

    def _install_resize_guard(self) -> None:
        """包装 ``Live.refresh``：重绘前核对终端尺寸，拖动中暂缓画帧。

        拖动窗口后 conhost 会把已写入的进度行重新折行，rich 缓存的
        光标几何（``_shape``）立即失配——``position_cursor`` 擦不掉旧帧，
        新帧又叠上去，屏幕上会堆出一串重复的进度条。

        处理策略（去抖）：

        1. 检测到尺寸变化 → 记录候选尺寸并**抑制画帧**（拖动中不画，
           避免每 100ms 往错误几何里塞一帧）；
        2. 尺寸连续稳定 ``_settle`` 秒（拖动结束、conhost reflow 已完成）
           → 一次性清屏 + 重放日志，live 区域在新几何下重新锚定；
        3. 之后恢复正常画帧。
        """
        live = self._progress.live
        original = live.refresh

        def refresh(*args, **kwargs):
            if not self._check_resize():
                return              # 拖动中：不画帧，等稳定后重锚
            return original(*args, **kwargs)

        live.refresh = refresh

    def _check_resize(self, force: bool = False) -> bool:
        """核对终端尺寸并做去抖；返回 False 表示此刻不应画帧。

        ``force=True`` 跳过稳定窗口（收尾时用），立刻完成重锚。
        """
        if self._progress is None:
            return True
        live = self._progress.live
        with live._lock:                         # 刷新线程 / 主线程串行化检测
            console = self._progress.console
            try:
                size = console.size
            except Exception:
                return True
            now = time.perf_counter()
            if self._last_size is None:          # 首次观测：只记账
                self._last_size = size
                self._cand = size
                self._cand_since = now
                return True
            if size != self._cand:               # 尺寸又变了 → 重新计时
                self._cand = size
                self._cand_since = now
                self._dirty = True
            if not self._dirty:
                return True                      # 尺寸从未变化
            if not force and (now - self._cand_since) < self._settle:
                return False                     # 拖动中 / reflow 未静止
            self._last_size = size
            self._dirty = False
            if self._live:
                self._reanchor()
            return True

    def _reanchor(self) -> None:
        """清掉重排产生的残帧，并在新位置重锚 live 区域。"""
        live = self._progress.live
        with live._lock:                         # 与后台刷新互斥（RLock 可重入）
            live._live_render._shape = None      # 先让 position_cursor 失效
            console = self._progress.console
            console.clear()                      # 抹掉重排留下的所有残帧
            for line in self._log_tail:          # 重放日志，live 帧随后跟上
                console.print(line, markup=False, highlight=False)

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
        # 分隔符必须用 ASCII：conhost（GBK/中文环境）把 `·`(U+00B7) 按宽度 2
        # 渲染，rich 却按 1 计算，帧会比 rich 预期宽出几列而折行——rich 的
        # `_shape` 仍记 1 行，position_cursor 擦不掉旧帧，进度条逐帧堆积。
        return (f"上组 std {fmt_ms(info.std_ms)}, hack {fmt_ms(info.hack_ms)}"
                f", 内存 {fmt_mb(info.peak_mb)}, {reason}")

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
        if self._progress is None:
            self._print_plain(text)
            return
        live = self._progress.live
        with live._lock:                    # 与刷新线程的 reanchor 互斥
            # 先核对尺寸：刚拖过窗口时先清残帧再打这一行
            stable = self._check_resize()
            self._log_tail.append(text)
            if not self._live:
                self._print_plain(text)
            elif stable:
                # 日志里可能有 [第 1 轮]、[0..n) 这类方括号，必须关掉 markup。
                # 持 live._lock 覆盖整个 print：position_cursor 计算与实际
                # 写入必须原子，否则 reanchor 会插在两者之间留下错位内容。
                self._progress.console.print(text, markup=False,
                                             highlight=False)
            # 拖动中（stable=False）：只记入 tail，等重锚时统一重放

    @staticmethod
    def _print_plain(text: str = "") -> None:
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
        self._check_resize()
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
            # 拖动中收尾：跳过稳定窗口，先把残帧清掉再定格
            self._check_resize(force=True)
            self._progress.update(
                self._task_id,
                completed=self.round,
                description=self._stage(),
                info=self._info(),
            )
            self._progress.stop()
        self._live = False
