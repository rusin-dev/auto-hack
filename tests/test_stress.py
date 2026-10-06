"""stress 回归测试：端到端对拍、命中落盘清理、输出归一化。"""

import tempfile
import unittest
from pathlib import Path

from autohack.config import Config
from autohack.stress import Hit, normalize, outputs_equal, save_hits, stress

STD = """
#include <iostream>
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n);
    for (int i = 0; i < n; i++) cin >> a[i];
    long long s = 0;
    for (int i = 0; i < n; i++) s += a[i];
    cout << s << "\\n";
    return 0;
}
"""

# 被 hack 的程序：漏加最后一个元素
HACK = """
#include <iostream>
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n);
    for (int i = 0; i < n; i++) cin >> a[i];
    long long s = 0;
    for (int i = 0; i < n - 1; i++) s += a[i];
    cout << s << "\\n";
    return 0;
}
"""


class NormalizeTest(unittest.TestCase):
    def test_ignores_trailing_spaces_and_blank_lines(self):
        self.assertEqual(normalize(b"1 2 \r\n3\t\n\n"), [b"1 2", b"3"])
        self.assertTrue(outputs_equal(b"1 2 \n3\n", b"1 2\n3\n\n"))
        self.assertFalse(outputs_equal(b"1\n", b"2\n"))


class SaveHitsTest(unittest.TestCase):
    def test_stale_hits_are_removed(self):
        """回归：上一次运行残留的 .in/.out 会和新结果混在一起。"""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            problem = repo / "test" / "T1"
            problem.mkdir(parents=True)
            out_dir = repo / "hack" / "T1"
            out_dir.mkdir(parents=True)
            for i in range(1, 6):                    # 上一轮留下 5 个命中
                (out_dir / f"{i:03d}.in").write_bytes(b"old")
                (out_dir / f"{i:03d}.out").write_bytes(b"old")
            (out_dir / "hits.txt").write_text("stale", encoding="utf-8")

            cfg = Config(problem="T1")
            hits = [Hit(1, b"new-in-1", b"new-out-1", ["WA"]),
                    Hit(2, b"new-in-2", b"new-out-2", ["TLE (2000 ms)"])]
            result = save_hits(repo, problem, cfg, hits)

            self.assertEqual(result, out_dir)
            self.assertEqual(sorted(p.name for p in out_dir.glob("*.in")),
                             ["001.in", "002.in"])
            self.assertEqual(sorted(p.name for p in out_dir.glob("*.out")),
                             ["001.out", "002.out"])
            self.assertEqual((out_dir / "001.in").read_bytes(), b"new-in-1")
            text = (out_dir / "hits.txt").read_text(encoding="utf-8")
            self.assertIn("001  WA", text)
            self.assertNotIn("stale", text)


class StressEndToEndTest(unittest.TestCase):
    def test_finds_wa_and_saves_hits(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            problem = repo / "test" / "T1"
            problem.mkdir(parents=True)
            (problem / "std.cpp").write_text(STD, encoding="utf-8")
            (problem / "hack.cpp").write_text(HACK, encoding="utf-8")

            cfg = Config(
                problem="T1",
                tl_ms=2000,
                ml_mb=256,
                rounds=3,
                max_hits=0,
                seed=42,
                vars={"n": {"min": 1, "max": 8},
                      "a": {"min": 1, "max": 100}},
            )
            report = stress(problem_dir=problem, cfg=cfg,
                            build_root=repo / "build" / "T1",
                            repo_root=repo, rounds=3, seed=42,
                            verbose=False)

            self.assertEqual(report.aborted, "")
            self.assertTrue(report.success, report.aborted)
            self.assertEqual(report.rounds, 3)
            self.assertEqual(len(report.hits), 3)
            for hit in report.hits:
                self.assertEqual(hit.reasons, ["WA"])
            self.assertEqual(report.stats["wa"], 3)

            out_dir = repo / "hack" / "T1"
            self.assertTrue((out_dir / "001.in").is_file())
            self.assertTrue((out_dir / "001.out").is_file())
            self.assertTrue((out_dir / "std.cpp").is_file())
            self.assertTrue((out_dir / "hits.txt").is_file())
            # 生成的数据格式必须能被标程正确读取：首行 n，次行 n 个数
            lines = (out_dir / "001.in").read_text(encoding="utf-8").splitlines()
            n = int(lines[0])
            self.assertEqual(len(lines[1].split()), n)
            # 输出是标程的正确答案
            expected = sum(int(x) for x in lines[1].split())
            self.assertEqual(int((out_dir / "001.out").read_text(encoding="utf-8")), expected)

    def test_seed_reproducible(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            problem = repo / "test" / "T1"
            problem.mkdir(parents=True)
            (problem / "std.cpp").write_text(STD, encoding="utf-8")
            (problem / "hack.cpp").write_text(HACK, encoding="utf-8")
            cfg = Config(problem="T1", vars={"n": {"min": 1, "max": 8},
                                             "a": {"min": 1, "max": 100}})
            first = stress(problem_dir=problem, cfg=cfg,
                           build_root=repo / "b1", repo_root=repo / "r1",
                           rounds=2, seed=7, verbose=False)
            second = stress(problem_dir=problem, cfg=cfg,
                            build_root=repo / "b2", repo_root=repo / "r2",
                            rounds=2, seed=7, verbose=False)
            self.assertEqual([h.data for h in first.hits],
                             [h.data for h in second.hits])


if __name__ == "__main__":
    unittest.main()
