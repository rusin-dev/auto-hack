"""生成器回归测试：N 维数组折行、预算上限、多组测试。"""

import random
import unittest

from autohack.asts import VarSpec
from autohack.config import Config
from autohack.generator import (MAX_ELEMENTS, _array_lines, generate_with,
                                plan_text)

FULL_BUDGET = {"left": MAX_ELEMENTS}


def _levels(*counts):
    """构造 [[0..c) ...] 形式的各维下标范围。"""
    return [{"lo": "0", "hi": str(c), "inclusive": False, "step": 1}
            for c in counts]


class ArrayLinesTest(unittest.TestCase):
    def test_1d_array(self):
        spec = VarSpec("a", "array", "int", levels=_levels(7))
        rows = _array_lines(spec, {"a": {"min": 1, "max": 9}},
                            random.Random(1), {}, dict(FULL_BUDGET))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].split()), 7)

    def test_2d_array(self):
        spec = VarSpec("a", "array", "int", levels=_levels(3, 4))
        rows = _array_lines(spec, {"a": {"min": 1, "max": 9}},
                            random.Random(1), {}, dict(FULL_BUDGET))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(row.split()) == 4 for row in rows))

    def test_3d_array_not_truncated(self):
        """回归：三维及以上曾静默只取前两维，生成的元素数量错误。"""
        spec = VarSpec("a", "array", "int", levels=_levels(2, 3, 4))
        rows = _array_lines(spec, {"a": {"min": 1, "max": 9}},
                            random.Random(1), {}, dict(FULL_BUDGET))
        tokens = [token for row in rows for token in row.split()]
        self.assertEqual(len(tokens), 2 * 3 * 4)
        self.assertEqual(len(rows), 6)          # 前两维折叠，最后一维折行
        self.assertTrue(all(len(row.split()) == 4 for row in rows))

    def test_count_override_expression(self):
        spec = VarSpec("a", "array", "int")
        cfg_vars = {"a": {"min": 1, "max": 9, "count": "n * 2"}}
        rows = _array_lines(spec, cfg_vars, random.Random(1), {"n": 3},
                            dict(FULL_BUDGET))
        self.assertEqual(len(rows[0].split()), 6)

    def test_budget_limit(self):
        spec = VarSpec("a", "array", "int", levels=_levels(MAX_ELEMENTS + 1))
        with self.assertRaises(ValueError):
            _array_lines(spec, {"a": {"min": 1, "max": 9}},
                         random.Random(1), {}, dict(FULL_BUDGET))

    def test_zero_size(self):
        spec = VarSpec("a", "array", "int", levels=_levels(0))
        rows = _array_lines(spec, {}, random.Random(1), {}, dict(FULL_BUDGET))
        self.assertEqual(rows, [])


class GenerateWithTest(unittest.TestCase):
    def test_repeat_block(self):
        plan = [("line", ["t"]),
                ("repeat", "t", [("line", ["n"])])]
        cfg = Config(seed=1,
                     vars={"t": {"min": 2, "max": 2},
                           "n": {"min": 3, "max": 3}})
        data = generate_with(plan, cfg, random.Random(1))
        self.assertEqual(data, b"2\n3\n3\n")

    def test_env_feeds_count_expressions(self):
        plan = [("line", ["n"]), ("array", VarSpec("a", "array", "int"))]
        cfg = Config(seed=1,
                     vars={"n": {"min": 4, "max": 4},
                           "a": {"min": 1, "max": 9, "count": "n"}})
        data = generate_with(plan, cfg, random.Random(1))
        lines = data.decode().splitlines()
        self.assertEqual(lines[0], "4")
        self.assertEqual(len(lines[1].split()), 4)

    def test_plan_text_runs(self):
        plan = [("line", ["t"]),
                ("repeat", "t", [("array",
                                  VarSpec("a", "array", "int", levels=_levels(3)))])]
        text = plan_text(plan, 1)
        self.assertIn("重复 t 次", text)
        self.assertIn("数组: a", text)


if __name__ == "__main__":
    unittest.main()
