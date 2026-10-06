"""AST 解析的回归测试：重点覆盖倒序循环与步长解析的修复。"""

import unittest

from autohack.asts import resolve_vars, scan
from autohack.generator import _level_count


def _norm(text):
    return text.replace(" ", "") if isinstance(text, str) else text


SRC_REVERSE = """
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n);
    for (int i = n - 1; i >= 0; i--) cin >> a[i];
    return 0;
}
"""

SRC_REVERSE_LITERAL_START = """
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n);
    for (int i = n; i >= 0; i--) cout << a[i];
    return 0;
}
"""

SRC_NO_INIT = """
#include <vector>
using namespace std;
int main() {
    int n, i;
    cin >> n;
    for (; i >= 0; i--) cout << i;
    return 0;
}
"""

SRC_STEP = """
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n + 1);
    for (int i = 0; i <= n; i += 2) cin >> a[i];
    return 0;
}
"""

SRC_MIRRORED_REVERSE = """
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n + 1);
    for (int i = n; 0 <= i; i--) cin >> a[i];
    return 0;
}
"""

SRC_MIRRORED_ASCENDING = """
#include <vector>
using namespace std;
int main() {
    int n;
    cin >> n;
    vector<int> a(n);
    for (int i = 0; n > i; i++) cin >> a[i];
    return 0;
}
"""

SRC_REPEAT = """
#include <iostream>
using namespace std;
int main() {
    int t;
    cin >> t;
    while (t--) {
        int n;
        cin >> n;
        cout << n << "\\n";
    }
    return 0;
}
"""

SRC_SCANF = """
int main() {
    int n;
    scanf("%d", &n);
    int a[100];
    for (int i = 0; i < n; i++) scanf("%d", &a[i]);
    return 0;
}
"""


class ReverseLoopTest(unittest.TestCase):
    def test_expression_start_no_attribute_error(self):
        """回归：`i = n-1` 这类非字面量起点曾抛 AttributeError（Loop.lo 未声明）。"""
        obj = scan(SRC_REVERSE)
        loops = [loop for loop in obj.loops if loop.kind == "for"]
        self.assertEqual(len(loops), 1)
        loop = loops[0]
        self.assertEqual(_norm(loop.hi_expr), "n-1")
        self.assertEqual(loop.lo_expr, "0")
        self.assertTrue(loop.inclusive)
        self.assertEqual(loop.step, -1)

    def test_reverse_loop_array_levels(self):
        obj = scan(SRC_REVERSE)
        specs, warnings = resolve_vars(obj)
        by_name = {spec.name: spec for spec in specs}
        self.assertIn("a", by_name)
        spec = by_name["a"]
        self.assertEqual(spec.kind, "array")
        self.assertEqual(len(spec.levels), 1)
        level = spec.levels[0]
        self.assertEqual(level["lo"], "0")
        self.assertEqual(_norm(level["hi"]), "n-1")
        self.assertEqual(level["step"], -1)
        self.assertEqual(warnings, [])
        # 倒序循环的元素个数曾恒为 0
        self.assertEqual(_level_count(level, {"n": 5}), 5)

    def test_literal_start_keeps_upper_bound(self):
        obj = scan(SRC_REVERSE_LITERAL_START)
        loops = [loop for loop in obj.loops if loop.kind == "for"]
        self.assertEqual(loops[0].hi_expr, "n")
        self.assertEqual(loops[0].lo_expr, "0")

    def test_missing_initializer_does_not_crash(self):
        obj = scan(SRC_NO_INIT)
        self.assertTrue(obj.loops)


class MirroredConditionTest(unittest.TestCase):
    """循环变量写在比较式右边（0 <= i）时方向曾被弄反，数组长度算成 0。"""

    def test_mirrored_descending(self):
        obj = scan(SRC_MIRRORED_REVERSE)
        loop = [loop for loop in obj.loops if loop.kind == "for"][0]
        self.assertEqual(loop.hi_expr, "n")
        self.assertEqual(loop.lo_expr, "0")
        self.assertTrue(loop.inclusive)
        self.assertEqual(loop.step, -1)
        specs, warnings = resolve_vars(obj)
        spec = [s for s in specs if s.name == "a"][0]
        self.assertEqual(_level_count(spec.levels[0], {"n": 4}), 5)

    def test_mirrored_ascending(self):
        obj = scan(SRC_MIRRORED_ASCENDING)
        loop = [loop for loop in obj.loops if loop.kind == "for"][0]
        self.assertEqual(loop.hi_expr, "n")
        self.assertFalse(loop.inclusive)
        self.assertEqual(loop.step, 1)
        specs, warnings = resolve_vars(obj)
        spec = [s for s in specs if s.name == "a"][0]
        self.assertEqual(_level_count(spec.levels[0], {"n": 4}), 4)


class StepTest(unittest.TestCase):
    def test_compound_assignment_step(self):
        """回归：`i += 2` 是 assignment_expression，曾被当成 update_expression 漏掉。"""
        obj = scan(SRC_STEP)
        loop = [loop for loop in obj.loops if loop.kind == "for"][0]
        self.assertEqual(loop.step, 2)
        self.assertEqual(loop.var, "i")

    def test_compound_assignment_level_count(self):
        obj = scan(SRC_STEP)
        specs, _ = resolve_vars(obj)
        spec = [s for s in specs if s.name == "a"][0]
        level = spec.levels[0]
        self.assertEqual(level["step"], 2)
        # i = 0, 2, 4（n = 5，i <= 5）
        self.assertEqual(_level_count(level, {"n": 5}), 3)

    def test_postfix_increment_step(self):
        src = """
int main() {
    int n;
    cin >> n;
    int a[100];
    for (int i = 0; i < n; i++) cin >> a[i];
    return 0;
}
"""
        loop = [loop for loop in scan(src).loops if loop.kind == "for"][0]
        self.assertEqual(loop.step, 1)


class LevelCountTest(unittest.TestCase):
    def test_ascending_exclusive(self):
        level = {"lo": "0", "hi": "n", "inclusive": False, "step": 1}
        self.assertEqual(_level_count(level, {"n": 5}), 5)

    def test_ascending_inclusive(self):
        level = {"lo": "1", "hi": "n", "inclusive": True, "step": 1}
        self.assertEqual(_level_count(level, {"n": 5}), 5)

    def test_descending_inclusive(self):
        level = {"lo": "0", "hi": "n", "inclusive": True, "step": -1}
        self.assertEqual(_level_count(level, {"n": 5}), 6)

    def test_descending_exclusive(self):
        level = {"lo": "0", "hi": "n", "inclusive": False, "step": -1}
        self.assertEqual(_level_count(level, {"n": 5}), 5)


class RepeatAndScanfTest(unittest.TestCase):
    def test_repeat_detection_still_works(self):
        obj = scan(SRC_REPEAT)
        self.assertIsNotNone(obj.repeat)
        self.assertEqual(obj.repeat.var, "t")
        inner = [r for r in obj.reads if r.repeat]
        self.assertEqual([r.name for r in inner], ["n"])

    def test_scanf_reads(self):
        obj = scan(SRC_SCANF)
        specs, warnings = resolve_vars(obj)
        by_name = {spec.name: spec for spec in specs}
        self.assertEqual(by_name["n"].kind, "scalar")
        self.assertEqual(by_name["a"].kind, "array")
        self.assertEqual(by_name["a"].levels[0]["hi"], "n")
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
