"""v3.1.4 — caches and bytecode are never part of a delivery (0단계 수정, 2026-11-01).

MSN-261029080201 passed 2/2 steps but the apply was rolled back because src/__pycache__/*.pyc (written while the
verifier ran the tests) was in the manifest and its hash changed. Generated files are now excluded up front.
"""
from core.result_manager import ResultManager


def test_generated_paths_are_recognised():
    gen = ResultManager.is_generated_path
    assert gen("src/__pycache__/expense_summary.cpython-39.pyc")
    assert gen("tests/.pytest_cache/v/cache/nodeids")
    assert gen("node_modules/left-pad/index.js")
    assert gen("app/.DS_Store")
    assert gen("build/output.log")
    assert gen("pkg\\__pycache__\\x.pyc")


def test_real_sources_are_not_generated():
    gen = ResultManager.is_generated_path
    assert not gen("src/expense_summary.py")
    assert not gen("tests/test_expense.py")
    assert not gen("cache_policy.py")           # a file *named* cache is still source
    assert not gen("docs/README.md")
    assert not gen("data/fixture.json")
