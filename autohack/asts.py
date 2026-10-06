"""用 tree-sitter 解析 C++ 源码，识别输入变量与读入结构。

输出三类信息：
1. decls   —— 变量声明（标量 / 数组 / vector / string）
2. reads   —— 按源码顺序排列的读入语句（cin >> / scanf / getline）
3. repeat  —— 多组测试循环（如 while(t--)），其内部的读入需要重复生成
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_cpp
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_cpp.language())

_FMT_RE = re.compile(
    r"%[-+ #0']*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|z|j|t)?"
    r"[diouxXeEfFgGaAcspn]"
)
_WORD_RE = re.compile(r"[A-Za-z_]\w*")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_CMP_OPS = ("<", "<=", ">", ">=")
_MIRROR_OPS = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}

_VECTOR_BASES = {"vector", "array", "deque", "list", "set", "map"}
_STRING_BASES = {"string", "wstring", "char_sequence"}
_CHAR_BASES = {"char"}
_NUMERIC_HINTS = {
    "int", "long", "short", "char", "double", "float", "signed",
    "unsigned", "size_t", "auto", "bool", "ll", "i64", "u64", "__int128",
}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Decl:
    name: str
    base: str
    dims: list[str] = field(default_factory=list)
    line: int = 0

    @property
    def is_string(self) -> bool:
        return self.base in _STRING_BASES

    @property
    def is_array(self) -> bool:
        return bool(self.dims) or self.base in _VECTOR_BASES


@dataclass
class Read:
    op: str                 # cin / scanf / getline
    name: str
    indexes: list[str] = field(default_factory=list)
    spec: str = ""          # scanf 的格式符，如 d / s / c
    line: int = 1
    byte: int = 0
    raw: str = ""
    repeat: bool = False


@dataclass
class Loop:
    start: int
    end: int
    kind: str                   # for / while
    var: str | None = None
    lo: int | None = None               # 起始值的字面量形式（非字面量时为 None）
    lo_expr: str | None = None
    hi_expr: str | None = None
    inclusive: bool = True
    step: int = 1
    cond: str = ""

    @property
    def has_bounds(self) -> bool:
        return self.lo_expr is not None and self.hi_expr is not None


@dataclass
class RepeatInfo:
    var: str
    start: int
    end: int
    line: int


@dataclass
class Scan:
    source: str
    reads: list[Read]
    decls: dict[str, Decl]
    loops: list[Loop]
    repeat: RepeatInfo | None
    warnings: list[str]


@dataclass
class VarSpec:
    """一个输入变量的生成规格。"""
    name: str
    kind: str                   # scalar / array / string
    base: str
    levels: list[dict] = field(default_factory=list)   # 数组各维的下标范围
    count_expr: str | None = None                       # 无下标信息时的元素个数表达式
    line: int = 1
    op: str = "cin"

    @property
    def needs_count(self) -> bool:
        return self.kind == "array" and not self.levels and not self.count_expr


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _text(node: Node | None, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _walk(node: Node):
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _same(a: Node | None, b: Node | None) -> bool:
    """tree-sitter 每次访问都会新建 Node 包装对象，只能按位置比较。"""
    if a is None or b is None:
        return a is b
    return (a.start_byte, a.end_byte, a.type) == (b.start_byte, b.end_byte, b.type)


def _line_of(source: str, byte: int) -> int:
    return source.count("\n", 0, byte) + 1


def _binop(node: Node, src: bytes):
    if node.type != "binary_expression" or len(node.children) < 3:
        return None, None, None
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None:
        return None, None, None
    return node.children[1].type, left, right


def _base_type(type_node: Node | None, src: bytes) -> str:
    txt = _text(type_node, src).strip()
    if not txt:
        return "int"
    txt = txt.removeprefix("std::")
    if "<" in txt:
        txt = txt.split("<", 1)[0]
    txt = txt.split()[0] if txt.split() else txt
    return txt


def _int_or_none(txt: str) -> int | None:
    txt = txt.strip().rstrip("lLuU")
    try:
        return int(txt, 0)
    except ValueError:
        return None


def _declarator_children(node: Node) -> list[Node]:
    return [
        c for c in node.children
        if c.type in ("identifier", "init_declarator", "array_declarator",
                      "pointer_declarator", "reference_declarator")
    ]


# --------------------------------------------------------------------------
# 声明收集
# --------------------------------------------------------------------------

def _vector_dims(args: Node, src: bytes) -> list[str]:
    dims: list[str] = []
    for kid in args.children:
        if kid.type in ("(", ")", ","):
            continue
        if kid.type == "call_expression":            # vector<int>(m)
            sub = kid.child_by_field_name("arguments")
            inner = [c for c in sub.children if c.type not in ("(", ")", ",")] if sub else []
            dims.append(_text(inner[0], src) if inner else "0")
        else:
            dims.append(_text(kid, src))
    return dims


def _record_decl(node: Node, base: str, decls: dict[str, Decl], src: bytes) -> None:
    ntype = node.type
    if ntype == "identifier":
        name = _text(node, src)
        decls.setdefault(name, Decl(name, base, [], node.start_point[0] + 1))
        return

    if ntype == "init_declarator":
        inner = node.child_by_field_name("declarator")
        value = node.child_by_field_name("value")
        if (value is not None and value.type == "argument_list"
                and base in _VECTOR_BASES and inner is not None):
            name = _text(inner, src)
            decls.setdefault(name, Decl(name, base, _vector_dims(value, src),
                                        node.start_point[0] + 1))
            return
        if inner is not None:
            _record_decl(inner, base, decls, src)
        return

    if ntype == "array_declarator":
        dims: list[str] = []
        cur: Node | None = node
        while cur is not None and cur.type == "array_declarator":
            size = cur.child_by_field_name("size")
            dims.append(_text(size, src) if size is not None else "0")
            cur = cur.child_by_field_name("declarator")
        if cur is None:
            return
        if cur.type == "init_declarator":
            name_node = cur.child_by_field_name("declarator")
            if name_node is not None and name_node.type == "identifier":
                name = _text(name_node, src)
                decls.setdefault(name, Decl(name, base, dims, node.start_point[0] + 1))
            return
        if cur.type == "identifier":
            name = _text(cur, src)
            decls.setdefault(name, Decl(name, base, dims, node.start_point[0] + 1))
        return

    if ntype in ("pointer_declarator", "reference_declarator"):
        inner = node.child_by_field_name("declarator")
        if inner is not None:
            _record_decl(inner, base, decls, src)


def _collect_decls(root: Node, src: bytes) -> dict[str, Decl]:
    decls: dict[str, Decl] = {}
    for node in _walk(root):
        if node.type != "declaration":
            continue
        type_node = node.child_by_field_name("type")
        base = _base_type(type_node, src)
        for child in node.children:
            if _same(child, type_node):
                continue
            if child.type in ("identifier", "init_declarator", "array_declarator",
                              "pointer_declarator", "reference_declarator"):
                _record_decl(child, base, decls, src)
    return decls


# --------------------------------------------------------------------------
# 读入语句收集
# --------------------------------------------------------------------------

def _resolve_target(node: Node, src: bytes) -> tuple[str, list[str]]:
    """把 cin/scanf 的目标表达式还原成 (变量名, 下标表达式列表)。"""
    cur = node
    while cur.type == "pointer_expression" and _text(cur.children[0], src) == "&":
        arg = cur.child_by_field_name("argument")
        if arg is None:
            break
        cur = arg
    while cur.type == "parenthesized_expression":
        kids = [c for c in cur.children if c.type not in ("(", ")")]
        if not kids:
            break
        cur = kids[0]

    if cur.type == "identifier":
        return _text(cur, src), []

    if cur.type == "subscript_expression":
        indexes: list[str] = []
        walker: Node = cur
        while walker.type == "subscript_expression":
            ilist = walker.child_by_field_name("indices")
            piece = _text(ilist, src).strip()
            if piece.startswith("[") and piece.endswith("]"):
                piece = piece[1:-1]
            indexes.append(piece)
            walker = walker.child_by_field_name("argument")  # type: ignore[assignment]
            if walker is None:
                break
        indexes.reverse()
        if walker is not None and walker.type == "identifier":
            return _text(walker, src), indexes
        if walker is not None and walker.type == "field_expression":
            owner = walker.child_by_field_name("argument")
            return (_text(owner, src) if owner is not None else _text(walker, src)), indexes
        return (_text(walker, src) if walker is not None else ""), indexes

    if cur.type == "field_expression":
        owner = cur.child_by_field_name("argument")
        return (_text(owner, src) if owner is not None else _text(cur, src)), []

    if cur.type == "binary_expression":
        op, left, right = _binop(cur, src)
        if op in ("+", "-") and left is not None and right is not None:
            lt, rt = _text(left, src), _text(right, src)
            if _IDENT_RE.fullmatch(lt):
                return lt, [rt]
            if _IDENT_RE.fullmatch(rt):
                return rt, [lt]
        return _text(cur, src), []

    if cur.type == "identifier":
        return _text(cur, src), []
    return _text(cur, src), []


def _cin_chain(node: Node, src: bytes) -> list[Node] | None:
    targets: list[Node] = []
    cur = node
    for _ in range(64):
        op, left, right = _binop(cur, src)
        if op != ">>" or right is None or left is None:
            return None
        targets.append(right)
        if left.type == "binary_expression" and len(left.children) >= 3 \
                and left.children[1].type == ">>":
            cur = left
            continue
        head = _text(left, src).replace(" ", "")
        if head == "cin" or head.endswith("::cin"):
            targets.reverse()
            return targets
        return None
    return None


def _collect_reads(root: Node, src: bytes) -> list[Read]:
    reads: list[Read] = []
    for node in _walk(root):
        if node.type == "binary_expression":
            op, left, _right = _binop(node, src)
            if op != ">>" or left is None:
                continue
            parent = node.parent
            if (parent is not None and parent.type == "binary_expression"
                    and len(parent.children) >= 3
                    and parent.children[1].type == ">>"
                    and _same(parent.child_by_field_name("left"), node)):
                continue                            # 链内层节点，交给最外层处理
            targets = _cin_chain(node, src)
            if not targets:
                continue
            for tgt in targets:
                name, indexes = _resolve_target(tgt, src)
                reads.append(Read("cin", name, indexes, "",
                                  tgt.start_point[0] + 1, tgt.start_byte, _text(tgt, src)))
            continue

        if node.type == "call_expression":
            fn = _text(node.child_by_field_name("function"), src).split("::")[-1].strip()
            args = node.child_by_field_name("arguments")
            if args is None:
                continue
            kids = [c for c in args.children if c.type not in ("(", ")", ",")]

            if fn in ("scanf", "fscanf", "sscanf", "wscanf") and kids:
                if kids[0].type not in ("string_literal", "concatenated_string"):
                    continue
                specs = _FMT_RE.findall(_text(kids[0], src))
                for spec, tgt in zip(specs, kids[1:]):
                    name, indexes = _resolve_target(tgt, src)
                    reads.append(Read("scanf", name, indexes, spec[-1],
                                      tgt.start_point[0] + 1, tgt.start_byte,
                                      _text(tgt, src)))
            elif fn == "getline" and len(kids) >= 2:
                tgt = kids[1]
                name, indexes = _resolve_target(tgt, src)
                reads.append(Read("getline", name, indexes, "",
                                  tgt.start_point[0] + 1, tgt.start_byte, _text(tgt, src)))

    reads.sort(key=lambda r: r.byte)
    return reads


# --------------------------------------------------------------------------
# 循环
# --------------------------------------------------------------------------

def _condition_of(node: Node, src: bytes) -> tuple[Node | None, str]:
    cond = node.child_by_field_name("condition")
    if cond is None:
        return None, ""
    if cond.type == "condition_clause":
        value = cond.child_by_field_name("value")
        return value, _text(cond, src).strip().strip("()").strip()
    return cond, _text(cond, src).strip()


def _parse_for(node: Node, src: bytes) -> Loop:
    loop = Loop(node.start_byte, node.end_byte, "for", cond=_text(
        node.child_by_field_name("condition"), src).strip())

    init = node.child_by_field_name("initializer")
    if init is not None:
        for child in init.children:
            if child.type == "init_declarator":
                ident = child.child_by_field_name("declarator")
                value = child.child_by_field_name("value")
                if ident is not None and value is not None:
                    loop.var = _text(ident, src)
                    num = _int_or_none(_text(value, src))
                    if num is not None:
                        loop.lo = num
                        loop.lo_expr = str(num)
                    else:
                        loop.lo_expr = _text(value, src)
            elif child.type == "expression_statement" and child.children:
                expr = child.children[0]
                if expr.type == "assignment_expression":
                    left = expr.child_by_field_name("left")
                    right = expr.child_by_field_name("right")
                    if left is not None:
                        loop.var = _text(left, src)
                        if right is not None:
                            num = _int_or_none(_text(right, src))
                            loop.lo = num
                            loop.lo_expr = str(num) if num is not None else _text(right, src)

    cond_node, _ = _condition_of(node, src)
    if cond_node is not None and cond_node.type == "binary_expression":
        op, left, right = _binop(cond_node, src)
        if op in _CMP_OPS:
            lt = _text(left, src)
            rt = _text(right, src)
            bound = None
            var_on_left = True
            if loop.var and lt == loop.var:
                bound = rt
            elif loop.var and rt == loop.var:
                bound, var_on_left = lt, False
            elif _IDENT_RE.fullmatch(lt):
                loop.var, bound = lt, rt
            elif _IDENT_RE.fullmatch(rt):
                loop.var, bound, var_on_left = rt, lt, False
            if bound is not None:
                # 循环变量可能写在比较式的右边（0 <= i），方向要先归一
                eff_op = op if var_on_left else _MIRROR_OPS[op]
                if eff_op in ("<", "<="):
                    loop.hi_expr = bound
                    loop.inclusive = eff_op == "<="
                else:                                # i >= k，倒序循环
                    # 起始值（init 或赋值表达式的右侧）成为上界，条件侧成为下界。
                    # 起始值可能是表达式（n-1 这类非字面量），此时用 lo_expr 兜底，
                    # 否则上界会丢失、数组长度推断不出来。
                    start_expr = (str(loop.lo) if loop.lo is not None
                                  else loop.lo_expr)
                    loop.lo = None
                    loop.lo_expr = bound
                    loop.inclusive = eff_op == ">="
                    if start_expr is not None:
                        loop.hi_expr = start_expr

    upd = node.child_by_field_name("update")
    if upd is not None and upd.type == "update_expression":
        arg = upd.child_by_field_name("argument")
        if arg is not None and loop.var is None:
            loop.var = _text(arg, src)
        # ++/-- 可能在前缀位置也可能在后缀位置，不能只看 children[0]
        ops = [c.type for c in upd.children]
        if "--" in ops:
            loop.step = -1
        elif "++" in ops:
            loop.step = 1
    elif upd is not None and upd.type == "assignment_expression":
        # for 的更新位也可能是 i += 2 / i -= 2（assignment_expression）
        left = upd.child_by_field_name("left")
        right = upd.child_by_field_name("right")
        op = upd.children[1].type if len(upd.children) > 1 else ""
        if left is not None and op in ("+=", "-=") and loop.var is None:
            loop.var = _text(left, src)
        if right is not None and op in ("+=", "-="):
            num = _int_or_none(_text(right, src))
            if num:
                loop.step = num if op == "+=" else -num

    return loop


def _parse_while(node: Node, src: bytes) -> Loop:
    _cond_node, cond_text = _condition_of(node, src)
    var = None
    m = re.search(r"([A-Za-z_]\w*)\s*(--|\+\+)", cond_text)
    if m:
        var = m.group(1)
    else:
        ids = [w for w in _WORD_RE.findall(cond_text)]
        if len(ids) == 1:
            var = ids[0]
    return Loop(node.start_byte, node.end_byte, "while", var=var,
                step=-1 if "--" in cond_text else 1, cond=cond_text)


def _collect_loops(root: Node, src: bytes) -> list[Loop]:
    loops: list[Loop] = []
    for node in _walk(root):
        if node.type == "for_statement":
            loops.append(_parse_for(node, src))
        elif node.type == "while_statement":
            loops.append(_parse_while(node, src))
    loops.sort(key=lambda l: (l.start, -(l.end - l.start)))
    return loops


# --------------------------------------------------------------------------
# 多组测试循环识别
# --------------------------------------------------------------------------

def _detect_repeat(reads: list[Read], loops: list[Loop],
                   decls: dict[str, Decl]) -> RepeatInfo | None:
    for loop in loops:
        if not loop.cond:
            continue
        inner = [r for r in reads if loop.start <= r.byte <= loop.end]
        if not inner:
            continue
        if loop.var and any(
            re.search(rf"\b{re.escape(loop.var)}\b", ix)
            for r in inner for ix in r.indexes
        ):
            continue                                # 循环变量被当作数组下标 → 是遍历循环
        candidates = []
        for word in _WORD_RE.findall(loop.cond):
            if word in ("true", "false", "NULL", "nullptr"):
                continue
            if not any(r.name == word and r.byte < loop.start for r in reads):
                continue
            decl = decls.get(word)
            if decl is not None and decl.is_array:
                continue
            if word in candidates:
                continue
            candidates.append(word)
        if candidates:
            return RepeatInfo(candidates[0], loop.start, loop.end, 0)
    return None


# --------------------------------------------------------------------------
# 读入 → 变量生成规格
# --------------------------------------------------------------------------

def _index_level(read: Read, index: str, loops: list[Loop]) -> dict | None:
    index = index.strip()
    if index.isdigit():
        return {"lo": index, "hi": index, "inclusive": True, "step": 1}
    best: Loop | None = None
    for loop in loops:
        if loop.kind != "for" or loop.var != index or not loop.has_bounds:
            continue
        if not (loop.start <= read.byte <= loop.end):
            continue
        if best is None or loop.start > best.start:
            best = loop
    if best is None:
        return None
    return {
        "lo": best.lo_expr,
        "hi": best.hi_expr,
        "inclusive": best.inclusive,
        "step": best.step or 1,
    }


def _is_string_read(read: Read, decl: Decl | None) -> bool:
    if read.op == "getline":
        return True
    base = decl.base if decl else ""
    if base in _STRING_BASES:
        return True
    if base in _CHAR_BASES and decl is not None and decl.dims:
        return read.spec in ("s", "") and read.op in ("cin", "scanf", "getline")
    return False


def resolve_vars(scan: Scan) -> tuple[list[VarSpec], list[str]]:
    """按读入顺序返回唯一的输入变量规格。"""
    specs: list[VarSpec] = []
    seen: set[str] = set()
    warnings: list[str] = []

    for read in scan.reads:
        if read.name in seen:
            continue
        seen.add(read.name)
        decl = scan.decls.get(read.name)

        if _is_string_read(read, decl):
            specs.append(VarSpec(read.name, "string",
                                 decl.base if decl else "string",
                                 line=read.line, op=read.op))
            continue

        if read.indexes:
            levels = []
            missing = []
            for ix in read.indexes:
                level = _index_level(read, ix, scan.loops)
                if level is None:
                    missing.append(ix)
                else:
                    levels.append(level)
            if missing:
                warnings.append(
                    f"变量 {read.name} 的下标 {', '.join(missing)} 找不到对应的循环边界，"
                    f"请在配置里手动填写它的元素个数 (count)"
                )
                specs.append(VarSpec(read.name, "array",
                                     decl.base if decl else "int",
                                     line=read.line, op=read.op))
            else:
                specs.append(VarSpec(read.name, "array",
                                     decl.base if decl else "int",
                                     levels=levels, line=read.line, op=read.op))
            continue

        if decl is not None and decl.is_array:
            if decl.base in _VECTOR_BASES and decl.dims:
                levels = [{"lo": "0", "hi": dim, "inclusive": False, "step": 1}
                          for dim in decl.dims]
                specs.append(VarSpec(read.name, "array", decl.base,
                                     levels=levels, line=read.line, op=read.op))
            else:
                warnings.append(
                    f"变量 {read.name} 是数组但没找到读它的循环，"
                    f"请在配置里手动填写它的元素个数 (count)"
                )
                specs.append(VarSpec(read.name, "array",
                                     decl.base if decl else "int",
                                     line=read.line, op=read.op))
            continue

        specs.append(VarSpec(read.name, "scalar",
                             decl.base if decl else "int",
                             line=read.line, op=read.op))

    return specs, warnings


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def scan(source: str) -> Scan:
    src = source.encode("utf-8")
    tree = Parser(_LANGUAGE).parse(src)
    root = tree.root_node

    decls = _collect_decls(root, src)
    reads = _collect_reads(root, src)
    loops = _collect_loops(root, src)
    repeat = _detect_repeat(reads, loops, decls)
    warnings: list[str] = []
    if root.has_error:
        warnings.append("源码解析时出现语法错误，识别结果可能不完整")
    if not reads:
        warnings.append("没有识别到任何输入语句 (cin/scanf/getline)")

    if repeat is not None:
        repeat.line = _line_of(source, repeat.start)
        for read in reads:
            if repeat.start <= read.byte <= repeat.end:
                read.repeat = True

    return Scan(source, reads, decls, loops, repeat, warnings)


def describe(scan_obj: Scan) -> tuple[list[VarSpec], list[str]]:
    return resolve_vars(scan_obj)
