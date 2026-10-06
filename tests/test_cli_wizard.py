"""未配置题目 → 新窗口进入配置向导 的流程测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autohack import cli
from autohack.config import Config

STD = """
#include <iostream>
using namespace std;
int main() {
    int n;
    cin >> n;
    int a[100];
    for (int i = 0; i < n; i++) cin >> a[i];
    return 0;
}
"""

HACK = """
#include <iostream>
using namespace std;
int main() {
    int n;
    cin >> n;
    int a[100];
    for (int i = 0; i < n; i++) cin >> a[i];
    return 0;
}
"""


def _make_problem(root: Path, name: str = "T1", with_std: bool = True) -> Path:
    problem = root / name
    problem.mkdir(parents=True, exist_ok=True)
    if with_std:
        (problem / "std.cpp").write_text(STD, encoding="utf-8")
        (problem / "hack.cpp").write_text(HACK, encoding="utf-8")
    return problem


class ParserWizardFlagTest(unittest.TestCase):
    def test_wizard_flag_is_hidden_but_parseable(self):
        args = cli.build_parser().parse_args(["--wizard", "-p", "T1", "-y"])
        self.assertTrue(args.wizard)
        self.assertEqual(args.problem, "T1")

    def test_wizard_flag_absent_by_default(self):
        self.assertFalse(cli.build_parser().parse_args([]).wizard)

    def test_help_does_not_show_wizard(self):
        import io
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cli.build_parser().print_help()
        self.assertNotIn("--wizard", buf.getvalue())


class SpawnWindowTest(unittest.TestCase):
    def _args(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_wizard_mode_cmd(self):
        args = self._args(["-p", "T1", "-r", "3", "-s", "7"])
        with mock.patch("autohack.cli.subprocess.Popen") as popen:
            code = cli._spawn_window(args, Path("T1"), None, wizard=True)
        self.assertEqual(code, 0)
        cmd = popen.call_args[0][0]
        self.assertIn("--wizard", cmd)
        self.assertNotIn("--worker", cmd)
        self.assertNotIn("-y", cmd)              # 配完是否开拍由新窗口自己问
        self.assertEqual(cmd[cmd.index("-r") + 1], "3")
        self.assertEqual(cmd[cmd.index("-s") + 1], "7")

    def test_wizard_mode_cmd_keeps_yes_when_user_passed_it(self):
        args = self._args(["-p", "T1", "-y"])
        with mock.patch("autohack.cli.subprocess.Popen") as popen:
            cli._spawn_window(args, Path("T1"), None, wizard=True)
        cmd = popen.call_args[0][0]
        self.assertIn("--wizard", cmd)
        self.assertIn("-y", cmd)

    def test_worker_mode_cmd(self):
        args = self._args(["-p", "T1"])
        with mock.patch("autohack.cli.subprocess.Popen") as popen:
            code = cli._spawn_window(args, Path("T1"), Config(problem="T1"))
        self.assertEqual(code, 0)
        cmd = popen.call_args[0][0]
        self.assertIn("--worker", cmd)
        self.assertIn("-y", cmd)                 # 父窗口已确认过

    def test_popen_failure_falls_back_in_current_window(self):
        args = self._args(["-p", "T1"])
        with mock.patch("autohack.cli.subprocess.Popen",
                        side_effect=OSError("no console")), \
                mock.patch("autohack.cli._configure_and_run",
                           return_value=17) as run:
            code = cli._spawn_window(args, Path("T1"), None, wizard=True)
        self.assertEqual(code, 17)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["wait"], False)


class MainRoutingTest(unittest.TestCase):
    def test_unconfigured_problem_spawns_wizard_window(self):
        args = ["-p", "T1", "-y"]
        with mock.patch.object(cli, "_select_problem",
                               return_value=Path("T1")), \
                mock.patch.object(cli, "_load_cfg", return_value=None), \
                mock.patch.object(cli, "_spawn_window",
                                  return_value=0) as spawn, \
                mock.patch.object(cli, "_configure_and_run") as in_place:
            code = cli.main(args)
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        self.assertTrue(spawn.call_args.kwargs.get("wizard"))
        self.assertIsNone(spawn.call_args.args[2])   # cfg 还没有
        in_place.assert_not_called()

    def test_unconfigured_with_no_window_runs_wizard_here(self):
        args = ["-p", "T1", "-y", "--no-window"]
        with mock.patch.object(cli, "_select_problem",
                               return_value=Path("T1")), \
                mock.patch.object(cli, "_load_cfg", return_value=None), \
                mock.patch.object(cli, "_spawn_window") as spawn, \
                mock.patch.object(cli, "_configure_and_run",
                                  return_value=0) as in_place:
            code = cli.main(args)
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        in_place.assert_called_once()
        self.assertEqual(in_place.call_args.kwargs["wait"], False)

    def test_configured_problem_spawns_worker_window(self):
        cfg = Config(problem="T1")
        with mock.patch.object(cli, "_select_problem",
                               return_value=Path("T1")), \
                mock.patch.object(cli, "_load_cfg", return_value=cfg), \
                mock.patch.object(cli, "_spawn_window",
                                  return_value=0) as spawn, \
                mock.patch.object(cli, "_configure_and_run") as in_place:
            code = cli.main(["-p", "T1", "-y"])
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        self.assertFalse(spawn.call_args.kwargs.get("wizard", False))
        in_place.assert_not_called()

    def test_reconfig_spawns_wizard_window(self):
        cfg = Config(problem="T1")
        with mock.patch.object(cli, "_select_problem",
                               return_value=Path("T1")), \
                mock.patch.object(cli, "_load_cfg", return_value=cfg), \
                mock.patch.object(cli, "_spawn_window",
                                  return_value=0) as spawn:
            code = cli.main(["-p", "T1", "-y", "--reconfig"])
        self.assertEqual(code, 0)
        self.assertTrue(spawn.call_args.kwargs.get("wizard"))

    def test_declining_existing_config_spawns_wizard_window(self):
        cfg = Config(problem="T1")
        with mock.patch.object(cli, "_select_problem",
                               return_value=Path("T1")), \
                mock.patch.object(cli, "_load_cfg", return_value=cfg), \
                mock.patch.object(cli, "_ask", return_value="n"), \
                mock.patch.object(cli, "_spawn_window",
                                  return_value=0) as spawn:
            code = cli.main(["-p", "T1"])
        self.assertEqual(code, 0)
        self.assertTrue(spawn.call_args.kwargs.get("wizard"))


class VtHelperTest(unittest.TestCase):
    def test_enable_vt_never_raises(self):
        """输出被重定向时 GetConsoleMode 会失败，必须静默跳过。"""
        if __import__("os").name != "nt":
            self.skipTest("Windows 专属")
        cli._enable_vt()
        cli._enable_vt()          # 幂等


class WizardWorkerTest(unittest.TestCase):
    def _run_worker(self, root: Path, argv):
        args = cli.build_parser().parse_args(argv)
        with mock.patch.object(cli, "TEST_ROOT", root), \
                mock.patch("builtins.input", return_value=""), \
                mock.patch.object(cli, "_wait_key") as wait_key, \
                mock.patch.object(cli, "_widen_console"), \
                mock.patch.object(cli, "_set_console_title"), \
                mock.patch.object(cli, "_run_here",
                                  return_value=42) as run_here:
            code = cli._wizard_worker(args)
        return code, run_here, wait_key

    def test_wizard_then_stress_in_new_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_problem(root)
            code, run_here, wait_key = self._run_worker(
                root, ["--wizard", "-p", "T1"])

            self.assertEqual(code, 42)
            cfg_file = root / "T1" / "config.yaml"
            self.assertTrue(cfg_file.is_file())
            text = cfg_file.read_text(encoding="utf-8")
            self.assertIn("vars:", text)
            self.assertIn("n:", text)
            self.assertIn("a:", text)
            run_here.assert_called_once()
            self.assertEqual(run_here.call_args.kwargs["wait"], True)
            wait_key.assert_not_called()

    def test_wizard_failure_waits_for_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_problem(root, with_std=False)      # 没有 std.cpp
            code, run_here, wait_key = self._run_worker(
                root, ["--wizard", "-p", "T1"])

            self.assertEqual(code, 1)
            run_here.assert_not_called()
            wait_key.assert_called_once()
            self.assertFalse((root / "T1" / "config.yaml").exists())

    def test_missing_problem_dir(self):
        with tempfile.TemporaryDirectory() as td:
            args = cli.build_parser().parse_args(["--wizard", "-p", "Nope"])
            with mock.patch.object(cli, "TEST_ROOT", Path(td)), \
                    mock.patch.object(cli, "_wait_key") as wait_key:
                code = cli._wizard_worker(args)
            self.assertEqual(code, 1)
            wait_key.assert_called_once()

    def test_wizard_requires_problem(self):
        args = cli.build_parser().parse_args(["--wizard"])
        self.assertEqual(cli._wizard_worker(args), 1)


if __name__ == "__main__":
    unittest.main()
