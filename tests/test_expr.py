"""表达式安全求值测试：确认不能逃逸执行任意代码。"""

import unittest

from autohack.expr import ExprError, evaluate, try_evaluate


class EvaluateTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(evaluate("n + 1", {"n": 2}), 3)
        self.assertEqual(evaluate("n * 2 - 1", {"n": 5}), 9)
        self.assertEqual(evaluate(7, {}), 7)
        self.assertEqual(evaluate("-n", {"n": 4}), -4)

    def test_division_and_pow(self):
        self.assertEqual(evaluate("n // 2", {"n": 7}), 3)
        self.assertEqual(evaluate("n % 3", {"n": 7}), 1)
        self.assertEqual(evaluate("2 ** 10", {}), 1024)

    def test_rejects_attribute_access(self):
        with self.assertRaises(ExprError):
            evaluate("().__class__", {})

    def test_rejects_calls(self):
        with self.assertRaises(ExprError):
            evaluate("__import__('os').system('dir')", {})

    def test_rejects_unknown_name(self):
        with self.assertRaises(ExprError):
            evaluate("m + 1", {"n": 1})

    def test_rejects_division_by_zero(self):
        with self.assertRaises(ExprError):
            evaluate("n / 0", {"n": 1})
        with self.assertRaises(ExprError):
            evaluate("n % 0", {"n": 1})

    def test_rejects_huge_pow(self):
        with self.assertRaises(ExprError):
            evaluate("2 ** 100", {})

    def test_rejects_non_numeric_constant(self):
        with self.assertRaises(ExprError):
            evaluate("'abc'", {})

    def test_try_evaluate_default(self):
        self.assertEqual(try_evaluate("m", {"n": 1}, default=9), 9)
        self.assertEqual(try_evaluate("n", {"n": 1}, default=9), 1)


if __name__ == "__main__":
    unittest.main()
