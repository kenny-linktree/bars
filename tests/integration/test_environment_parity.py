"""The child-environment allowlist is written out in four places; they must agree."""
import importlib.util
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def swift_allowlist():
    """The string literals of `allowed` in BarsModel.makeRefreshProcess."""
    source = (ROOT / "macOS/App/BarsModel.swift").read_text()
    match = re.search(r"let allowed: Set<String> = \[(.*?)\]", source, re.S)
    if match is None:
        raise AssertionError("BarsModel.swift no longer declares `let allowed: Set<String> = [...]`.")
    body = re.sub(r"//[^\n]*", "", match.group(1))
    names = re.findall(r'"([^"\\]*)"', body)
    # Nothing but comma-separated string literals may appear in the literal.
    if re.sub(r'"[^"\\]*"|[\s,]', "", body):
        raise AssertionError("The Swift allowlist contains something other than string literals.")
    return names


class ChildEnvironmentParityTests(unittest.TestCase):
    def test_all_allowlists_name_the_same_variables(self):
        sys.path.insert(0, str(ROOT / "collector"))
        try:
            from bars_collector import adapters
        finally:
            sys.path.remove(str(ROOT / "collector"))
        lists = {
            "collector/bars_collector/adapters.py": list(adapters.CHILD_ENVIRONMENT),
            "integration/refresh.py": list(module("parity_refresh", ROOT / "integration/refresh.py").CHILD_ENVIRONMENT),
            "scripts/install.py": list(module("parity_installer", ROOT / "scripts/install.py").CHILD_ENVIRONMENT),
            "macOS/App/BarsModel.swift": swift_allowlist(),
        }
        expected = set(lists["collector/bars_collector/adapters.py"])
        self.assertIn("HOME", expected)
        for path, names in lists.items():
            with self.subTest(path=path):
                self.assertEqual(len(names), len(set(names)), "duplicate names")
                self.assertEqual(set(names), expected)


if __name__ == "__main__":
    unittest.main()
