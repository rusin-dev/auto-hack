"""对拍主流程：生成数据 → 运行标程与被 hack 程序 → 判定 → 落盘。"""

from __future__ import annotations

import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .asts import Scan, resolve_vars
from .asts import scan as scan_source
from .config import Config, config_path
from .generator import build_plan, generate_with, plan_text
from .progress import RoundInfo, fmt_duration, fmt_mb, fmt_ms
from .runner import BuildError, compile_cpp, run_generator, run_program

HACK_DIR_NAME = "hack"

UNLIMITED = 0          # rounds / max_hits 为 0 表示无休止


@dataclass
class Hit:
    index: int
    data: bytes
    reference: bytes
    reasons: list[str]


@dataclass
class StressReport:
    problem: str
    rounds: int = 0
    hits: list[Hit] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    aborted: str = ""
    warnings: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    stopped: bool = False
    unlimited: bool = False
    tl_ms: int = 0
    ml_mb: int = 0
    elapsed: float = 0.0
    speed: float = 0.0
    agg: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return bool(self.hits) and not self.aborted


# --------------------------------------------------------------------------
# 归一化输出（与 Codeforces 一致：忽略行尾空白与末尾空行）
# --------------------------------------------------------------------------

def normalize(data: bytes) -> list[bytes]:
    lines = [line.rstrip(b" \t\r") for line in data.split(b"\n")]
    while lines and lines[-1] == b"":
        lines.pop()
    return lines


def outputs_equal(a: bytes, b: bytes) -> bool:
    return normalize(a) == normalize(b)


# --------------------------------------------------------------------------
# 准备
# --------------------------------------------------------------------------

def prepare_build(problem_dir: Path, build_dir: Path) -> dict[str, Path]:
    problem_dir = Path(problem_dir)
    build_dir = Path(build_dir)
    bins = {}
    for key in ("std", "hack"):
        src = problem_dir / f"{key}.cpp"
        bins[key] = compile_cpp(src, build_dir / key)
    return bins


def prepare_generator(cfg: Config, problem_dir: Path, build_dir: Path):
    """返回 callable(round_index) -> bytes，或 None（表示用内置生成器）。"""
    if cfg.kind != "custom" and not cfg.gen:
        return None

    problem_dir = Path(problem_dir)
    gen_rel = cfg.gen or "gen.cpp"
    gen_path = problem_dir / gen_rel
    if not gen_path.is_file():
        for cand in ("gen.cpp", "gen.py", "generator.cpp", "generator.py"):
            if (problem_dir / cand).is_file():
                gen_path = problem_dir / cand
                break
    if not gen_path.is_file():
        raise BuildError(
            f"题目 {problem_dir.name} 被标记为自定义生成器，但没有找到生成器文件。\n"
            f"请把 gen.cpp（或 gen.py）放到 {problem_dir} 目录下，或修改 config.yaml 的 gen 字段。"
        )

    if gen_path.suffix.lower() == ".py":
        import sys as _sys
        base_cmd = [_sys.executable, str(gen_path)]
    else:
        exe = compile_cpp(gen_path, build_dir / "gen")
        base_cmd = [str(exe)]

    def call(round_index: int) -> bytes:
        cmd = list(base_cmd) + [str(round_index)]
        try:
            return run_generator(cmd, cwd=problem_dir, timeout=15.0)
        except BuildError as exc:
            if "returned" in str(exc) or "timeout" in str(exc).lower():
                cmd = list(base_cmd)
                return run_generator(cmd, cwd=problem_dir, timeout=15.0)
            raise

    return call


# --------------------------------------------------------------------------
# 对拍
# --------------------------------------------------------------------------

def stress(problem_dir: Path, cfg: Config, build_root: Path,
           repo_root: Path, rounds: int | None = None,
           seed: int | None = None, checks: dict | None = None,
           max_hits: int | None = None, verbose: bool = True,
           on_round=None, log=None) -> StressReport:
    """执行对拍。

    ``rounds`` / ``max_hits`` 传 0 表示无休止。
    ``on_round`` 每跑完一轮回调一次 :class:`RoundInfo`，用于渲染进度条。
    ``log`` 覆盖日志输出（进度条需要先擦行再打印）。
    """
    problem_dir = Path(problem_dir)
    repo_root = Path(repo_root)
    report = StressReport(problem=problem_dir.name)
    report.tl_ms = cfg.tl_ms
    report.ml_mb = cfg.ml_mb

    effective_checks = dict(cfg.checks)
    if checks:
        effective_checks.update({k: bool(v) for k, v in checks.items() if v is not None})

    total_rounds = int(rounds if rounds is not None else cfg.rounds)
    limit_hits = int(max_hits if max_hits is not None else cfg.max_hits)
    report.unlimited = total_rounds <= 0
    if seed is not None:
        cfg.seed = seed

    def say(msg: str) -> None:
        if not verbose:
            return
        if log is not None:
            log(msg)
        else:
            print(msg, flush=True)

    started = time.perf_counter()          # 与进度条同一时间基准（含编译耗时）

    try:
        binaries = prepare_build(problem_dir, build_root)
    except BuildError as exc:
        report.aborted = str(exc)
        return report
    say(f"  编译完成: {binaries['std'].name}, {binaries['hack'].name}")

    gen_call = None
    try:
        gen_call = prepare_generator(cfg, problem_dir, build_root)
    except BuildError as exc:
        report.aborted = str(exc)
        return report

    plan = None
    if gen_call is None:
        try:
            source = (problem_dir / "std.cpp").read_text(encoding="utf-8", errors="replace")
            scanned: Scan = scan_source(source)
            specs, warns = resolve_vars(scanned)
            report.warnings.extend(warns)
            missing = sorted({s.name for s in specs if s.name not in cfg.vars})
            if missing:
                report.aborted = "缺少取值范围: " + ", ".join(missing) + \
                                 "（请先运行配置向导）"
                return report
            plan = build_plan(scanned, specs)
        except Exception as exc:                       # noqa: BLE001
            report.aborted = f"分析输入结构失败: {exc}"
            return report
        say("  输入结构:\n" + plan_text(plan, 2))

    tl_seconds = max(cfg.tl_ms / 1000.0, 0.05)
    std_timeout = max(tl_seconds * 10.0, 10.0)

    stats = {"wa": 0, "tle": 0, "mle": 0, "re": 0, "std_fail": 0}
    agg = {"std_ms_sum": 0.0, "std_ms_max": 0.0,
           "hack_ms_sum": 0.0, "hack_ms_max": 0.0, "peak_max": 0.0}
    report.agg = agg

    rounds_desc = "无休止" if report.unlimited else f"{total_rounds} 轮"
    say(f"  开始对拍: {rounds_desc}, TLE={cfg.tl_ms}ms, MLE={cfg.ml_mb}MB, "
        f"停止条件={'无限制' if limit_hits <= 0 else str(limit_hits) + ' 个 hack 点'}")

    index = 0
    try:
        while True:
            index += 1
            report.rounds = index

            try:
                if gen_call is not None:
                    data = gen_call(index)
                else:
                    rng = (random.Random(cfg.seed + index) if cfg.seed is not None
                           else random.Random())
                    data = generate_with(plan, cfg, rng)
            except Exception as exc:                   # noqa: BLE001
                report.aborted = f"生成数据失败(第 {index} 轮): {exc}"
                break

            std_res = run_program(binaries["std"], data, timeout=std_timeout)
            if not std_res.ok:
                stats["std_fail"] += 1
                report.aborted = (
                    f"标程 std.cpp 在第 {index} 轮运行失败: {std_res.message}\n"
                    f"  标程 stderr:\n{std_res.stderr.decode('utf-8', 'replace')[:500]}"
                )
                break

            hack_res = run_program(binaries["hack"], data,
                                   timeout=tl_seconds + 0.2,
                                   ml_mb=cfg.ml_mb if effective_checks.get("mle") else None)

            reasons: list[str] = []
            if effective_checks.get("tle") and hack_res.tle:
                reasons.append(f"TLE ({cfg.tl_ms} ms)")
                stats["tle"] += 1
            if effective_checks.get("mle") and hack_res.mle:
                reasons.append(f"MLE ({cfg.ml_mb} MB)")
                stats["mle"] += 1
            if effective_checks.get("re") and hack_res.crash and not hack_res.tle:
                reasons.append(f"RE (exit {hack_res.rc})")
                stats["re"] += 1
            if effective_checks.get("wa") and not hack_res.tle and not hack_res.crash \
                    and not hack_res.mle and not outputs_equal(std_res.stdout, hack_res.stdout):
                reasons.append("WA")
                stats["wa"] += 1

            agg["std_ms_sum"] += std_res.ms
            agg["hack_ms_sum"] += hack_res.ms
            agg["std_ms_max"] = max(agg["std_ms_max"], std_res.ms)
            agg["hack_ms_max"] = max(agg["hack_ms_max"], hack_res.ms)
            if hack_res.peak_mb:
                agg["peak_max"] = max(agg["peak_max"], hack_res.peak_mb)

            if reasons:
                report.hits.append(Hit(len(report.hits) + 1, data, std_res.stdout, reasons))

            if on_round is not None:
                on_round(RoundInfo(
                    index=index,
                    hits=len(report.hits),
                    std_ms=std_res.ms,
                    hack_ms=hack_res.ms,
                    peak_mb=hack_res.peak_mb,
                    reasons=list(reasons),
                    stats=dict(stats),
                    unlimited=report.unlimited,
                ))

            if reasons:
                say(f"  [第 {index} 轮] 命中 hack 点: {', '.join(reasons)}")
                if limit_hits > 0 and len(report.hits) >= limit_hits:
                    break

            if on_round is None and verbose and (index % 50 == 0
                                                or (not report.unlimited and index == total_rounds)):
                say(f"  ... 已跑 {index}"
                    + (f"/{total_rounds}" if not report.unlimited else "")
                    + f" 轮, 命中 {len(report.hits)}")

            if not report.unlimited and index >= total_rounds:
                break
    except KeyboardInterrupt:
        report.stopped = True
        say("  已手动停止 (Ctrl+C)")

    report.stats = stats
    report.elapsed = time.perf_counter() - started
    report.speed = (report.rounds / report.elapsed) if report.elapsed > 0 else 0.0

    if report.hits:
        report.output_dir = save_hits(repo_root, problem_dir, cfg, report.hits)
        say(f"  结果已保存到 {report.output_dir}")
    elif not report.aborted:
        say(f"  {rounds_desc}内没有找到 hack 点，可加大轮数或放宽/收紧取值范围")

    return report


# --------------------------------------------------------------------------
# 落盘：hack/<题目>/ 与 test/<题目>/ 保持同样的结构，并加上 .in / .out
# --------------------------------------------------------------------------

def save_hits(repo_root: Path, problem_dir: Path, cfg: Config, hits: list[Hit]) -> Path:
    repo_root = Path(repo_root)
    problem_dir = Path(problem_dir)
    out_dir = repo_root / HACK_DIR_NAME / problem_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in ("std.cpp", "hack.cpp"):
        src = problem_dir / name
        if src.is_file():
            shutil.copy2(src, out_dir / name)
    cfg_file = config_path(problem_dir)
    if cfg_file.is_file():
        shutil.copy2(cfg_file, out_dir / cfg_file.name)

    for hit in hits:
        (out_dir / f"{hit.index:03d}.in").write_bytes(hit.data)
        (out_dir / f"{hit.index:03d}.out").write_bytes(hit.reference)

    log_lines = [
        f"problem: {cfg.problem or problem_dir.name}",
        f"tl_ms: {cfg.tl_ms}",
        f"ml_mb: {cfg.ml_mb}",
        "",
    ]
    for hit in hits:
        log_lines.append(f"{hit.index:03d}  {' '.join(hit.reasons)}")
    (out_dir / "hits.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return out_dir


def list_problems(test_root: Path) -> list[Path]:
    test_root = Path(test_root)
    if not test_root.is_dir():
        return []
    result = []
    for child in sorted(test_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "std.cpp").is_file() and (child / "hack.cpp").is_file():
            result.append(child)
    return result


def scan_problems(test_root: Path) -> list[Path]:
    """列出 test/ 下所有题目目录（不判断源码、配置是否齐全）。"""
    test_root = Path(test_root)
    if not test_root.is_dir():
        return []
    return sorted(child for child in test_root.iterdir() if child.is_dir())


# --------------------------------------------------------------------------
# 结束报告
# --------------------------------------------------------------------------

def print_report(report: StressReport, log=None) -> None:
    emit = log if log is not None else (lambda text="": print(text, flush=True))
    line = "-" * 58

    emit(line)
    if report.aborted:
        emit(f"中止: {report.aborted}")

    if report.unlimited:
        mode = "无休止" + ("，手动停止" if report.stopped else "")
    else:
        mode = "手动停止" if report.stopped else "正常结束"
    emit(f"题目 {report.problem}  |  {mode}")
    emit(f"  轮数: {report.rounds}    命中: {len(report.hits)} 个    "
         f"总用时: {fmt_duration(report.elapsed)}    "
         f"平均速度: {report.speed:.1f} 轮/秒")

    if report.stats:
        bits = [f"{key.upper()}={report.stats.get(key, 0)}"
                for key in ("wa", "tle", "mle", "re", "std_fail")
                if report.stats.get(key)]
        emit("  命中原因: " + ("  ".join(bits) if bits else "无"))

    agg = report.agg or {}
    timed = max(1, report.rounds)
    emit(f"  标程 std   平均 {fmt_ms(agg.get('std_ms_sum', 0) / timed)}"
         f" / 最大 {fmt_ms(agg.get('std_ms_max'))}")
    emit(f"  被测 hack  平均 {fmt_ms(agg.get('hack_ms_sum') / timed)}"
         f" / 最大 {fmt_ms(agg.get('hack_ms_max'))}"
         f"    峰值内存 {fmt_mb(agg.get('peak_max'))} (限制 {report.ml_mb} MB)"
         f"    TLE 限制 {report.tl_ms} ms")

    if report.hits:
        emit(f"  命中明细 ({len(report.hits)}):")
        for hit in report.hits[:20]:
            emit(f"    {hit.index:03d}  {' '.join(hit.reasons)}")
        if len(report.hits) > 20:
            emit(f"    ... 另外 {len(report.hits) - 20} 个")

    for warning in report.warnings:
        emit(f"  提示: {warning}")
    if report.output_dir:
        emit(f"  hack 数据: {report.output_dir}")
    emit(line)
