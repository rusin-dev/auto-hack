"""按 AST 识别出的输入结构 + 用户给定的取值范围生成随机数据。"""

from __future__ import annotations

import random

from .asts import Scan, VarSpec
from .expr import evaluate, try_evaluate

DEFAULT_INT_MIN = 1
DEFAULT_INT_MAX = 10 ** 9
DEFAULT_STR_MIN = 1
DEFAULT_STR_MAX = 10
MAX_ELEMENTS = 5_000_000          # 单条数据的元素上限，防止范围填错卡死


# --------------------------------------------------------------------------
# 生成计划
# --------------------------------------------------------------------------

def build_plan(scan: Scan, specs: list[VarSpec]) -> list:
    """把读入语句编译成一棵生成计划（行 / 数组 / 字符串 / 重复块）。"""
    spec_map = {s.name: s for s in specs}
    if scan.repeat is not None:
        start, end = scan.repeat.start, scan.repeat.end
        pre = [r for r in scan.reads if r.byte < start]
        inside = [r for r in scan.reads if start <= r.byte <= end]
        post = [r for r in scan.reads if r.byte > end]
    else:
        pre, inside, post = list(scan.reads), [], []

    plan: list = _rows_for(pre, spec_map, scan)
    if inside:
        plan.append(("repeat", scan.repeat.var, _rows_for(inside, spec_map, scan)))
    plan.extend(_rows_for(post, spec_map, scan))
    return plan


def _rows_for(reads, spec_map: dict[str, VarSpec], scan: Scan) -> list:
    rows: list = []
    buffer: list[str] = []
    for read in reads:
        spec = spec_map.get(read.name)
        if spec is None:
            decl = scan.decls.get(read.name)
            if decl is not None and decl.is_array:
                spec = VarSpec(read.name, "array", decl.base, line=read.line, op=read.op)
            else:
                spec = VarSpec(read.name, "scalar",
                               decl.base if decl else "int",
                               line=read.line, op=read.op)

        if spec.kind == "scalar":
            buffer.append(spec.name)
            continue
        if buffer:
            rows.append(("line", buffer))
            buffer = []
        if spec.kind == "string":
            rows.append(("string", spec))
        else:
            rows.append(("array", spec))
    if buffer:
        rows.append(("line", buffer))
    return rows


# --------------------------------------------------------------------------
# 取值
# --------------------------------------------------------------------------

def _bounds(rng_range: dict, default_min: int, default_max: int) -> tuple[int, int]:
    try:
        lo = int(rng_range.get("min", default_min))
    except (TypeError, ValueError):
        lo = default_min
    try:
        hi = int(rng_range.get("max", default_max))
    except (TypeError, ValueError):
        hi = default_max
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _int_value(name: str, cfg_vars: dict, rng: random.Random, env: dict) -> int:
    rng_range = cfg_vars.get(name) or {}
    lo, hi = _bounds(rng_range, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    return rng.randint(lo, hi)


def _str_value(name: str, cfg_vars: dict, scan_cfg: dict, rng: random.Random) -> str:
    rng_range = cfg_vars.get(name) or {}
    lo, hi = _bounds(rng_range, DEFAULT_STR_MIN, DEFAULT_STR_MAX)
    length = rng.randint(lo, hi)
    alphabet = scan_cfg.get("alphabet") or "abcdefghijklmnopqrstuvwxyz"
    if not alphabet:
        alphabet = "abcdefghijklmnopqrstuvwxyz"
    return "".join(rng.choice(alphabet) for _ in range(length))


def _level_count(level: dict, env: dict) -> int:
    lo = evaluate(level["lo"], env)
    hi = evaluate(level["hi"], env)
    step = int(level.get("step") or 1)
    if step == 0:
        step = 1
    if step > 0:
        end = hi if level.get("inclusive", True) else hi - 1
        return max(0, (end - lo) // step + 1)
    # 倒序：从 hi 走到 lo，lo/hi 恒为下界/上界（lo <= hi）
    end = lo if level.get("inclusive", True) else lo + 1
    return max(0, (hi - end) // (-step) + 1)


def _array_lines(spec: VarSpec, cfg_vars: dict, rng: random.Random, env: dict,
                 budget: dict) -> list[str]:
    rng_range = cfg_vars.get(spec.name) or {}
    lo, hi = _bounds(rng_range, DEFAULT_INT_MIN, DEFAULT_INT_MAX)

    override = rng_range.get("count")
    if override:
        counts = [max(0, evaluate(override, env))]
    elif spec.levels:
        counts = [_level_count(level, env) for level in spec.levels]
    else:
        count_expr = spec.count_expr
        if not count_expr:
            raise ValueError(
                f"数组 {spec.name} 的元素个数未知，请在 config.yaml 的 "
                f"vars.{spec.name}.count 里填写（例如 n 或 10）"
            )
        counts = [max(0, evaluate(count_expr, env))]

    counts = [max(0, c) for c in counts]
    total = 1
    for c in counts:
        total *= c
    _spend(budget, total,
           f"数组 {spec.name} 要生成 {total} 个元素")
    if total == 0:
        return []

    # 逐维展开：每行放最后一维的元素（读入按 token 进行，行断在哪里都合法），
    # 一维就是一整行，二维就是矩阵，三维及以上按最后一维折行。
    cols = counts[-1]
    rows = total // cols
    return [" ".join(str(rng.randint(lo, hi)) for _ in range(cols))
            for _ in range(rows)]


def _spend(budget: dict, amount: int, what: str) -> None:
    if amount > budget["left"]:
        raise ValueError(
            f"{what}，超过单条数据上限 {MAX_ELEMENTS}；"
            f"请检查数据范围是否填错（例如把 n 的上限填成了 10^9）"
        )
    budget["left"] -= amount


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _emit_rows(rows, cfg_vars: dict, cfg_opts: dict, rng: random.Random,
               env: dict, out: list[str], budget: dict) -> None:
    for row in rows:
        kind = row[0]
        if kind == "line":
            tokens = []
            for name in row[1]:
                _spend(budget, 1, f"变量 {name}")
                value = _int_value(name, cfg_vars, rng, env)
                env[name] = value
                tokens.append(str(value))
            out.append(" ".join(tokens))
        elif kind == "array":
            out.extend(_array_lines(row[1], cfg_vars, rng, env, budget))
        elif kind == "string":
            spec = row[1]
            rng_range = cfg_vars.get(spec.name) or {}
            str_hi = _bounds(rng_range, DEFAULT_STR_MIN, DEFAULT_STR_MAX)[1]
            _spend(budget, max(str_hi, 0), f"字符串 {spec.name}")
            out.append(_str_value(spec.name, cfg_vars, cfg_opts, rng))
        elif kind == "repeat":
            var = row[1]
            if var in env:
                times = int(env[var])
            else:
                times = try_evaluate(var, env, default=1)
            if times < 0:
                raise ValueError(f"重复次数不能为负: {var} = {times}")
            if times > 1_000_000:
                raise ValueError(
                    f"多组测试次数 {var} = {times} 太大，请把 {var} 的范围调小"
                )
            for _ in range(times):
                if budget["left"] <= 0:
                    raise ValueError(
                        f"单条数据过大（超过 {MAX_ELEMENTS} 个元素），"
                        f"请检查多组测试次数与 n 的范围"
                    )
                _emit_rows(row[2], cfg_vars, cfg_opts, rng, env, out, budget)


def generate_with(plan: list, cfg, rng: random.Random) -> bytes:
    env: dict = {}
    out: list[str] = []
    budget = {"left": MAX_ELEMENTS}
    cfg_vars = cfg.vars or {}
    cfg_opts = {"alphabet": cfg.alphabet}
    _emit_rows(plan, cfg_vars, cfg_opts, rng, env, out, budget)
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    if not text:
        text = "\n"
    return text.encode("utf-8")


def plan_text(plan: list, indent: int = 0) -> str:
    """调试用：把生成计划打印成树。"""
    pad = "  " * indent
    lines = []
    for row in plan:
        kind = row[0]
        if kind == "line":
            lines.append(f"{pad}行: {' '.join(row[1])}")
        elif kind == "array":
            spec = row[1]
            if spec.levels:
                desc = ", ".join(f"[{lv['lo']}..{lv['hi']}]"
                                 f"{'' if lv['inclusive'] else '(不含右端)'}"
                                 for lv in spec.levels)
            else:
                desc = f"count={spec.count_expr or '?'}"
            lines.append(f"{pad}数组: {spec.name} {desc}")
        elif kind == "string":
            lines.append(f"{pad}字符串: {row[1].name}")
        elif kind == "repeat":
            lines.append(f"{pad}重复 {row[1]} 次:")
            lines.append(plan_text(row[2], indent + 1))
    return "\n".join(lines)
