import json

import pytest
from fastapi.testclient import TestClient

from fixmytutorial.events import EventBus
from fixmytutorial.jobs import RateLimiter
from fixmytutorial.ledger import Ledger
from fixmytutorial.models import RunPlan, RunRecord, RunStatus, Step, Verdict, utcnow
from fixmytutorial.store import Store
from fixmytutorial.web.app import Deps, create_app


class FakeQueue:
    def __init__(self, store):
        self.store = store
        self.submitted = []

    def submit(self, rec, source):
        self.store.save_run(rec)
        self.submitted.append((rec, source))


@pytest.fixture
def env(settings, tmp_path):
    idx = tmp_path / "results.json"
    idx.write_text(json.dumps({"generated_at": "2026-10-25", "entries": [
        {"url": "https://a.example/t", "title": "Tut A", "language": "python", "source": "a.example", "year": 2022,
         "verdict": "fixed", "categories": ["removed_api"], "changes": 1, "run_id": "done1", "featured": True},
        {"url": "https://b.example/t", "title": "Tut B", "language": "javascript", "source": "b.example",
         "year": 2023, "verdict": "still_works", "categories": [], "changes": 0, "run_id": None}]}))
    s = settings.model_copy(update={"rot_index_path": idx, "admin_token": "sekret"})
    store = Store(tmp_path / "s.db")
    deps = Deps(settings=s, store=store, ledger=Ledger(tmp_path / "l.db", s), bus=EventBus(store, ping_s=0.05),
                queue=FakeQueue(store), limiter=RateLimiter(2))
    with TestClient(create_app(deps)) as client:
        yield client, deps


def done_record():
    plan = RunPlan(language="python", runtime="3.12",
                   steps=[Step(id="s1", title="Append", kind="run_code", content="df.append(x)")])
    final = RunPlan(language="python", runtime="3.12", steps=[plan.steps[0].model_copy(
        update={"content": "pd.concat([df, x])", "fix_reason": "pandas 2.0 removed append"})])
    return RunRecord(id="done1", source="https://a.example/t", title="Tut A", status=RunStatus.DONE,
                     verdict=Verdict.FIXED, plan=plan, final_plan=final, summary="Fixed append.",
                     created_at=utcnow(), finished_at=utcnow())


def test_home_renders_with_featured_example(env):
    client, _ = env
    r = client.get("/")
    assert r.status_code == 200 and "Is that tutorial still working?" in r.text and "Tut A" in r.text


def test_home_shows_paused_banner(env):
    client, deps = env
    deps.ledger._s = deps.settings.model_copy(update={"cap_daily_usd": 0.0})
    r = client.get("/")
    assert "paused" in r.text and "disabled" in r.text


def test_submit_redirects_and_queues(env):
    client, deps = env
    r = client.post("/runs", data={"url": "https://c.example/t"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/run/")
    rec, source = deps.queue.submitted[0]
    assert source.url == "https://c.example/t" and rec.public


def test_submit_requires_input(env):
    client, _ = env
    r = client.post("/runs", data={"url": " ", "markdown": ""})
    assert r.status_code == 400 and "Paste a tutorial link" in r.text


def test_cache_hit_redirects_to_existing_run(env):
    client, deps = env
    deps.store.save_run(done_record())
    r = client.post("/runs", data={"url": "https://a.example/t"}, follow_redirects=False)
    assert r.headers["location"] == "/run/done1" and deps.queue.submitted == []


def test_rate_limit(env):
    client, _ = env
    for i in range(2):
        client.post("/runs", data={"url": f"https://x{i}.example/t"}, follow_redirects=False)
    r = client.post("/runs", data={"url": "https://x9.example/t"}, follow_redirects=False)
    assert r.status_code == 429 and "runs in the last hour" in r.text



def test_rate_limit_ignores_spoofed_forwarded_for(env):
    client, _ = env
    for i in range(3):
        r = client.post("/runs", data={"url": f"https://y{i}.example/t"}, follow_redirects=False,
                        headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"})
    assert r.status_code == 429


def post_from(deps, peer, forwarded, n=3):
    def headers(i):
        values = forwarded(i)
        return [("X-Forwarded-For", v) for v in ([values] if isinstance(values, str) else values)]

    with TestClient(create_app(deps), client=(peer, 5000)) as c:
        return [c.post("/runs", data={"url": f"https://{peer}-{i}.example/t"}, follow_redirects=False,
                       headers=headers(i)).status_code for i in range(n)]


def test_direct_public_peer_cannot_spoof_forwarded_for(env):
    _, deps = env
    assert post_from(deps, "8.8.4.4", lambda i: f"203.0.113.{i}")[-1] == 429


def test_users_behind_private_proxy_get_separate_limits(env):
    _, deps = env
    assert post_from(deps, "10.1.2.3", lambda i: f"1.1.1.1, 203.0.113.{i}") == [303, 303, 303]


def test_proxy_header_line_wins_over_a_client_header_line(env):
    _, deps = env
    assert post_from(deps, "10.1.2.3", lambda i: [f"203.0.113.{i}", "8.8.8.8"])[-1] == 429


def test_unparsable_forwarded_value_falls_back_to_the_proxy(env):
    _, deps = env
    assert post_from(deps, "10.1.2.3", lambda i: f"not-an-ip-{i}")[-1] == 429


def test_ipv6_addresses_in_one_slash_64_share_a_limit(env):
    _, deps = env
    assert post_from(deps, "10.1.2.3", lambda i: f"2a00:1450:4001:81c::{i + 1}")[-1] == 429

def test_run_page_and_404(env):
    client, deps = env
    deps.store.save_run(done_record())
    page = client.get("/run/done1")
    assert page.status_code == 200 and 'id="initial"' in page.text and "/static/run.js" in page.text
    assert client.get("/run/nope").status_code == 404


def test_events_stream_replays_history(env):
    client, deps = env
    deps.store.save_run(done_record())
    for ev in [{"type": "stage", "stage": "planning"}, {"type": "verdict", "verdict": "fixed"}, {"type": "done"}]:
        deps.store.append_event("done1", ev)
    with client.stream("GET", "/run/done1/events") as r:
        body = "".join(r.iter_text())
    assert "event: stage" in body and "event: done" in body and "id: 3" in body


def test_report_page(env):
    client, deps = env
    deps.store.save_run(done_record())
    r = client.get("/r/done1")
    assert r.status_code == 200
    assert "pandas 2.0 removed append" in r.text and "/badge/done1.svg" in r.text and "og:image" in r.text


def test_badge_and_og(env):
    client, deps = env
    deps.store.save_run(done_record())
    assert client.get("/badge/done1.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/og/done1.png").content[:4] == b"\x89PNG"


def test_index_page(env):
    client, _ = env
    r = client.get("/index")
    assert r.status_code == 200 and "Tut B" in r.text and "50%" in r.text


def test_admin_requires_token(env):
    client, _ = env
    assert client.get("/admin/ledger").status_code == 404
    data = client.get("/admin/ledger?token=sekret").json()
    assert "lifetime_usd" in data and data["public_runs_open"] is True


def test_healthz(env):
    client, _ = env
    assert client.get("/healthz").json() == {"ok": True}


def test_failed_restore_pauses_public_runs_on_boot(settings, tmp_path):
    class FailingSnapshot:
        enabled = True

        def __init__(self, ledger):
            self.ledger = ledger

        def restore(self):
            self.ledger.mark_unverified("past spend could not be loaded.")
            return 0

    store = Store(tmp_path / "s2.db")
    ledger = Ledger(tmp_path / "l2.db", settings)
    deps = Deps(settings=settings, store=store, ledger=ledger, bus=EventBus(store, ping_s=0.05),
                queue=FakeQueue(store), limiter=RateLimiter(2), snapshot=FailingSnapshot(ledger))
    with TestClient(create_app(deps)) as client:
        assert "past spend could not be loaded" in client.get("/").text
        r = client.post("/runs", data={"url": "https://z.example/t"}, follow_redirects=False)
        assert r.status_code == 503 and deps.queue.submitted == []
