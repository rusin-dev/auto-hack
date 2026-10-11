"""stress 回归测试：端到端对拍、命中落盘清理、输出归一化。"""

import tempfile
import unittest
from pathlib import Path

from autohack.config import Config
from autohack.generator import _edge_pool
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
    def test_whitespace_is_not_a_difference(self):
        """回归：按整行比较会把排版差异误判成 WA。"""
        self.assertEqual(normalize(b"1 2 \r\n3\t\n\n"), [b"1", b"2", b"3"])
        self.assertTrue(outputs_equal(b"1 2 \n3\n", b"1 2\n3\n\n"))
        # 行内多余空格 / Tab / CRLF
        self.assertTrue(outputs_equal(b"1  2\r\n", b"1\t2\n"))
        # 一行多个数 vs 每行一个数
        self.assertTrue(outputs_equal(b"1 2 3\n", b"1\n2\n3\n"))
        # 末尾换行缺失
        self.assertTrue(outputs_equal(b"42", b"42\n"))

    def test_real_differences_still_hit(self):
        self.assertFalse(outputs_equal(b"1\n", b"2\n"))
        self.assertFalse(outputs_equal(b"1 2\n", b"1 2 3\n"))   # 少一个 token
        self.assertFalse(outputs_equal(b"1 2\n", b"1\n"))       # 多一个 token
        self.assertFalse(outputs_equal(b"\n", b"42\n"))         # 空输出
        self.assertFalse(outputs_equal(b"12\n", b"1 2\n"))      # 合并数字不等于拆分


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


class BoundaryStrategyStressTest(unittest.TestCase):
    """只有 n=1 才错的程序：证明边界策略比纯随机实用得多。"""

    N1_STD = """
#include <iostream>
using namespace std;
int main() { int n, a; cin >> n >> a; cout << (n == 1 ? a * 2 : a) << "\\n"; }
"""

    N1_HACK = """
#include <iostream>
using namespace std;
int main() { int n, a; cin >> n >> a; cout << (n <= 1 ? 0 : a) << "\\n"; }
"""

    VARS = {"n": {"min": 1, "max": 200000}, "a": {"min": 1, "max": 10 ** 9}}

    def _run(self, root: Path, strategy: str, rounds: int, seed: int = 1):
        problem = root / "test" / "N1"
        problem.mkdir(parents=True, exist_ok=True)
        (problem / "std.cpp").write_text(self.N1_STD, encoding="utf-8")
        (problem / "hack.cpp").write_text(self.N1_HACK, encoding="utf-8")
        cfg = Config(problem="N1", strategy=strategy, rounds=rounds,
                     max_hits=1, seed=seed, tl_ms=1000, ml_mb=256,
                     vars=dict(self.VARS))
        return stress(problem_dir=problem, cfg=cfg,
                      build_root=root / "build" / strategy, repo_root=root,
                      rounds=rounds, seed=seed, verbose=False)

    def test_edge_finds_boundary_only_bug(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._run(Path(td), "edge", 5)
            self.assertEqual(report.aborted, "")
            self.assertEqual(len(report.hits), 1)
            # edge 策略只从边界池取值，n=1（下界）第 1 轮就出现
            self.assertIn(int(report.hits[0].data.split()[0]),
                          _edge_pool(1, 200000))
            self.assertEqual(report.hits[0].data.split()[0], b"1")

    def test_mixed_reaches_it_far_faster_than_random(self):
        with tempfile.TemporaryDirectory() as td:
            mixed = self._run(Path(td), "mixed", 30)
            self.assertEqual(mixed.aborted, "")
            self.assertEqual(len(mixed.hits), 1)
            self.assertEqual(mixed.hits[0].data.split()[0], b"1")

            # P(n=1) 在 [1, 200000] 上均匀随机是 5e-6：100 轮基本打不到
            plain = self._run(Path(td), "random", 100)
            self.assertEqual(plain.aborted, "")
            self.assertEqual(len(plain.hits), 0)


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


class ParallelStressTest(unittest.TestCase):
    """多线程对拍：轮号领取、进度回调顺序、max_hits 语义与单线程一致。"""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        root = Path(cls._td.name)
        problem = root / "test" / "T1"
        problem.mkdir(parents=True)
        (problem / "std.cpp").write_text(STD, encoding="utf-8")
        (problem / "hack.cpp").write_text(HACK, encoding="utf-8")
        cls.root = root
        cls.problem = problem
        cls.build = root / "build" / "T1"          # 两个用例共用编译缓存

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _cfg(self, **kw) -> Config:
        base = dict(problem="T1",
                    vars={"n": {"min": 1, "max": 8},
                          "a": {"min": 1, "max": 100}})
        base.update(kw)
        return Config(**base)

    def test_parallel_runs_every_round(self):
        done: list[int] = []
        cfg = self._cfg(rounds=6, max_hits=0, seed=42, threads=4)
        report = stress(problem_dir=self.problem, cfg=cfg,
                        build_root=self.build, repo_root=self.root / "r1",
                        rounds=6, seed=42, verbose=False,
                        on_round=done.append)

        self.assertEqual(report.aborted, "")
        self.assertEqual(report.threads, 4)
        self.assertEqual(report.rounds, 6)
        self.assertEqual(len(report.hits), 6)
        self.assertEqual(report.stats["wa"], 6)
        # 进度条回调按“已完成轮数”送达：4 线程下也必须单调不回退
        indices = [info.index for info in done]
        self.assertEqual(len(indices), 6)
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(indices[-1], 6)
        # 命中按轮号排序并重新编号：001..006 对应第 1..6 轮
        self.assertEqual([h.round_index for h in report.hits], list(range(1, 7)))
        self.assertEqual([h.index for h in report.hits], list(range(1, 7)))

    def test_parallel_honors_max_hits(self):
        """无休止 + 只留 1 个 hack 点：在途轮次也不能多存一个。"""
        cfg = self._cfg(rounds=0, max_hits=1, seed=1, threads=4)
        report = stress(problem_dir=self.problem, cfg=cfg,
                        build_root=self.build, repo_root=self.root / "r2",
                        rounds=0, seed=1, verbose=False)

        self.assertEqual(report.aborted, "")
        self.assertEqual(len(report.hits), 1)
        self.assertEqual(report.stats["wa"], 1)
        self.assertTrue(report.output_dir is not None)
        self.assertTrue((report.output_dir / "001.in").is_file())
        self.assertFalse((report.output_dir / "002.in").is_file())

    def test_parallel_seed_matches_single_thread(self):
        """同一个种子：多线程与单线程命中的数据完全一致（按轮号排序后）。"""
        kw = dict(rounds=3, seed=11, verbose=False)
        one = stress(problem_dir=self.problem, cfg=self._cfg(threads=1, max_hits=0),
                     build_root=self.build, repo_root=self.root / "r3", **kw)
        many = stress(problem_dir=self.problem, cfg=self._cfg(threads=4, max_hits=0),
                      build_root=self.build, repo_root=self.root / "r4", **kw)
        self.assertEqual([h.data for h in one.hits], [h.data for h in many.hits])
        self.assertEqual([h.round_index for h in one.hits],
                         [h.round_index for h in many.hits])

    def test_parallel_ctrl_c_stops_workers(self):
        """主线程等待线程时收到 Ctrl+C：置停止位并等在途轮次收尾。"""
        import threading as _threading
        from unittest import mock

        real_join = _threading.Thread.join
        pending = {"armed": True}

        def fake_join(self, timeout=None):
            # 只截获 drain() 对工作线程的 join；内存监视线程的 join 照常走
            if pending["armed"] and self.name.startswith("autohack-stress-"):
                pending["armed"] = False
                raise KeyboardInterrupt
            return real_join(self, timeout)

        cfg = self._cfg(rounds=40, max_hits=0, seed=5, threads=3)
        with mock.patch.object(_threading.Thread, "join", fake_join):
            report = stress(problem_dir=self.problem, cfg=cfg,
                            build_root=self.build, repo_root=self.root / "r5",
                            rounds=40, seed=5, verbose=False)

        self.assertTrue(report.stopped)
        self.assertEqual(report.aborted, "")
        # 停止位生效后不该再把 40 轮跑完（否则说明线程没收到停止信号）
        self.assertLess(report.rounds, 40)


if __name__ == "__main__":
    unittest.main()
