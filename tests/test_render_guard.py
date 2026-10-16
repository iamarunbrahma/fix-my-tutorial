from datetime import datetime, timezone

import pytest

from scripts.render_guard import Config, decide, run_guard

CFG = Config(service="fix-my-tutorial", plan="starter", end_date="2026-12-16", bandwidth_cap_gb=30.0)
OCT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def test_a_normal_day_keeps_the_demo_running():
    assert decide(OCT, CFG, bandwidth_gb=1.2, plan="starter", instances=1) == []


@pytest.mark.parametrize("now,bw,plan,inst,why", [
    (datetime(2026, 12, 16, tzinfo=timezone.utc), 0.0, "starter", 1, "2026-12-16"),
    (OCT, 30.0, "starter", 1, "30.0 GB"),
    (OCT, 0.0, "standard", 1, "standard"),
    (OCT, 0.0, "starter", 2, "2 instances"),
])
def test_anything_that_could_cost_more_suspends_it(now, bw, plan, inst, why):
    reasons = decide(now, CFG, bandwidth_gb=bw, plan=plan, instances=inst)
    assert reasons and why in " ".join(reasons)


class FakeRender:
    def __init__(self, suspended="not_suspended", plan="starter", unit="mb", mb=500.0):
        self.svc = {"id": "srv-1", "name": "fix-my-tutorial", "type": "web_service", "suspended": suspended,
                    "serviceDetails": {"plan": plan, "numInstances": 1}}
        self.unit, self.mb, self.posts, self.gets = unit, mb, [], []

    def get(self, path):
        self.gets.append(path)
        if path.startswith("/services?"):
            return [{"service": self.svc}, {"service": {**self.svc, "id": "srv-2", "name": "upstream"}}]
        if path.startswith("/metrics/bandwidth?"):
            return [{"unit": self.unit, "values": [{"value": self.mb}, {"value": self.mb}]}]
        raise AssertionError(path)

    def post(self, path):
        self.posts.append(path)


def test_guard_reads_only_its_own_service_and_leaves_it_running():
    api = FakeRender()
    out = run_guard(api, CFG, OCT)
    assert out["reasons"] == [] and api.posts == [] and out["bandwidth_gb"] == pytest.approx(1.0)
    assert "resource=srv-1" in next(g for g in api.gets if g.startswith("/metrics"))


def test_guard_suspends_after_judging_and_only_once():
    api = FakeRender()
    run_guard(api, CFG, datetime(2026, 12, 17, tzinfo=timezone.utc))
    assert api.posts == ["/services/srv-1/suspend"]
    already = FakeRender(suspended="suspended")
    run_guard(already, CFG, datetime(2026, 12, 17, tzinfo=timezone.utc))
    assert already.posts == []


def test_an_unreadable_bandwidth_answer_stops_the_run():
    with pytest.raises(ValueError):
        run_guard(FakeRender(unit="furlongs"), CFG, OCT)


def test_a_missing_service_stops_the_run():
    api = FakeRender()
    api.svc["name"] = "something-else"
    with pytest.raises(RuntimeError):
        run_guard(api, CFG, OCT)
