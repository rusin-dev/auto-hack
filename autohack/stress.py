"""对拍主流程：生成数据 → 运行标程与被 hack 程序 → 判定 → 落盘。"""

from __future__ import annotations

import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import style
from .asts import Scan, resolve_vars
from .asts import scan as scan_source
from .config import Config, config_path
from .generator import build_plan, generate_with, plan_text
from .progress import RoundInfo, fmt_duration, fmt_mb, fmt_ms
from .runner import BuildError, compile_cpp, run_generator, run_program

HACK_DIR_NAME = "hack"

UNLIMITED = 0          # rounds / max_hits 为 0 表示无休止


def _resolve_threads(value) -> int:
    """把用户给的线程数收敛成 >= 1 的整数（脏配置 / 0 / 负数都兜住）。"""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


@dataclass
class Hit:
    index: int
    data: bytes
    reference: bytes
    reasons: list[str]
    round_index: int = 0       # 命中发生在第几轮（多线程下完成顺序不定，落盘前按它排序）


@dataclass
class StressReport:
    problem: str
    rounds: int = 0
    threads: int = 1
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
           on_round=None, log=None, threads: int | None = None) -> StressReport:
    """执行对拍。

    ``rounds`` / ``max_hits`` 传 0 表示无休止。
    ``threads`` > 1 时并行跑多路（默认取 ``cfg.threads``）：轮号按
    1..N 依次领取，所以同一个种子下单轮数据与单线程完全一致。
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
    say(style.ok(f"  编译完成: {binaries['std'].name}, {binaries['hack'].name}"))

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
        say("  " + style.dim("输入结构:") + "\n" + plan_text(plan, 2))

    tl_seconds = max(cfg.tl_ms / 1000.0, 0.05)
    std_timeout = max(tl_seconds * 10.0, 10.0)
    # 关掉 TLE 检查时放宽超时：否则慢程序会在 tl 附近被杀掉，
    # 而 RE/WA 判定又都要求“没有超时”，导致这轮永远判不出任何命中。
    hack_timeout = (tl_seconds + 0.2 if effective_checks.get("tle")
                    else max(tl_seconds * 10.0, 10.0))

    stats = {"wa": 0, "tle": 0, "mle": 0, "re": 0, "std_fail": 0}
    agg = {"std_ms_sum": 0.0, "std_ms_max": 0.0,
           "hack_ms_sum": 0.0, "hack_ms_max": 0.0, "peak_max": 0.0}
    report.agg = agg

    rounds_desc = "无休止" if report.unlimited else f"{total_rounds} 轮"
    workers = _resolve_threads(cfg.threads if threads is None else threads)
    report.threads = workers
    say(style.title(f"  开始对拍: {rounds_desc}, TLE={cfg.tl_ms}ms, MLE={cfg.ml_mb}MB, "
        f"停止条件={'无限制' if limit_hits <= 0 else str(limit_hits) + ' 个 hack 点'}"
        + (f", 线程={workers}" if workers > 1 else "")))

    # ---- 并发控制 ----
    #  claim_lock : 轮号分配（report.rounds 保持“跑到第 N 轮”的口径，与单线程一致）
    #  state_lock : stats / agg / hits 等共享状态；进度条回调与日志也在它保护下，
    #              保证 on_round 按完成序号顺序送达（否则进度条会倒退）
    #  stop       : 置位后不再领取新一轮（攒够命中 / 中止 / Ctrl+C）
    claim_lock = threading.Lock()
    state_lock = threading.Lock()
    stop = threading.Event()
    claimed = 0                          # 已领取的轮次
    completed = 0                        # 已判定完的轮次（进度条用它，天然单调）

    def claim() -> int | None:
        nonlocal claimed
        with claim_lock:
            if stop.is_set():
                return None
            if not report.unlimited and claimed >= total_rounds:
                return None
            claimed += 1
            report.rounds = claimed
            return claimed

    def abort(message: str) -> None:
        with state_lock:
            if not report.aborted:
                report.aborted = message      # 第一个失败原因最有参考价值
        stop.set()

    def run_round(index: int) -> None:
        nonlocal completed
        # ---- 生成数据（rng 按轮号播种，结果与线程调度无关）----
        try:
            if gen_call is not None:
                data = gen_call(index)
            else:
                rng = (random.Random(cfg.seed + index) if cfg.seed is not None
                       else random.Random())
                data = generate_with(plan, cfg, rng)
        except Exception as exc:                       # noqa: BLE001
            abort(f"生成数据失败(第 {index} 轮): {exc}")
            return

        if stop.is_set():          # 其它线程已中止/到上限，省掉无谓的运行
            return

        std_res = run_program(binaries["std"], data, timeout=std_timeout)
        if not std_res.ok:
            with state_lock:
                stats["std_fail"] += 1
            abort(
                f"标程 std.cpp 在第 {index} 轮运行失败: {std_res.message}\n"
                f"  标程 stderr:\n{std_res.stderr.decode('utf-8', 'replace')[:500]}"
            )
            return

        if stop.is_set():
            return

        hack_res = run_program(binaries["hack"], data,
                               timeout=hack_timeout,
                               ml_mb=cfg.ml_mb if effective_checks.get("mle") else None)

        # ---- 判定：结果先落在本地，共享状态统一加锁更新 ----
        reasons: list[str] = []
        bumps: dict[str, int] = {}
        if effective_checks.get("tle") and hack_res.tle:
            reasons.append(f"TLE ({cfg.tl_ms} ms)")
            bumps["tle"] = bumps.get("tle", 0) + 1
        if effective_checks.get("mle") and hack_res.mle:
            reasons.append(f"MLE ({cfg.ml_mb} MB)")
            bumps["mle"] = bumps.get("mle", 0) + 1
        if effective_checks.get("re") and hack_res.crash and not hack_res.tle:
            reasons.append(f"RE (exit {hack_res.rc})")
            bumps["re"] = bumps.get("re", 0) + 1
        if effective_checks.get("wa") and not hack_res.tle and not hack_res.crash \
                and not hack_res.mle and not outputs_equal(std_res.stdout, hack_res.stdout):
            reasons.append("WA")
            bumps["wa"] = bumps.get("wa", 0) + 1

        with state_lock:
            agg["std_ms_sum"] += std_res.ms
            agg["hack_ms_sum"] += hack_res.ms
            agg["std_ms_max"] = max(agg["std_ms_max"], std_res.ms)
            agg["hack_ms_max"] = max(agg["hack_ms_max"], hack_res.ms)
            if hack_res.peak_mb:
                agg["peak_max"] = max(agg["peak_max"], hack_res.peak_mb)

            # 已经攒够命中数时，在途的这一轮不再落盘/计数，
            # 这样 max_hits 的语义在多线程下与单线程完全一致。
            recorded = bool(reasons) and (limit_hits <= 0
                                          or len(report.hits) < limit_hits)
            if recorded:
                for key, value in bumps.items():
                    stats[key] += value
                report.hits.append(Hit(len(report.hits) + 1, data,
                                       std_res.stdout, list(reasons),
                                       round_index=index))
                if limit_hits > 0 and len(report.hits) >= limit_hits:
                    stop.set()

            completed += 1
            done = completed
            hits_now = len(report.hits)
            stats_now = dict(stats)

            # 回调与日志在 state_lock 内：既避免行交错，也保证按完成序号顺序送达
            if on_round is not None:
                on_round(RoundInfo(
                    index=done,        # 用已完成轮数：并行时完成顺序乱，它才单调
                    hits=hits_now,
                    std_ms=std_res.ms,
                    hack_ms=hack_res.ms,
                    peak_mb=hack_res.peak_mb,
                    reasons=list(reasons),
                    stats=stats_now,
                    unlimited=report.unlimited,
                ))

            if recorded:
                say(style.ok(f"  [第 {index} 轮] 命中 hack 点: {', '.join(reasons)}"))

            if on_round is None and verbose and not stop.is_set() and (
                    done % 50 == 0
                    or (not report.unlimited and done == total_rounds)):
                # 用已完成轮数而不是轮号：多线程下 8 号轮可能先跑完，
                # 用轮号会提前打出“已跑 8/8 轮”
                say(style.dim(f"  ... 已跑 {done}"
                    + (f"/{total_rounds}" if not report.unlimited else "")
                    + f" 轮, 命中 {hits_now}"))

    def loop() -> None:
        while True:
            index = claim()
            if index is None:
                return
            run_round(index)

    pool: list[threading.Thread] = []

    def drain(tolerate: bool) -> None:
        """等在跑的线程收尾；tolerate=True 时忽略等待期间重复的 Ctrl+C。"""
        while True:
            alive = [thread for thread in pool if thread.is_alive()]
            if not alive:
                return
            try:
                for thread in alive:
                    thread.join(timeout=0.1)
            except KeyboardInterrupt:
                if not tolerate:
                    raise
                # 每个阶段都有超时兜底，继续等必然能收尾

    try:
        if workers <= 1:
            loop()
        else:
            def worker() -> None:
                try:
                    loop()
                except KeyboardInterrupt:         # 信号只投递给主线程，防御性兜底
                    stop.set()
                except Exception as exc:           # noqa: BLE001
                    abort(f"对拍线程异常退出: {exc}")

            pool = [threading.Thread(target=worker, daemon=True,
                                     name=f"autohack-stress-{i + 1}")
                    for i in range(workers)]
            for thread in pool:
                thread.start()
            drain(tolerate=False)
    except KeyboardInterrupt:
        report.stopped = True
        stop.set()
        say(style.warn("  已手动停止 (Ctrl+C)"
            + ("，等待进行中的轮次结束..." if any(t.is_alive() for t in pool) else "")))
        drain(tolerate=True)

    report.stats = stats
    report.elapsed = time.perf_counter() - started
    report.speed = (report.rounds / report.elapsed) if report.elapsed > 0 else 0.0

    if report.hits:
        # 多线程下命中按“完成顺序”追加，这里按轮号排回去并重新编号，
        # 001.in / hits.txt 的顺序才与单线程（以及同种子的上次运行）一致。
        report.hits.sort(key=lambda hit: hit.round_index)
        for number, hit in enumerate(report.hits, 1):
            hit.index = number
        report.output_dir = save_hits(repo_root, problem_dir, cfg, report.hits)
        say(style.ok(f"  结果已保存到 {report.output_dir}"))
    elif not report.aborted:
        say(style.warn(f"  {rounds_desc}内没有找到 hack 点，可加大轮数或放宽/收紧取值范围"))

    return report


# --------------------------------------------------------------------------
# 落盘：hack/<题目>/ 与 test/<题目>/ 保持同样的结构，并加上 .in / .out
# --------------------------------------------------------------------------

def save_hits(repo_root: Path, problem_dir: Path, cfg: Config, hits: list[Hit]) -> Path:
    repo_root = Path(repo_root)
    problem_dir = Path(problem_dir)
    out_dir = repo_root / HACK_DIR_NAME / problem_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)

    # 先清掉上一次运行留下的命中文件，避免新旧结果混在一起
    for pattern in ("*.in", "*.out"):
        for old in out_dir.glob(pattern):
            old.unlink(missing_ok=True)
    (out_dir / "hits.txt").unlink(missing_ok=True)

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
    line = style.dim("-" * 58)

    emit(line)
    if report.aborted:
        emit(style.bad(f"中止: {report.aborted}"))

    if report.unlimited:
        mode = ("无休止" + ("，手动停止" if report.stopped else ""))
        mode = style.warn(mode) if report.stopped else mode
    else:
        mode = style.warn("手动停止") if report.stopped else style.ok("正常结束")
    emit(f"题目 {report.problem}  |  {mode}")

    hits_text = (style.ok(f"命中: {len(report.hits)} 个")
                 if report.hits else f"命中: {len(report.hits)} 个")
    emit(f"  轮数: {report.rounds}    {hits_text}    "
         f"总用时: {fmt_duration(report.elapsed)}    "
         f"平均速度: {report.speed:.1f} 轮/秒"
         + (f"    线程: {report.threads}" if report.threads > 1 else ""))

    if report.stats:
        bits = []
        for key in ("wa", "tle", "mle", "re", "std_fail"):
            count = report.stats.get(key)
            if count:
                text = f"{key.upper()}={count}"
                # 标程自己挂了是坏消息，其余命中都是好消息
                bits.append(style.bad(text) if key == "std_fail" else style.ok(text))
        emit("  命中原因: " + ("  ".join(bits) if bits else style.dim("无")))

    agg = report.agg or {}
    timed = max(1, report.rounds)
    emit(f"  标程 std   平均 {fmt_ms(agg.get('std_ms_sum', 0) / timed)}"
         f" / 最大 {fmt_ms(agg.get('std_ms_max'))}")
    emit(f"  被测 hack  平均 {fmt_ms(agg.get('hack_ms_sum', 0) / timed)}"
         f" / 最大 {fmt_ms(agg.get('hack_ms_max'))}"
         f"    峰值内存 {fmt_mb(agg.get('peak_max'))} (限制 {report.ml_mb} MB)"
         f"    TLE 限制 {report.tl_ms} ms")

    if report.hits:
        emit(style.ok(f"  命中明细 ({len(report.hits)}):"))
        for hit in report.hits[:20]:
            emit(f"    {hit.index:03d}  {style.ok(' '.join(hit.reasons))}")
        if len(report.hits) > 20:
            emit(style.dim(f"    ... 另外 {len(report.hits) - 20} 个"))

    for warning in report.warnings:
        emit(style.warn(f"  提示: {warning}"))
    if report.output_dir:
        emit(f"  {style.dim('hack 数据:')} {report.output_dir}")
    emit(line)
