"""rich 进度条的回归测试。"""

import io
import re
import unittest
from contextlib import redirect_stdout

from rich.console import Console

from autohack.progress import (ProgressReporter, RoundInfo, fmt_duration,
                               fmt_mb, fmt_ms)

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _info(index: int, hits: int = 0, reasons: list | None = None,
          stats: dict | None = None) -> RoundInfo:
    return RoundInfo(index=index, hits=hits, std_ms=12.3, hack_ms=4.5,
                     peak_mb=2.7, reasons=reasons or [],
                     stats=stats or {})


class FormatterTest(unittest.TestCase):
    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(0), "00:00")
        self.assertEqual(fmt_duration(65), "01:05")
        self.assertEqual(fmt_duration(3725), "1:02:05")

    def test_fmt_ms(self):
        self.assertEqual(fmt_ms(None), "--")
        self.assertEqual(fmt_ms(0.25), "0.25ms")
        self.assertEqual(fmt_ms(12.3), "12.3ms")
        self.assertEqual(fmt_ms(12345), "12.3s")

    def test_fmt_mb(self):
        self.assertEqual(fmt_mb(None), "--")
        self.assertEqual(fmt_mb(0.5), "512KB")
        self.assertEqual(fmt_mb(12.5), "12.5MB")
        self.assertEqual(fmt_mb(2048), "2.00GB")


class DisabledReporterTest(unittest.TestCase):
    """enabled=False（非 TTY / --no-bar）时必须是纯文本输出。"""

    def test_plain_logs(self):
        reporter = ProgressReporter(total=10, enabled=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            for i in range(1, 4):
                reporter.update(_info(i))
            reporter.print("普通日志行")
            reporter.finish()

        out = buf.getvalue()
        self.assertIn("普通日志行", out)
        self.assertEqual(reporter.round, 3)
        self.assertNotIn("\x1b[", out)          # 没有任何 ANSI 控制序列
        self.assertFalse(reporter._live)


class EnabledReporterTest(unittest.TestCase):
    def _make(self, total: int = 10) -> tuple:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        return ProgressReporter(total=total, enabled=True, console=console), buf

    def test_renders_progress_line(self):
        reporter, buf = self._make(total=10)
        for i in range(1, 6):
            reporter.update(_info(i, hits=1 if i >= 3 else 0,
                                  reasons=["WA"] if i >= 3 else [],
                                  stats={"wa": 1} if i >= 3 else {}))
        reporter.finish()

        text = ANSI.sub("", buf.getvalue())
        frames = [line for line in text.splitlines() if "轮/秒" in line]
        self.assertTrue(frames, "没有渲染出进度行")
        # 进度条本体：Unicode 下是 ━，ASCII/legacy 控制台降级为 -
        self.assertRegex(frames[-1], r"^\s*[-━]{10,}")
        self.assertIn("5/10", text)             # 阶段：5/10  50.0%
        self.assertIn("50.0%", text)
        self.assertIn("命中 1", text)
        self.assertIn("WA=1", text)
        self.assertIn("上组 std 12.3ms", text)
        self.assertIn("Ctrl+C 停止", text)
        self.assertTrue(reporter._live is False)

    def test_log_interleaves_and_markup_stays_literal(self):
        """日志里的 [第 3 轮]、[0..n) 不能被 rich 当成 markup 吃掉。"""
        reporter, buf = self._make(total=10)
        reporter.update(_info(1))
        reporter.print("  [第 3 轮] 命中 hack 点: WA")
        reporter.print("数组 a 范围 [0..n)")
        reporter.finish()

        text = ANSI.sub("", buf.getvalue())
        self.assertIn("[第 3 轮] 命中 hack 点: WA", text)
        self.assertIn("数组 a 范围 [0..n)", text)

    def test_unlimited_mode(self):
        reporter, buf = self._make(total=0)
        for i in range(1, 4):
            reporter.update(_info(i))
        reporter.finish()

        text = ANSI.sub("", buf.getvalue())
        self.assertIn("3 轮", text)
        self.assertIn("无休止", text)
        self.assertNotIn("/0", text)

    def test_finish_without_update_is_noop(self):
        reporter, buf = self._make()
        reporter.finish()
        self.assertFalse(reporter._live)

    def test_repeated_finish_is_safe(self):
        reporter, buf = self._make()
        reporter.update(_info(1))
        reporter.finish()
        reporter.finish()
        self.assertFalse(reporter._live)


class SpeedAndStageTest(unittest.TestCase):
    def test_speed_zero_before_warmup(self):
        reporter = ProgressReporter(total=10, enabled=False)
        reporter.update(_info(1))
        self.assertEqual(reporter.speed, 0.0)

    def test_stage_text(self):
        limited = ProgressReporter(total=100, enabled=False)
        limited.round = 50
        self.assertEqual(limited._stage(), "50/100   50.0%")   # 百分比定宽右对齐
        limited.round = 100
        self.assertEqual(limited._stage(), "100/100  100.0%")

        unlimited = ProgressReporter(total=0, enabled=False)
        unlimited.round = 7
        self.assertEqual(unlimited._stage(), "7 轮  无休止")


if __name__ == "__main__":
    unittest.main()
