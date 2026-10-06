"""style 模块：颜色开关、paint 行为与上色输出的退化路径。"""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from autohack import cli, style
from autohack.stress import StressReport, print_report


class ColorsEnabledTest(unittest.TestCase):
    def test_plain_when_stdout_is_not_tty(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            self.assertFalse(style.colors_enabled())
            self.assertEqual(style.ok("命中"), "命中")
            self.assertNotIn("\x1b[", style.title("标题"))

    def test_force_color_enables(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            self.assertTrue(style.colors_enabled())
            text = style.ok("命中")
        self.assertTrue(text.startswith("\x1b[32m"))
        self.assertTrue(text.endswith(style.RESET))
        self.assertIn("命中", text)

    def test_no_color_wins_over_force_color(self):
        env = {"NO_COLOR": "1", "FORCE_COLOR": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(style.colors_enabled())
            self.assertEqual(style.bad("错误"), "错误")

    def test_semantic_wrappers(self):
        cases = {
            style.title: "\x1b[1;36m",
            style.ok: "\x1b[32m",
            style.warn: "\x1b[33m",
            style.bad: "\x1b[31m",
            style.dim: "\x1b[2m",
        }
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            for fn, prefix in cases.items():
                with self.subTest(fn=fn.__name__):
                    self.assertEqual(fn("X"), prefix + "X" + style.RESET)

    def test_empty_codes_returns_plain(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            self.assertEqual(style.paint("X"), "X")


class BannerAndReportTest(unittest.TestCase):
    """横幅与结束报告：有颜色时带 ANSI，重定向时必须退化为纯文本。"""

    def test_banner_plain_without_tty(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            text = cli._banner()
        self.assertNotIn("\x1b[", text)
        self.assertIn("auto-hack", text)
        self.assertIn("=" * 60, text)

    def test_banner_colored_when_forced(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            text = cli._banner()
        self.assertIn("\x1b[", text)
        self.assertIn("auto-hack", text)

    def test_report_plain_without_tty(self):
        report = StressReport(problem="T1")
        report.stats = {"wa": 2}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(buf):
            print_report(report)
        out = buf.getvalue()
        self.assertNotIn("\x1b[", out)
        self.assertIn("正常结束", out)
        self.assertIn("WA=2", out)

    def test_report_colored_when_forced(self):
        report = StressReport(problem="T1")
        report.aborted = "编译失败"
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True), \
                redirect_stdout(buf):
            print_report(report)
        out = buf.getvalue()
        self.assertIn("\x1b[31m", out)            # 中止 -> 红
        self.assertIn("中止: 编译失败", out)
        self.assertIn("\x1b[2m", out)             # 分隔线 -> 暗淡


if __name__ == "__main__":
    unittest.main()
