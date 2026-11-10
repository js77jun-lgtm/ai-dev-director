"""
v3.1.1: HEAD planned "create data/missions.json" while the same ORDER protected data/*.json. The Builder could never
write the file, and the mission burned milestone after milestone on it. An ORDER whose required outputs contradict its
own scope now goes back to HEAD at planning time, with the exact conflict; nothing is widened or unprotected.
"""
import pytest

from agents.mock_head import MockHead
from core.autopilot import order_conflicts
from core.mission import MissionRunner, MissionStore
from schemas.order import OrderSchema
from tests.test_v306_head_proxy_autonomy import ORDER, STEP2, mission_factory


def test_self_contradicting_order_is_named_before_a_run_is_spent():
    bad = OrderSchema(goal="create data/missions.json", allowed_paths=["tests/**"], protected_paths=["data/*.json"],
                      required_outputs=["data/missions.json (10+ missions)", "tests/a.test.cjs", "works on mobile"])
    problems = order_conflicts(bad)
    assert len(problems) == 1 and "data/missions.json" in problems[0] and "protected_paths" in problems[0]
    outside = OrderSchema(goal="g", allowed_paths=["tests/**"], required_outputs=["src/a.py"])
    assert "not inside allowed_paths" in order_conflicts(outside)[0]
    good = OrderSchema(goal="g", allowed_paths=["tests/**", "data/missions.json"], protected_paths=["data/daily.json"],
                       required_outputs=["data/missions.json", "tests/a.test.cjs"])
    assert order_conflicts(good) == []


@pytest.mark.head_proxy
def test_mission_planner_returns_a_conflicting_order_to_head(tmp_path, monkeypatch):
    builds, seen = [], []
    conflicting = dict(STEP2, allowed_paths=["tests/**"], protected_paths=["feature2.py"])
    plans = [{"status": "CONTINUE", "summary": "", "reason": "", "order": conflicting},
             {"status": "CONTINUE", "summary": "", "reason": "", "order": STEP2},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)

    def plan_next(self, goal, history, ctx, cwd=None):
        seen.append(ctx.get("previous_plan_rejected"))
        return plans.pop(0)
    monkeypatch.setattr(MockHead, "plan_next", plan_next)
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "double then quad", OrderSchema(**ORDER), config={"autonomy": {"max_steps": 4}})
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE" and len(done["steps"]) == 2          # the conflicting ORDER never became a step
    assert seen[0] is None and "ORDER_CONFLICT" in seen[1] and "feature2.py" in seen[1]
