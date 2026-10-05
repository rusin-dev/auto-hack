"""安全地求值 C++ 侧的简单整数表达式，例如 n、n+1、n*2-1。"""

from __future__ import annotations

import ast as _ast


class ExprError(ValueError):
    pass


def evaluate(expr: str | float, env: dict) -> int:
    """把形如 ``n+1`` 的表达式按 ``env`` 求值。只允许整数常量与已知变量。"""
    if isinstance(expr, (int, float)):
        return int(expr)
    text = str(expr).strip()
    if not text:
        raise ExprError("空表达式")
    try:
        node = _ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"无法解析表达式 {expr!r}: {exc}") from exc
    return int(_eval(node.body, env, text))


def _eval(node, env: dict, src: str) -> int:
    if isinstance(node, _ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExprError(f"表达式 {src!r} 含非数值常量")
        return int(node.value)
    if isinstance(node, _ast.Name):
        if node.id not in env:
            raise ExprError(f"表达式 {src!r} 用到未知变量 {node.id!r}")
        return int(env[node.id])
    if isinstance(node, _ast.UnaryOp) and isinstance(node.op, (_ast.UAdd, _ast.USub, _ast.Invert)):
        val = _eval(node.operand, env, src)
        return val if isinstance(node.op, _ast.UAdd) else -val if isinstance(node.op, _ast.USub) else ~val
    if isinstance(node, _ast.BinOp):
        left = _eval(node.left, env, src)
        right = _eval(node.right, env, src)
        op = node.op
        if isinstance(op, _ast.Add):
            return left + right
        if isinstance(op, _ast.Sub):
            return left - right
        if isinstance(op, _ast.Mult):
            return left * right
        if isinstance(op, _ast.FloorDiv):
            if right == 0:
                raise ExprError(f"表达式 {src!r} 除零")
            return left // right
        if isinstance(op, _ast.Div):
            if right == 0:
                raise ExprError(f"表达式 {src!r} 除零")
            return int(left / right)
        if isinstance(op, _ast.Mod):
            if right == 0:
                raise ExprError(f"表达式 {src!r} 模零")
            return left % right
        if isinstance(op, _ast.Pow):
            if right < 0 or right > 64:
                raise ExprError(f"表达式 {src!r} 幂次越界")
            return left ** right
        raise ExprError(f"表达式 {src!r} 含不支持的运算符 {type(op).__name__}")
    raise ExprError(f"表达式 {src!r} 含不支持的节点 {type(node).__name__}")


def try_evaluate(expr: str | float, env: dict, default: int = 0) -> int:
    try:
        return evaluate(expr, env)
    except ExprError:
        return default
