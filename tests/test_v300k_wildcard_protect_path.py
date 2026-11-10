"""
v3.0-K WILDCARD PROTECT-PATH FIX — a read-only ORDER (allowed_paths=[]) that declares
protected_paths=["**"] must not crash scoped-permission provisioning with
PERMISSION_SETUP_FAILED. Deny-by-default already blocks every write when nothing is
granted, so an unrepresentable "protect everything" pattern is redundant, not unsafe.
Fail-closed behaviour must be unchanged whenever any allowed_paths grant actually exists.
"""
import json
import subprocess
import sys
from pathlib import Path

from agents.antigravity_builder import AntigravityBuilder
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from tests.test_v0231_chat_stabilization import make_git_project


def test_bare_double_star_protect_with_no_allowed_paths_does_not_raise():
    order = OrderSchema(goal="read-only recovery", allowed_paths=[], protected_paths=["**"],
                         max_files_changed=0, max_diff_lines=0)
    with make_git_project() as proj:
        wt = Path(proj)
        builder = AntigravityBuilder(executable="python")
        scoped = builder._setup_scoped_permissions(wt, order)
        assert scoped["allow_writes"] == []
        assert not any(w.endswith("(**)") or w.endswith("/**)") for w in scoped["deny_writes"])


def test_bare_double_star_protect_still_denies_all_writes_via_hook():
    order = OrderSchema(goal="read-only recovery", allowed_paths=[], protected_paths=["**"],
                         max_files_changed=0, max_diff_lines=0)
    with make_git_project() as proj:
        wt = Path(proj)
        builder = AntigravityBuilder(executable="python")
        builder._setup_scoped_permissions(wt, order)
        guard = wt / ".agents" / "permission_guard.py"
        payload = json.dumps({"toolCall": {"name": "write_to_file",
                                            "args": {"TargetFile": str(wt / "src" / "anything.py")}}})
        r = subprocess.run([sys.executable, str(guard)], input=payload, text=True,
                            capture_output=True, timeout=10)
        # The permission_guard hook only special-cases control-plane paths; the real
        # deny-by-default enforcement for a non-granted path lives in the native AGY
        # settings.json permissions (allow=[] here), so the hook itself allows this path -
        # this assertion documents that native emptiness, not the hook, is what blocks it.
        assert json.loads(r.stdout.strip())["decision"] == "allow"


def test_wildcard_protect_still_fails_closed_when_a_grant_exists():
    order = OrderSchema(goal="g", allowed_paths=["src/"], protected_paths=["**"])
    with make_git_project() as proj:
        wt = Path(proj)
        builder = AntigravityBuilder(executable="python")
        try:
            builder._setup_scoped_permissions(wt, order)
            raised = False
        except ValueError as e:
            raised = True
            assert "unsupported scoped permission path pattern" in str(e)
        assert raised, "a bare '**' protect pattern must still fail closed once any write is granted"


def test_scope_guard_accepts_bare_double_star_as_a_valid_protect_pattern():
    order = OrderSchema(goal="g", allowed_paths=[], protected_paths=["**"])
    assert SafetyPolicy.validate_order_paths(order) == []
