"""生成器回归测试：N 维数组折行、预算上限、多组测试、边界值策略。"""

import random
import unittest

from autohack.asts import VarSpec
from autohack.config import Config
from autohack.generator import (
    MAX_ELEMENTS,
    STRATEGIES,
    _array_lines,
    _edge_pool,
    _pick_int,
    generate_with,
    plan_text,
    strategy_of,
)

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


class StrategyTest(unittest.TestCase):
    """边界值策略：hack 点绝大多数住在端点、溢出值和退化结构上。"""

    def test_strategy_of_normalizes_dirty_values(self):
        self.assertEqual(strategy_of("MIXED "), "mixed")
        self.assertEqual(strategy_of("edge"), "edge")
        self.assertEqual(strategy_of("random"), "random")
        for dirty in ("", None, "边界", "edges", 12):
            self.assertEqual(strategy_of(dirty), "mixed")
        self.assertEqual(len(STRATEGIES), 3)

    def test_edge_pool_stays_inside_range_and_dedups(self):
        self.assertEqual(_edge_pool(5, 7), [5, 6, 7])
        pool = _edge_pool(1, 10 ** 9)
        self.assertEqual(pool[0], 1)
        self.assertEqual(pool[-1], 10 ** 9)
        self.assertEqual(len(pool), len(set(pool)))
        self.assertTrue(all(1 <= v <= 10 ** 9 for v in pool))
        self.assertIn(1, pool)                    # 下界
        self.assertIn(999999999, pool)            # 上界 - 1
        self.assertIn(2, pool)                    # 区间内的经典边界
        self.assertNotIn(-1, pool)                # 区间外的经典边界不进来
        self.assertNotIn(2 ** 31 - 1, pool)       # 超出 1e9 的溢出边界不进来

    def test_edge_pool_reaches_long_long_limits(self):
        pool = _edge_pool(-(2 ** 63), 2 ** 63 - 1)
        for value in (0, 1, -1, -(2 ** 31), 2 ** 31 - 1, -(2 ** 63), 2 ** 63 - 1):
            self.assertIn(value, pool)

    def test_pick_int_respects_strategy(self):
        pool = set(_edge_pool(1, 500))
        edge = [_pick_int(random.Random(seed), 1, 500, "edge")
                for seed in range(50)]
        self.assertTrue(set(edge) <= pool)        # edge 只从边界池取
        self.assertGreater(len(set(edge)), 1)     # 但不会只取到一个值

        random_draws = [_pick_int(random.Random(seed), 1, 10 ** 9, "random")
                        for seed in range(300)]
        boundaries = [v for v in random_draws if v in _edge_pool(1, 10 ** 9)]
        self.assertLess(len(boundaries), len(random_draws) // 2)

        mixed = [_pick_int(random.Random(seed), 1, 10 ** 9, "mixed")
                 for seed in range(300)]
        hits = [v for v in mixed if v in _edge_pool(1, 10 ** 9)]
        self.assertTrue(hits, "mixed 应该命中若干边界值")
        self.assertTrue(len(mixed) - len(hits), "mixed 仍应保留大量随机值")

    def test_pick_int_collapsed_range(self):
        for strategy in STRATEGIES:
            self.assertEqual(_pick_int(random.Random(1), 7, 7, strategy), 7)

    def test_array_uses_edges_and_flat_blocks(self):
        spec = VarSpec("a", "array", "int", levels=_levels(4))
        pool = set(_edge_pool(-5, 5))
        edge_rows = [_array_lines(spec, {"a": {"min": -5, "max": 5}},
                                  random.Random(seed), {}, dict(FULL_BUDGET), "edge")
                     for seed in range(30)]
        for row in edge_rows:
            for token in row[0].split():
                self.assertIn(int(token), pool)
        # 全相等块：同一行 4 个值相同
        flat = [row for row in edge_rows if len(set(row[0].split())) == 1]
        self.assertTrue(flat, "edge 策略应产出整块全相等的数据")

        flat_random = [_array_lines(spec, {"a": {"min": -5, "max": 5}},
                                    random.Random(seed), {}, dict(FULL_BUDGET), "random")
                       for seed in range(30)]
        self.assertFalse([row for row in flat_random
                          if len(set(row[0].split())) == 1])

    def test_config_strategy_drives_generation(self):
        plan = [("line", ["n"]), ("array", VarSpec("a", "array", "int"))]
        cfg = Config(strategy="edge",
                     vars={"n": {"min": 1, "max": 4},
                           "a": {"min": 1, "max": 10 ** 9, "count": "n"}})
        first = generate_with(plan, cfg, random.Random(3))
        self.assertEqual(first, generate_with(plan, cfg, random.Random(3)))
        tokens = first.decode().split()[1:]
        self.assertTrue(set(map(int, tokens)) <= set(_edge_pool(1, 10 ** 9)))

        dirty = Config(strategy="随便写的", vars={"n": {"min": 2, "max": 2}})
        self.assertEqual(generate_with([("line", ["n"])], dirty,
                                       random.Random(1)), b"2\n")


if __name__ == "__main__":
    unittest.main()
