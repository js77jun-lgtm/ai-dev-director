"""
v3.0.8 BUILDER PROMPT SIZE: a 310K-char Builder prompt (every test file writable) produced empty responses and 600 s
timeouts. Writable files are now ranked (required outputs, files the ORDER names, then smallest first) under a smaller
budget, overflow is listed by path only, and the Builder may answer with exact search/replace edits instead of
rewriting whole files. An edit whose anchor is missing or ambiguous rejects the whole batch before anything is written.
"""
import json
import sys

import pytest

from agents.proposal_builder import ProposalBuilder
from core.research_runner import TriadResearchRunner
from schemas.order import OrderSchema


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def reply(monkeypatch, proposal, seen=None):
    def invoke(self, domain, prompt, cwd, stage):
        if seen is not None:
            seen["prompt"] = prompt
        return dict(output=json.dumps(proposal), invocation="t", exit_code=0, duration_sec=0)
    monkeypatch.setattr(TriadResearchRunner, "invoke", invoke)


def test_exact_edits_change_only_the_anchored_text(tmp_path, monkeypatch):
    write(tmp_path, "backend/vision.py", "def parse(text):\n    return json.loads(text)\n\nTIMEOUT = 120\n")
    reply(monkeypatch, {"edits": [
        {"path": "backend/vision.py", "old": "    return json.loads(text)\n", "new": "    return json.loads(strip_fence(text))\n"},
        {"path": "backend/vision.py", "old": "TIMEOUT = 120", "new": "TIMEOUT = 45"}],
        "files": [{"path": "backend/fence.py", "content": "def strip_fence(t):\n    return t\n"}]})
    res = ProposalBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["backend/**"]), str(tmp_path))
    assert sorted(res.files_modified) == ["backend/fence.py", "backend/vision.py"]
    assert (tmp_path / "backend/vision.py").read_text(encoding="utf-8") == \
        "def parse(text):\n    return json.loads(strip_fence(text))\n\nTIMEOUT = 45\n"


@pytest.mark.parametrize("old, why", [("TIMEOUT = 999", "found 0"), ("x = 1", "found 2")])
def test_missing_or_ambiguous_anchor_rejects_the_whole_batch(tmp_path, monkeypatch, old, why):
    write(tmp_path, "src/a.py", "x = 1\nx = 1\n")
    write(tmp_path, "src/b.py", "B = 0\n")
    reply(monkeypatch, {"edits": [{"path": "src/b.py", "old": "B = 0", "new": "B = 1"},
                                  {"path": "src/a.py", "old": old, "new": "y"}]})
    with pytest.raises(ValueError, match=why):
        ProposalBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/**"]), str(tmp_path))
    assert (tmp_path / "src/b.py").read_text(encoding="utf-8") == "B = 0\n"          # nothing was written


def test_edits_obey_the_scope_guard_and_need_an_existing_file(tmp_path, monkeypatch):
    write(tmp_path, "secret/keys.py", "K = 1\n")
    reply(monkeypatch, {"edits": [{"path": "secret/keys.py", "old": "K = 1", "new": "K = 2"}]})
    builder = ProposalBuilder(executable=sys.executable)
    with pytest.raises(ValueError, match="denied"):
        builder.run(OrderSchema(goal="g", allowed_paths=["src/**"]), str(tmp_path))
    reply(monkeypatch, {"edits": [{"path": "src/new.py", "old": "a", "new": "b"}]})
    with pytest.raises(ValueError, match="does not exist"):
        builder.run(OrderSchema(goal="g", allowed_paths=["src/**"]), str(tmp_path))


def test_large_writable_trees_are_ranked_and_overflow_is_listed_by_path(tmp_path, monkeypatch):
    for i in range(6):                                                    # 6 x 40 KB of writable tests
        write(tmp_path, f"backend/tests/test_big_{i}.py", "# " + "t" * 40_000 + "\n")
    write(tmp_path, "backend/tests/test_vision_lmstudio.py", "# NAMED-TEST-MARKER " + "n" * 50_000 + "\n")
    write(tmp_path, "backend/vision_lmstudio.py", "# REQUIRED-MARKER\n")
    seen = {}
    reply(monkeypatch, {"edits": [{"path": "backend/vision_lmstudio.py", "old": "# REQUIRED-MARKER", "new": "# done"}]}, seen)
    builder = ProposalBuilder(executable=sys.executable)
    order = OrderSchema(goal="Parse fenced JSON; extend backend/tests/test_vision_lmstudio.py", allowed_paths=["backend/**"],
                        required_outputs=["backend/vision_lmstudio.py"])
    builder.run(order, str(tmp_path))
    info = builder.last_execution_info
    assert info["context_files"][:2] == ["backend/vision_lmstudio.py", "backend/tests/test_vision_lmstudio.py"]
    assert "NAMED-TEST-MARKER" in seen["prompt"] and info["omitted_files"]
    assert "WRITABLE FILES NOT SHOWN" in seen["prompt"] and info["omitted_files"][0] in seen["prompt"]
    assert info["prompt_chars"] < 200_000


def test_full_file_proposals_still_work(tmp_path, monkeypatch):
    reply(monkeypatch, {"files": [{"path": "src/a.py", "content": "A = 1\n"}]})
    res = ProposalBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/**"]), str(tmp_path))
    assert res.files_modified == ["src/a.py"] and (tmp_path / "src/a.py").read_text(encoding="utf-8") == "A = 1\n"


def test_anchor_miss_gets_retry_advice():
    from core.autopilot import builder_retry_feedback
    assert "verbatim" in builder_retry_feedback("Edit for a.py must match exactly once (found 0)",
                                        OrderSchema(goal="g", allowed_paths=["src/**"]))
