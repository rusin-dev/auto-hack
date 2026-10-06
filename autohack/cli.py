"""命令行入口：交互式配置向导 + 独立窗口对拍。"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .asts import VarSpec, resolve_vars
from .asts import scan as scan_source
from .config import Config, load_config, save_config
from .progress import ProgressReporter
from .stress import print_report, scan_problems, stress

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = REPO_ROOT / "test"
BUILD_ROOT = REPO_ROOT / "build"
SCRIPT = REPO_ROOT / "main.py"

BANNER = """
============================================================
  auto-hack  自动 Hack 对拍工具
============================================================
"""

WINDOWS_NEW_CONSOLE = 0x00000010        # CREATE_NEW_CONSOLE


def _p(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode("utf-8", "replace").decode(enc, "replace"), flush=True)


def _rounds_text(value: int) -> str:
    return "无休止" if value <= 0 else f"{value} 轮"


def _hits_text(value: int) -> str:
    return "不限制" if value <= 0 else f"{value} 个 hack 点"


# --------------------------------------------------------------------------
# 输入工具
# --------------------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        _p("")
        raise SystemExit(1)
    if not raw and default is not None:
        return str(default)
    return raw


def _ask_choice(prompt: str, choices: dict[str, str], default: str) -> str:
    _p(prompt)
    for key, label in choices.items():
        mark = " (默认)" if key == default else ""
        _p(f"  {key}) {label}{mark}")
    while True:
        raw = _ask("请输入编号", default)
        if raw in choices:
            return raw
        _p("  输入无效，请重新输入")


def _ask_int(prompt: str, default: int, minimum: int | None = None,
             maximum: int | None = None) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            _p("  请输入整数")
            continue
        if minimum is not None and value < minimum:
            _p(f"  不能小于 {minimum}")
            continue
        if maximum is not None and value > maximum:
            _p(f"  不能大于 {maximum}")
            continue
        return value


def _split_pair(text: str) -> tuple[int, int] | None:
    cleaned = text.replace("..", " ").replace(",", " ").replace(":", " ")
    parts = [p for p in cleaned.split() if p]
    if len(parts) == 1:
        try:
            value = int(parts[0])
            return value, value
        except ValueError:
            return None
    if len(parts) >= 2:
        try:
            lo, hi = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    return None


def _ask_range(prompt: str, default: tuple[int, int]) -> tuple[int, int]:
    hint = f"例如 {default[0]} {default[1]}"
    while True:
        raw = _ask(f"{prompt} ({hint})", f"{default[0]} {default[1]}")
        pair = _split_pair(raw)
        if pair is None:
            _p("  格式不对，请输入两个整数，用空格/逗号/.. 分隔")
            continue
        return pair


# --------------------------------------------------------------------------
# 配置向导
# --------------------------------------------------------------------------

def _level_desc(spec: VarSpec) -> str:
    if spec.kind == "scalar":
        return "单个整数"
    if spec.kind == "string":
        return "字符串"
    if not spec.levels:
        return "数组 (个数待定)"
    parts = []
    for level in spec.levels:
        edge = "]" if level["inclusive"] else ")"
        parts.append(f"[{level['lo']}..{level['hi']}{edge}")
    return "数组 " + " x ".join(parts)


def _auto_default(spec: VarSpec, scanned) -> tuple[int, int]:
    """根据 AST 上下文给出一个安全的默认取值范围。"""
    import re as _re

    if scanned.repeat is not None and spec.name == scanned.repeat.var:
        return 1, 10                                  # 多组测试数

    size_vars: set[str] = set()
    for loop in scanned.loops:
        for expr in (loop.hi_expr, loop.lo_expr):
            if not expr:
                continue
            size_vars.update(_re.findall(r"[A-Za-z_]\w*", expr))
    if spec.name in size_vars:
        return 1, 100                                 # 用作循环上界的变量

    if spec.kind == "string":
        return 1, 8
    if spec.base in ("long", "__int128", "double", "size_t", "unsigned"):
        return 1, 10 ** 18
    return 1, 10 ** 9


def _locate_generator(problem_dir: Path) -> Path | None:
    for name in ("gen.cpp", "gen.py", "generator.cpp", "generator.py"):
        candidate = problem_dir / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _ask_generator(problem_dir: Path) -> str | None:
    found = _locate_generator(problem_dir)
    if found is not None:
        _p(f"已找到生成器: {found.name}")
        use = _ask("直接使用它？(y/n)", "y").lower()
        if use.startswith("y"):
            return found.name
    _p(f"请把生成器放到 {problem_dir} 目录（gen.cpp 或 gen.py），")
    _p("或者现在直接输入生成器文件的完整路径：")
    raw = _ask("生成器路径", "")
    if not raw:
        if found is None:
            _p(f"没有生成器，无法继续。请把 gen.cpp 放到 {problem_dir} 后重试。")
            return None
        return found.name
    src = Path(raw).expanduser()
    if not src.is_file():
        _p(f"找不到文件: {src}")
        return None
    target = problem_dir / f"gen{src.suffix.lower()}"
    if src.resolve() != target.resolve():
        shutil.copy2(src, target)
        _p(f"已复制为 {target.name}")
    return target.name


def _ask_runtime(cfg: Config) -> None:
    cfg.tl_ms = _ask_int("时间限制 TLE (毫秒)", cfg.tl_ms, minimum=1)
    cfg.ml_mb = _ask_int("内存限制 MLE (MB)", cfg.ml_mb, minimum=1)
    cfg.rounds = _ask_int("对拍轮数 (0 = 无休止)", cfg.rounds, minimum=0,
                          maximum=100_000_000)
    cfg.max_hits = _ask_int("找到几个 hack 点后停止 (0 = 不限制)",
                            cfg.max_hits, minimum=0, maximum=100_000)
    seed_raw = _ask("随机种子（留空 = 每次不同）", "")
    cfg.seed = int(seed_raw) if seed_raw.strip() else None


def _wizard(problem_dir: Path) -> Config | None:
    _p(f"\n>>> 正在配置题目: {problem_dir.name}")

    kind_choice = _ask_choice(
        "\n请选择题目类型（决定数据怎么生成）:",
        {
            "1": "普通题 —— 数学/数组/字符串，自动用 AST 识别输入变量",
            "2": "图论/树/复杂结构 —— 需要你自己提供数据生成器",
            "3": "自定义生成器 —— 直接用已有的 gen.cpp / gen.py",
        },
        default="1",
    )

    gen_name = None
    kind = "standard"
    if kind_choice in ("2", "3"):
        kind = "custom"
        gen_name = _ask_generator(problem_dir)
        if gen_name is None:
            return None

    cfg = Config(problem=problem_dir.name, kind=kind, gen=gen_name)
    if kind == "custom":
        _p("已选择自定义生成器，数据范围由生成器自己决定。")
        _ask_runtime(cfg)
        save_config(problem_dir, cfg)
        _p(f"配置已保存: {problem_dir / 'config.yaml'}")
        return cfg

    # ---- 标准模式：AST 识别 ----
    source_path = problem_dir / "std.cpp"
    if not source_path.is_file() or source_path.stat().st_size == 0:
        _p(f"错误: {source_path} 不存在或是空文件，请先写好标程代码。")
        return None

    source = source_path.read_text(encoding="utf-8", errors="replace")
    _p(f"正在用 AST 分析 {source_path.name} ...")
    try:
        scanned = scan_source(source)
        specs, warnings = resolve_vars(scanned)
    except Exception as exc:                          # noqa: BLE001
        _p(f"分析失败: {exc}")
        return None

    for warning in warnings:
        _p(f"  提示: {warning}")

    if not specs:
        _p("没有识别到任何输入变量，请确认 std.cpp 里有 cin/scanf/getline。")
        return None

    _p("\n识别到的输入变量:")
    _p(f"  {'#':<3}{'名称':<10}{'类型':<8}{'说明':<22}{'行号':<6}")
    for i, spec in enumerate(specs, 1):
        _p(f"  {i:<3}{spec.name:<10}{spec.kind:<8}{_level_desc(spec):<22}{spec.line:<6}")
    if scanned.repeat is not None:
        _p(f"  (检测到多组测试: 第 {scanned.repeat.line} 行开始的循环，"
           f"循环变量 {scanned.repeat.var})")

    if not _ask("\n识别结果是否正确？直接回车继续，输入 n 重新分析", "y").lower().startswith("y"):
        return None

    _p("\n请为每个变量输入取值范围（数组是元素值的范围）:")
    for spec in specs:
        previous = cfg.vars.get(spec.name, {})
        auto = _auto_default(spec, scanned)
        default = ((int(previous.get("min", auto[0])), int(previous.get("max", auto[1])))
                   if previous else auto)
        label = "长度范围" if spec.kind == "string" else "取值范围"
        lo, hi = _ask_range(f"  {spec.name} 的 {label}", default)
        entry: dict = {"min": lo, "max": hi}

        if spec.kind == "array":
            previous_count = previous.get("count")
            if spec.levels and not previous_count:
                _p(f"     检测到长度: {_level_desc(spec)}")
                count_raw = _ask("     元素个数（直接回车 = 按代码自动推断）", "")
            else:
                _p("     未能自动推断元素个数，请填写")
                count_raw = _ask("     元素个数（可用表达式，如 n）",
                                 str(previous_count or ""))
            if count_raw.strip():
                entry["count"] = count_raw.strip()
            elif spec.needs_count:
                _p("     必须填写元素个数，否则无法生成数据。")
                return None
        cfg.vars[spec.name] = entry

    _p("")
    _ask_runtime(cfg)

    cfg.checks = {
        "wa": _ask("检查输出不一致 WA？(y/n)", "y").lower().startswith("y"),
        "tle": _ask("检查超时 TLE？(y/n)", "y").lower().startswith("y"),
        "mle": _ask("检查超内存 MLE？(y/n)", "y").lower().startswith("y"),
        "re": _ask("检查运行错误 RE？(y/n)", "y").lower().startswith("y"),
    }

    save_config(problem_dir, cfg)
    _p(f"\n配置已保存: {problem_dir / 'config.yaml'}")
    _p("下次可直接用 python main.py -p " + problem_dir.name + " 开拍。")
    return cfg


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Codeforces 公开 Hack 自动化对拍工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py                      交互式选择题目（未配置时自动\n"
            "                                       新开窗口进入配置向导）\n"
            "  python main.py -p T1                用已有 config.yaml 对拍（新窗口）\n"
            "  python main.py -p T1 -r 0           无休止对拍\n"
            "  python main.py -p T1 -r 5000 -s 1   指定轮数与随机种子\n"
            "  python main.py -p T1 --no-window    不开新窗口，在当前窗口跑\n"
            "  python main.py --list               列出 test/ 下的题目\n"
            "\n"
            "轮数 / 命中数填 0 表示无休止，按 Ctrl+C 随时停止。\n"
        ),
    )
    parser.add_argument("-p", "--problem", help="题目目录名（test/ 下的一级目录）")
    parser.add_argument("--list", action="store_true", help="列出所有题目")
    parser.add_argument("--reconfig", action="store_true", help="重新运行配置向导")
    parser.add_argument("-r", "--rounds", type=int,
                        help="对拍轮数，0 = 无休止（覆盖配置）")
    parser.add_argument("-s", "--seed", type=int, help="随机种子")
    parser.add_argument("--tl", type=int, dest="tl_ms", help="时间限制（毫秒）")
    parser.add_argument("--ml", type=int, dest="ml_mb", help="内存限制（MB）")
    parser.add_argument("--max-hits", type=int,
                        help="命中多少个 hack 点后停止，0 = 不限制")
    parser.add_argument("--no-tle", action="store_true", help="不检查 TLE")
    parser.add_argument("--no-mle", action="store_true", help="不检查 MLE")
    parser.add_argument("--no-wa", action="store_true", help="不检查 WA")
    parser.add_argument("--no-re", action="store_true", help="不检查 RE")
    parser.add_argument("--no-window", action="store_true",
                        help="不开新窗口，直接在当前窗口对拍")
    parser.add_argument("--bar", action="store_true", help="强制显示进度条")
    parser.add_argument("--no-bar", action="store_true", help="强制关闭进度条")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wizard", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认直接开拍")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结果")
    return parser


def _checks_from_args(args) -> dict | None:
    checks: dict = {}
    if args.no_tle:
        checks["tle"] = False
    if args.no_mle:
        checks["mle"] = False
    if args.no_wa:
        checks["wa"] = False
    if args.no_re:
        checks["re"] = False
    return checks or None


def _bar_enabled(args) -> bool:
    if args.bar:
        return True
    if args.no_bar:
        return False
    try:
        return sys.stdout.isatty()
    except Exception:                                # noqa: BLE001
        return False


def _problem_state(path: Path) -> str | None:
    """不可用原因；可用返回 None。"""
    missing = [name for name in ("std.cpp", "hack.cpp")
               if not (path / name).is_file()]
    if missing:
        return "缺少 " + "、".join(missing)
    try:
        cfg = load_config(path)
    except Exception:                                # noqa: BLE001
        return "配置损坏"
    return None if cfg else "未配置"


def _load_cfg(problem_dir: Path) -> Config | None:
    try:
        return load_config(problem_dir)
    except Exception:                                # noqa: BLE001
        return None


def _split_problems() -> tuple[list[Path], list[Path], dict[Path, str | None]]:
    """返回 (可用, 不可用, 状态表)。状态表 key 是目录，value 为不可用原因。"""
    states = {path: _problem_state(path)
              for path in scan_problems(TEST_ROOT)}
    ready = [path for path, state in states.items() if state is None]
    blocked = [path for path, state in states.items() if state is not None]
    return ready, blocked, states


def _print_index(ready: list[Path], blocked: list[Path],
                 states: dict[Path, str | None]) -> None:
    if ready:
        _p("\n可用题目：")
        for i, path in enumerate(ready, 1):
            _p(f"  {i}) {path.name}")
    if blocked:
        base = len(ready)
        _p("\n不可用题目：")
        for i, path in enumerate(blocked, 1):
            _p(f"   {base + i}) {path.name}（{states[path]}）")


def _select_problem(args) -> Path | None:
    ready, blocked, states = _split_problems()
    all_dirs = ready + blocked
    if not all_dirs:
        _p(f"没有在 {TEST_ROOT} 下找到题目。")
        _p("请新建一个目录，例如 test/T1/，并在里面放 std.cpp 和 hack.cpp。")
        return None

    if args.problem:
        for path in all_dirs:
            if path.name != args.problem:
                continue
            state = states[path]
            if state and state.startswith("缺少"):
                _p(f"题目 {path.name} 不可用: {state}")
                return None
            return path
        _p(f"找不到题目 {args.problem!r}。现有题目: "
           + ", ".join(p.name for p in all_dirs))
        return None

    if len(all_dirs) == 1 and not blocked:
        return all_dirs[0]

    while True:
        _print_index(ready, blocked, states)
        raw = _ask("请选择题号", "1")
        try:
            index = int(raw)
        except ValueError:
            continue
        if not 1 <= index <= len(all_dirs):
            continue
        path = all_dirs[index - 1]
        if index <= len(ready):
            return path
        state = states[path]
        if state and state.startswith("缺少"):
            _p(f"  {path.name} 不可用: {state}，请先补齐源文件。")
            continue
        return path                # 未配置 / 配置损坏 -> 下一步进配置向导


# --------------------------------------------------------------------------
# 独立窗口
# --------------------------------------------------------------------------

def _set_console_title(title: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleTitleW(title)
    except Exception:                                # noqa: BLE001, S110
        pass


def _widen_console(cols: int = 168, rows: int = 60) -> None:
    """把独立窗口拉宽，进度条才有空间放下全部细节。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [
                ("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.GetStdHandle(-11)               # STD_OUTPUT_HANDLE
        if not handle:
            return
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return                                   # 不是控制台（被重定向了）
        win_w = info.srWindow.Right - info.srWindow.Left + 1
        win_h = info.srWindow.Bottom - info.srWindow.Top + 1
        target_cols = max(cols, win_w)                # 只拉宽，不拉窄
        target_rows = max(rows, win_h)
        # 缓冲区必须 >= 窗口，先扩缓冲区再调窗口
        k32.SetConsoleScreenBufferSize(
            handle, COORD(target_cols, max(target_rows, 9999)))
        k32.SetConsoleWindowInfo(
            handle, False,
            ctypes.byref(SMALL_RECT(0, 0, target_cols - 1, target_rows - 1)))
    except Exception:                                # noqa: BLE001, S110
        pass


def _enable_vt() -> None:
    """打开本进程的控制台 VT 处理。

    经典 conhost 默认不开 VT，rich 会因此判定为 legacy 控制台并降级渲染
    （进度条字符从 ━ 变成 -、渲染走 Windows API 路径）。在创建 rich Console
    之前开一下 VT 即可走完整 ANSI 渲染。VT 不可用（老系统 / 输出被重定向）
    时静默跳过，rich 自己会照常降级。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ENABLE_PROCESSED_OUTPUT = 0x0001
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.GetStdHandle(-11)               # STD_OUTPUT_HANDLE
        if not handle:
            return
        mode = ctypes.c_uint(0)
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle,
                               mode.value | ENABLE_PROCESSED_OUTPUT
                               | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:                                # noqa: BLE001, S110
        pass


def _wait_key(message: str = "按任意键退出... ") -> None:
    print(message, end="", flush=True)
    try:
        import msvcrt
        msvcrt.getch()
    except Exception:                                # noqa: BLE001
        try:
            input()
        except Exception:                            # noqa: BLE001, S110
            pass
    print()


def _spawn_window(args, problem_dir: Path, cfg: Config | None,
                  wizard: bool = False) -> int:
    """新开独立窗口：worker 模式跑对拍，wizard 模式先进配置向导。"""
    cmd = [sys.executable, "-u", str(SCRIPT),
           "--wizard" if wizard else "--worker",
           "-p", problem_dir.name]
    # worker 模式下父窗口已经确认过，直接开拍；
    # 配置向导模式把「配完是否开拍」的确认交给新窗口自己问
    if args.yes or not wizard:
        cmd.append("-y")
    if args.rounds is not None:
        cmd += ["-r", str(args.rounds)]
    if args.seed is not None:
        cmd += ["-s", str(args.seed)]
    if args.tl_ms:
        cmd += ["--tl", str(args.tl_ms)]
    if args.ml_mb:
        cmd += ["--ml", str(args.ml_mb)]
    if args.max_hits is not None:
        cmd += ["--max-hits", str(args.max_hits)]
    for name in ("no_tle", "no_mle", "no_wa", "no_re"):
        if getattr(args, name):
            cmd.append("--" + name.replace("_", "-"))
    if args.bar:
        cmd.append("--bar")
    if args.no_bar:
        cmd.append("--no-bar")
    if args.quiet:
        cmd.append("-q")

    title = f"auto-hack | {problem_dir.name}" + (" 配置" if wizard else "")
    popen_kwargs: dict = {"cwd": str(REPO_ROOT)}
    if os.name == "nt":
        popen_kwargs["creationflags"] = WINDOWS_NEW_CONSOLE
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(cmd, **popen_kwargs)
    except OSError as exc:
        _p(f"新开窗口失败: {exc}，改为在当前窗口运行。")
        if wizard:
            return _configure_and_run(args, problem_dir, wait=False)
        return _run_here(args, problem_dir, cfg, wait=False)

    if wizard:
        _p("已打开新窗口进入配置向导：")
        _p(f"  窗口标题 : {title}")
        _p(f"  题目     : {problem_dir.name}")
        _p("  完成配置后会在该窗口确认并自动开始对拍。")
        _p("  本窗口可继续使用。")
        return 0

    rounds = args.rounds if args.rounds is not None else cfg.rounds
    max_hits = args.max_hits if args.max_hits is not None else cfg.max_hits
    _p("已启动独立窗口进行对拍：")
    _p(f"  窗口标题 : {title}")
    _p(f"  题目     : {problem_dir.name}")
    _p(f"  轮数     : {_rounds_text(rounds)}   停止条件: {_hits_text(max_hits)}")
    _p(f"  限制     : TLE={args.tl_ms or cfg.tl_ms}ms  "
       f"MLE={args.ml_mb or cfg.ml_mb}MB")
    _p("  对拍结束后窗口会停在「按任意键退出」，本窗口可继续使用。")
    return 0


# --------------------------------------------------------------------------
# 当前窗口执行
# --------------------------------------------------------------------------

def _run_here(args, problem_dir: Path, cfg: Config, wait: bool) -> int:
    if args.tl_ms:
        cfg.tl_ms = args.tl_ms
    if args.ml_mb:
        cfg.ml_mb = args.ml_mb
    if args.seed is not None:
        cfg.seed = args.seed

    rounds = args.rounds if args.rounds is not None else cfg.rounds
    max_hits = args.max_hits if args.max_hits is not None else cfg.max_hits

    _enable_vt()          # 必须在 rich Console 创建之前打开
    reporter = ProgressReporter(total=rounds, enabled=_bar_enabled(args))

    reporter.print(f"  题目 {problem_dir.name}  |  "
                   f"轮数 {_rounds_text(rounds)}  |  "
                   f"停止条件 {_hits_text(max_hits)}  |  "
                   f"TLE={cfg.tl_ms}ms  MLE={cfg.ml_mb}MB")
    reporter.print("")

    report = stress(
        problem_dir=problem_dir,
        cfg=cfg,
        build_root=BUILD_ROOT / problem_dir.name,
        repo_root=REPO_ROOT,
        rounds=rounds,
        seed=args.seed,
        checks=_checks_from_args(args),
        max_hits=max_hits,
        verbose=not args.quiet,
        on_round=reporter.update,
        log=reporter.print,
    )
    reporter.finish()
    print_report(report, log=reporter.print)

    if wait:
        reporter.print("")
        _wait_key()
    if report.aborted:
        return 2
    return 0 if report.hits else 3


def _worker(args) -> int:
    if not args.problem:
        _p("--worker 必须配合 -p 使用")
        return 1
    problem_dir = TEST_ROOT / args.problem
    cfg = _load_cfg(problem_dir)
    if cfg is None:
        _p(f"找不到配置: {problem_dir / 'config.yaml'}")
        _wait_key()
        return 1

    _set_console_title(f"auto-hack | {problem_dir.name}")
    _widen_console()
    _p(BANNER)
    return _run_here(args, problem_dir, cfg, wait=True)


def _configure_and_run(args, problem_dir: Path, wait: bool) -> int:
    """配置向导 → 确认开拍 → 对拍。

    ``wait=True`` 时全程结束前等待按键（供独立窗口使用，窗口不会一闪而过）。
    """
    cfg = _wizard(problem_dir)
    if cfg is None:
        if wait:
            _wait_key()
        return 1

    rounds = args.rounds if args.rounds is not None else cfg.rounds
    max_hits = args.max_hits if args.max_hits is not None else cfg.max_hits
    if not args.yes and not args.quiet:
        _p(f"\n即将对拍 {problem_dir.name}: 轮数 {_rounds_text(rounds)}, "
           f"TLE={args.tl_ms or cfg.tl_ms}ms, "
           f"MLE={args.ml_mb or cfg.ml_mb}MB, "
           f"停止条件={_hits_text(max_hits)}")
        if not _ask("开始？(y/n)", "y").lower().startswith("y"):
            _p("已取消")
            if wait:
                _wait_key()
            return 0
    return _run_here(args, problem_dir, cfg, wait=wait)


def _wizard_worker(args) -> int:
    """独立窗口模式：直接进入配置向导，配完确认后在本窗口开拍。"""
    if not args.problem:
        _p("--wizard 必须配合 -p 使用")
        return 1
    problem_dir = TEST_ROOT / args.problem
    if not problem_dir.is_dir():
        _p(f"找不到题目目录: {problem_dir}")
        _wait_key()
        return 1
    _set_console_title(f"auto-hack | {problem_dir.name} 配置")
    _widen_console()
    _p(BANNER)
    return _configure_and_run(args, problem_dir, wait=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.worker:
        return _worker(args)
    if args.wizard:
        return _wizard_worker(args)

    _p(BANNER)

    if args.list:
        ready, blocked, states = _split_problems()
        if not ready and not blocked:
            _p(f"{TEST_ROOT} 下还没有题目")
            return 0
        _print_index(ready, blocked, states)
        return 0

    problem_dir = _select_problem(args)
    if problem_dir is None:
        return 1

    cfg = _load_cfg(problem_dir)
    need_wizard = cfg is None or args.reconfig
    if not need_wizard and not args.yes and not args.quiet:
        _p(f"题目 {problem_dir.name} 已有配置 ({problem_dir / 'config.yaml'})")
        if not _ask("直接开始对拍？(y/n)", "y").lower().startswith("y"):
            need_wizard = True

    if need_wizard:
        # 没配置 / 要重新配置：新开一个窗口直接进入配置向导，
        # 配完在那个窗口确认并开拍（--no-window 时留在当前窗口）
        if args.no_window:
            return _configure_and_run(args, problem_dir, wait=False)
        return _spawn_window(args, problem_dir, cfg, wizard=True)

    rounds = args.rounds if args.rounds is not None else cfg.rounds
    max_hits = args.max_hits if args.max_hits is not None else cfg.max_hits

    if not args.yes and not args.quiet:
        _p(f"\n即将对拍 {problem_dir.name}: 轮数 {_rounds_text(rounds)}, "
           f"TLE={args.tl_ms or cfg.tl_ms}ms, MLE={args.ml_mb or cfg.ml_mb}MB, "
           f"停止条件={_hits_text(max_hits)}")
        if not _ask("开始？(y/n)", "y").lower().startswith("y"):
            _p("已取消")
            return 0

    if args.no_window:
        return _run_here(args, problem_dir, cfg, wait=False)
    return _spawn_window(args, problem_dir, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
