"""config 与 cli 的基础回归测试。"""

import tempfile
import unittest
from pathlib import Path

from autohack.cli import _split_pair, build_parser
from autohack.config import Config, load_config, save_config


class ConfigRoundTripTest(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(problem="T1", kind="custom", gen="gen.cpp",
                         tl_ms=1500, ml_mb=128, rounds=10, max_hits=2,
                         threads=4, seed=7, note="含中文备注",
                         checks={"wa": True, "tle": False, "mle": True, "re": True},
                         vars={"n": {"min": 1, "max": 10}})
            save_config(Path(td), cfg)
            loaded = load_config(Path(td))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.problem, "T1")
            self.assertEqual(loaded.tl_ms, 1500)
            self.assertEqual(loaded.seed, 7)
            self.assertEqual(loaded.threads, 4)
            self.assertEqual(loaded.note, "含中文备注")
            self.assertFalse(loaded.checks["tle"])       # False 不能被默认值覆盖
            self.assertTrue(loaded.checks["wa"])
            self.assertEqual(loaded.vars["n"], {"min": 1, "max": 10})

    def test_old_config_without_threads_defaults_to_one(self):
        """老版本的 config.yaml 没有 threads 字段，应回退成单线程。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.yaml").write_text("problem: T1\nrounds: 5\n",
                                                  encoding="utf-8")
            cfg = load_config(Path(td))
            self.assertEqual(cfg.threads, 1)

    def test_missing_config_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(load_config(Path(td)))

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text("problem: T1\nfuture_field: 123\n",
                            encoding="utf-8")
            cfg = load_config(Path(td))
            self.assertEqual(cfg.problem, "T1")
            self.assertFalse(hasattr(cfg, "future_field"))

    def test_non_mapping_raises(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.yaml").write_text("- 1\n- 2\n",
                                                  encoding="utf-8")
            with self.assertRaises(TypeError):
                load_config(Path(td))


class CliHelpersTest(unittest.TestCase):
    def test_split_pair(self):
        self.assertEqual(_split_pair("3 7"), (3, 7))
        self.assertEqual(_split_pair("3..7"), (3, 7))
        self.assertEqual(_split_pair("3,7"), (3, 7))
        self.assertEqual(_split_pair("5"), (5, 5))
        self.assertEqual(_split_pair("7 3"), (3, 7))     # 自动交换
        self.assertIsNone(_split_pair("abc"))

    def test_parser_defaults(self):
        args = build_parser().parse_args([])
        self.assertIsNone(args.problem)
        self.assertIsNone(args.rounds)
        self.assertFalse(args.no_window)

    def test_parser_flags(self):
        args = build_parser().parse_args(
            ["-p", "T1", "-r", "0", "-s", "1", "--no-tle", "--no-window", "-q"])
        self.assertEqual(args.problem, "T1")
        self.assertEqual(args.rounds, 0)
        self.assertEqual(args.seed, 1)
        self.assertTrue(args.no_tle)
        self.assertTrue(args.no_window)
        self.assertTrue(args.quiet)

    def test_parser_threads_flag(self):
        self.assertIsNone(build_parser().parse_args([]).threads)
        self.assertEqual(
            build_parser().parse_args(["-j", "4"]).threads, 4)
        self.assertEqual(
            build_parser().parse_args(["--threads", "2"]).threads, 2)

    def test_threads_of_prefers_cli_then_config(self):
        from autohack.cli import _threads_of

        cfg = Config(problem="T1", threads=3)
        self.assertEqual(_threads_of(cfg), 3)                    # 用配置
        args = build_parser().parse_args(["-j", "5"])
        self.assertEqual(_threads_of(cfg, args), 5)              # -j 覆盖配置
        self.assertEqual(_threads_of(Config(threads=0)), 1)      # 0 收敛成 1
        self.assertEqual(_threads_of(Config(threads="脏数据")), 1)
        self.assertEqual(_threads_of(None), 1)


if __name__ == "__main__":
    unittest.main()
