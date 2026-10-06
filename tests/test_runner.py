"""runner 回归测试：老 g++ 的 -std 回退、编译缓存、TLE/RE/MLE 判定。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autohack.runner import BuildError, compile_cpp, run_program

ECHO = """
#include <iostream>
#include <string>
int main() {
    std::string line;
    std::getline(std::cin, line);
    std::cout << line << "\\n";
    return 0;
}
"""

SPIN = """
int main() {
    volatile unsigned long long x = 0;
    while (true) { x++; }
    return 0;
}
"""

FAIL = """
int main() { return 3; }
"""

FATTY = """
#include <vector>
int main() {
    std::vector<char> v(200u << 20);
    for (unsigned long long i = 0; i < v.size(); i += 4096) v[i] = 1;
    return 0;
}
"""

BROKEN = "this is not c++ at all"


class CompileTest(unittest.TestCase):
    def _write(self, td, name, text):
        src = Path(td) / name
        src.write_text(text, encoding="utf-8")
        return src

    def test_compile_works_with_old_standard_flag(self):
        """回归：本机 g++ 4.8.1 不认 -std=c++17，需逐级降级重试。"""
        with tempfile.TemporaryDirectory() as td:
            src = self._write(td, "echo.cpp", ECHO)
            exe = compile_cpp(src, Path(td) / "echo")
            self.assertTrue(exe.exists())
            res = run_program(exe, b"hello\n", timeout=10)
            self.assertEqual(res.stdout.replace(b"\r\n", b"\n"), b"hello\n")
            self.assertTrue(res.ok)

    def test_compile_cache_skips_recompile(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._write(td, "fail.cpp", FAIL)
            exe = compile_cpp(src, Path(td) / "fail")
            self.assertTrue(exe.exists())
            with mock.patch("autohack.runner.subprocess.run",
                            side_effect=AssertionError("命中缓存不应再编译")):
                again = compile_cpp(src, Path(td) / "fail")
            self.assertEqual(again, exe)

    def test_cache_invalidated_by_newer_source(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._write(td, "fail.cpp", FAIL)
            compile_cpp(src, Path(td) / "fail")
            self._write(td, "fail.cpp", BROKEN)      # 内容变脏
            import os
            future = os.path.getmtime(src) + 10      # 保证 mtime 确实更新
            os.utime(src, (future, future))
            with self.assertRaises(BuildError):
                compile_cpp(src, Path(td) / "fail")

    def test_compile_error_reports_detail(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._write(td, "broken.cpp", BROKEN)
            with self.assertRaises(BuildError) as ctx:
                compile_cpp(src, Path(td) / "broken")
            self.assertIn("broken.cpp", str(ctx.exception))


class RunProgramTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        root = Path(cls._td.name)
        cls.echo = compile_cpp(_write_source(root, "echo.cpp", ECHO),
                               root / "echo")
        cls.spin = compile_cpp(_write_source(root, "spin.cpp", SPIN),
                               root / "spin")
        cls.fail = compile_cpp(_write_source(root, "fail.cpp", FAIL),
                               root / "fail")
        cls.fatty = compile_cpp(_write_source(root, "fatty.cpp", FATTY),
                                root / "fatty")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_stdin_stdout(self):
        res = run_program(self.echo, b"abc\n", timeout=10)
        self.assertTrue(res.ok)
        self.assertEqual(res.stdout.replace(b"\r\n", b"\n"), b"abc\n")
        self.assertGreater(res.ms, 0)

    def test_tle(self):
        res = run_program(self.spin, b"", timeout=0.3)
        self.assertTrue(res.tle)
        self.assertFalse(res.crash)
        self.assertFalse(res.ok)

    def test_crash_on_nonzero_exit(self):
        res = run_program(self.fail, b"", timeout=10)
        self.assertTrue(res.crash)
        self.assertEqual(res.rc, 3)
        self.assertFalse(res.ok)

    def test_mle_detection(self):
        res = run_program(self.fatty, b"", timeout=30, ml_mb=64)
        if res.peak_mb is None:
            self.skipTest("当前平台拿不到峰值内存")
        self.assertGreater(res.peak_mb, 64)
        self.assertTrue(res.mle)


def _write_source(root: Path, name: str, text: str) -> Path:
    src = root / name
    src.write_text(text, encoding="utf-8")
    return src


if __name__ == "__main__":
    unittest.main()
