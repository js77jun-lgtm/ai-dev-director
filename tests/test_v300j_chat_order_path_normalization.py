"""AUTO-RESOLVE FIRST: a chat-generated ORDER's path *format* mistakes (drive-absolute instead of
repo-relative; protecting a sibling outside the project) are normalized once, before Scope Guard ever
evaluates the order. validate_order_paths()/assess_order_for_auto_run() themselves are never touched -
a genuine attempt to read/write outside the project must still fail exactly as before.
"""
from pathlib import Path

from core.safety import SafetyPolicy
from schemas.order import OrderSchema


def _order(**kwargs):
    base = dict(
        goal="test", allowed_paths=[], protected_paths=[], allowed_deletions=[],
        acceptance_criteria=["ok"], max_files_changed=5, max_diff_lines=200,
        context="test", risk_level="LOW", requires_human_approval=False,
    )
    base.update(kwargs)
    return OrderSchema(**base)


# TEST A -------------------------------------------------------------------------------------------

def test_a_drive_absolute_allowed_path_is_normalized_and_never_escalates(tmp_path):
    project = tmp_path / "the trading sample project"
    project.mkdir()
    abs_src = str(project / "src")
    abs_tests = str(project / "tests")
    order = _order(allowed_paths=[abs_src, abs_tests], acceptance_criteria=["a test passes"])

    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.allowed_paths == ["src", "tests"]

    # The real, unmodified check now sees a clean order - no INVALID_PATH reason at all.
    reasons = SafetyPolicy.assess_order_for_auto_run(fixed)
    assert not any("INVALID_PATH" in r for r in reasons)


def test_a_unfixed_order_would_have_escalated(tmp_path):
    """Sanity check: without normalization, the same order genuinely does hit INVALID_PATH - proving
    test A exercises a real fix, not a no-op."""
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(allowed_paths=[str(project / "src")], acceptance_criteria=["a test passes"])
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    assert any("INVALID_PATH" in r and "drive-absolute" in r for r in reasons)


def test_protected_path_outside_project_is_dropped_not_escalated(tmp_path):
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(
        allowed_paths=["src"],
        protected_paths=["../AITOS_Regime_Ensemble/**", str(tmp_path / "AITOS_Regime_Ensemble")],
        acceptance_criteria=["a test passes"],
    )
    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.protected_paths == []
    reasons = SafetyPolicy.assess_order_for_auto_run(fixed)
    assert not any("INVALID_PATH" in r for r in reasons)


def test_protected_path_inside_project_is_rewritten_relative_not_dropped(tmp_path):
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(
        allowed_paths=["src"],
        protected_paths=[str(project / ".git"), str(project / ".venv")],
        acceptance_criteria=["a test passes"],
    )
    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert set(fixed.protected_paths) == {".git", ".venv"}


def test_allowed_path_outside_project_is_left_untouched_and_still_rejected(tmp_path):
    """The one case normalization must NEVER auto-fix: an allowed_path (write grant) that genuinely
    resolves outside the project. It must be passed through unchanged and still fail the real check -
    proving this fix cannot be used to smuggle in a real scope violation."""
    project = tmp_path / "the trading sample project"
    project.mkdir()
    outside = str(tmp_path / "AITOS_Regime_Ensemble")
    order = _order(allowed_paths=[outside], acceptance_criteria=["a test passes"])

    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.allowed_paths == [outside]  # untouched

    reasons = SafetyPolicy.assess_order_for_auto_run(fixed)
    assert any("INVALID_PATH" in r for r in reasons)  # still fails closed


def test_allowed_path_with_parent_traversal_is_left_untouched_and_still_rejected(tmp_path):
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(allowed_paths=["../AITOS_Regime_Ensemble/exploit.py"], acceptance_criteria=["a test passes"])

    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.allowed_paths == ["../AITOS_Regime_Ensemble/exploit.py"]

    reasons = SafetyPolicy.assess_order_for_auto_run(fixed)
    assert any("INVALID_PATH" in r and "parent traversal" in r for r in reasons)


def test_already_relative_paths_pass_through_unchanged(tmp_path):
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(allowed_paths=["src/", "tests/"], protected_paths=[".git"],
                    acceptance_criteria=["a test passes"])
    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.allowed_paths == ["src/", "tests/"]
    assert fixed.protected_paths == [".git"]


def test_normalization_never_touches_sensitive_file_escalation(tmp_path):
    """Normalization fixes path FORMAT only - it must never suppress a legitimate, separate
    SENSITIVE_FILE_IN_SCOPE escalation for a real dependency-file change."""
    project = tmp_path / "the trading sample project"
    project.mkdir()
    order = _order(allowed_paths=[str(project / "requirements.txt")], acceptance_criteria=["a test passes"])
    fixed = SafetyPolicy.normalize_order_paths(order, str(project))
    assert fixed.allowed_paths == ["requirements.txt"]  # format fixed
    reasons = SafetyPolicy.assess_order_for_auto_run(fixed)
    assert any("SENSITIVE_FILE_IN_SCOPE" in r for r in reasons)  # but still escalates, correctly


def test_normalization_is_a_noop_when_project_path_cannot_resolve():
    order = _order(allowed_paths=["src"], acceptance_criteria=["ok"])
    fixed = SafetyPolicy.normalize_order_paths(order, "")
    assert fixed.allowed_paths == ["src"]
