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
    assert data["caps"]["public"] == data["caps"]["lifetime"]  # the test app is not the public demo: no reserve
    assert data["reserve"] == {"usd": 7.5, "until": "2026-12-01", "held": False}
    assert data["rate_limit_key"] == "testclient"  # whose bucket this request counts against (checks the proxy setup)


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


def test_demo_without_spend_snapshots_keeps_public_runs_closed(tmp_path):
    from fixmytutorial.config import Settings
    s = Settings(_env_file=None, data_dir=tmp_path, sandbox_backend="fake", public_demo=True)
    store = Store(tmp_path / "s3.db")
    ledger = Ledger(tmp_path / "l3.db", s)
    deps = Deps(settings=s, store=store, ledger=ledger, bus=EventBus(store, ping_s=0.05),
                queue=FakeQueue(store), limiter=RateLimiter(2), snapshot=None)
    with TestClient(create_app(deps)) as client:
        assert "spend records are not set up" in client.get("/").text


def test_heal_loop_reopens_public_runs_after_a_hub_error(settings, tmp_path):
    import time

    class RecoveringSnapshot:
        enabled = True

        def __init__(self, ledger):
            self.ledger = ledger

        def restore(self):
            self.ledger.mark_unverified("past spend could not be loaded.")
            return 0

        def heal(self):
            self.ledger.clear_unverified("past spend could not be loaded.")

    s = settings.model_copy(update={"snapshot_heal_s": 0.05})
    store = Store(tmp_path / "s4.db")
    ledger = Ledger(tmp_path / "l4.db", s)
    deps = Deps(settings=s, store=store, ledger=ledger, bus=EventBus(store, ping_s=0.05),
                queue=FakeQueue(store), limiter=RateLimiter(2), snapshot=RecoveringSnapshot(ledger))
    with TestClient(create_app(deps)) as client:
        for _ in range(40):
            if "paused" not in client.get("/").text:
                break
            time.sleep(0.05)
        assert "paused" not in client.get("/").text


def test_snapshots_only_run_in_the_public_demo(tmp_path):
    from fixmytutorial.config import Settings
    from fixmytutorial.web.app import snapshot_for
    laptop = Settings(_env_file=None, data_dir=tmp_path, hf_token="hf_x")
    space = Settings(_env_file=None, data_dir=tmp_path, hf_token="hf_x", public_demo=True)
    store, ledger = Store(tmp_path / "s5.db"), Ledger(tmp_path / "l5.db", laptop)
    assert snapshot_for(laptop, store, ledger) is None
    snap = snapshot_for(space, store, ledger)
    assert snap.enabled and snap.owns_ledger


def test_server_leaves_forwarded_headers_to_the_app():
    """uvicorn's own proxy handling trusts the leftmost (client-written) X-Forwarded-For entry, which would undo
    client_ip's rate-limit keying, so the container must not enable it."""
    from pathlib import Path
    cmd = next(ln for ln in (Path(__file__).parents[1] / "Dockerfile").read_text().splitlines() if ln.startswith("CMD"))
    assert "--no-proxy-headers" in cmd and "--forwarded-allow-ips" not in cmd


def test_report_page_says_when_a_check_was_partial(env):
    client, deps = env
    rec = done_record()
    deps.store.save_run(rec.model_copy(update={"id": "part1", "plan": rec.plan.model_copy(update={"coverage": 0.8})}))
    assert "Partly checked: we ran 80% of the code on this page." in client.get("/r/part1").text
    deps.store.save_run(rec)
    assert "Partly checked" not in client.get("/r/done1").text


def test_inline_run_data_never_contains_markup(env):
    client, deps = env
    deps.store.save_run(done_record().model_copy(update={"id": "html1", "title": "x <!-- <script> y",
                                                         "status": RunStatus.EXECUTING}))
    page = client.get("/run/html1").text
    data = page.split('id="initial">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data and json.loads(data)["title"] == "x <!-- <script> y"


def test_cached_reports_still_open_while_public_runs_are_paused(env):
    client, deps = env
    deps.store.save_run(done_record())
    deps.ledger.mark_unverified("past spend could not be loaded.")
    r = client.post("/runs", data={"url": "https://a.example/t"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/run/done1"


def test_pages_name_the_sandbox_that_actually_runs_the_code(env):
    client, deps = env
    assert "Token Factory Sandboxes" not in client.get("/").text
    deps.settings = deps.settings.model_copy(update={"sandbox_backend": "contree"})
    assert "Token Factory Sandboxes" in client.get("/").text
    deps.settings = deps.settings.model_copy(update={"sandbox_backend": "docker"})
    assert "isolated Docker sandboxes" in client.get("/").text


def test_static_assets_are_versioned_so_redeploys_reach_returning_visitors(env):
    client, _ = env
    page = client.get("/").text
    assert "/static/app.css?v=" in page and "/static/vendor/tabs.min.js?v=" in page


def test_non_web_links_are_rejected_on_the_form(env):
    client, deps = env
    r = client.post("/runs", data={"url": "ftp://example.com/tutorial"}, follow_redirects=False)
    assert r.status_code == 400 and "Only http and https links are supported." in r.text
    assert deps.queue.submitted == []


def test_failed_and_unrunnable_checks_offer_no_badge(env):
    client, deps = env
    for i, verdict in enumerate([Verdict.ERROR, Verdict.NOT_RUNNABLE]):
        deps.store.save_run(done_record().model_copy(update={"id": f"nb{i}", "verdict": verdict}))
        assert "/badge/nb" not in client.get(f"/r/nb{i}").text
    deps.store.save_run(done_record())
    assert "/badge/done1.svg" in client.get("/r/done1").text


def test_live_streams_are_capped_per_visitor_and_released_when_done(env):
    client, deps = env
    deps.store.save_run(done_record())
    deps.store.append_event("done1", {"type": "done"})
    deps.streams.per_client = 1
    assert deps.streams.acquire("testclient")  # another tab already streaming
    assert client.get("/run/done1/events").status_code == 429
    deps.streams.release("testclient")
    for _ in range(2):  # a finished stream gives its slot back
        with client.stream("GET", "/run/done1/events") as r:
            assert r.status_code == 200
            "".join(r.iter_text())


def test_oversized_request_bodies_are_refused_before_parsing(env):
    from fixmytutorial.web.app import MAX_REQUEST_BYTES
    client, deps = env
    r = client.post("/runs", data={"markdown": "x" * (MAX_REQUEST_BYTES + 10)}, follow_redirects=False)
    assert r.status_code == 413 and deps.queue.submitted == []


def test_oversized_chunked_bodies_are_refused_too(env):
    from fixmytutorial.web.app import MAX_REQUEST_BYTES
    client, deps = env

    def chunks():
        yield b"markdown="
        for _ in range(MAX_REQUEST_BYTES // 65536 + 2):
            yield b"x" * 65536

    r = client.post("/runs", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert r.status_code == 413 and deps.queue.submitted == []


def test_the_largest_allowed_markdown_still_gets_through(env):
    client, deps = env
    r = client.post("/runs", data={"markdown": "é" * 59_999}, follow_redirects=False)
    assert r.status_code == 303 and len(deps.queue.submitted) == 1


def test_url_query_prefills_the_form_without_starting_a_run(env):
    client, deps = env
    r = client.get("/", params={"url": "https://c.example/guide?x=1&y=2"})
    assert r.status_code == 200
    assert 'name="url" value="https://c.example/guide?x=1&amp;y=2"' in r.text
    assert deps.queue.submitted == [] and deps.store.list_runs(limit=10) == []


def test_url_query_ignores_non_web_links(env):
    client, _ = env
    for bad in ["javascript:alert(1)", "ftp://x.example/a", "https://x.example/" + "a" * 2100]:
        r = client.get("/", params={"url": bad})
        assert r.status_code == 200 and 'name="url" value=""' in r.text


def test_url_query_links_a_recent_report(env):
    client, deps = env
    deps.store.save_run(done_record())
    r = client.get("/", params={"url": "https://a.example/t"})
    assert "We already checked this page" in r.text and 'href="/r/done1">See the report' in r.text
    assert "We already checked this page" not in client.get("/", params={"url": "https://c.example/t"}).text


def test_home_offers_the_bookmark(env):
    client, deps = env
    base = deps.settings.public_base_url.rstrip("/")
    r = client.get("/")
    assert f"javascript:location.href=&#39;{base}/?url=&#39;+encodeURIComponent(location.href)" in r.text


def test_report_offers_an_issue_for_tutorials_on_github(env):
    client, deps = env
    deps.store.save_run(done_record().model_copy(update={
        "id": "gh1", "source": "https://github.com/owner/repo/blob/main/guide.md"}))
    r = client.get("/r/gh1")
    assert "Tell the author" in r.text and "Open an issue on owner/repo" in r.text
    assert 'href="https://github.com/owner/repo/issues/new?title=' in r.text


def test_report_offers_a_note_elsewhere_and_only_for_broken_tutorials(env):
    client, deps = env
    deps.store.save_run(done_record())  # a.example: no GitHub repo
    r = client.get("/r/done1")
    assert "Copy note for the author" in r.text and "Open an issue" not in r.text
    deps.store.save_run(done_record().model_copy(update={"id": "ok1", "verdict": Verdict.STILL_WORKS}))
    assert "Tell the author" not in client.get("/r/ok1").text


def test_report_says_when_the_tutorial_broke(env):
    from datetime import date

    from fixmytutorial.models import BreakPoint
    client, deps = env
    deps.store.save_run(done_record())
    assert "When it broke" not in client.get("/r/done1").text
    bp = BreakPoint(package="pandas", step_id="s1", first_bad="2.0.0", first_bad_date=date(2023, 4, 3),
                    last_good="1.5.3", last_good_date=date(2023, 1, 19), how="ran")
    deps.store.save_run(done_record().model_copy(update={"id": "dated", "broke_in": bp}))
    r = client.get("/r/dated")
    assert "When it broke" in r.text and "Broke in pandas 2.0.0, released 3 Apr 2023." in r.text


def test_index_lists_the_releases_that_broke_tutorials(env):
    client, deps = env
    idx = json.loads(deps.settings.rot_index_path.read_text())
    idx["entries"][0].update({"published": "2022-03-14",
                              "broke_in": {"package": "pandas", "version": "2.0.0", "date": "2023-04-03",
                                           "how": "ran"}})
    deps.settings.rot_index_path.write_text(json.dumps(idx))
    r = client.get("/index")
    assert "Releases that broke the most tutorials" in r.text and "pandas 2.0.0" in r.text
    assert "Broke in pandas 2.0.0" in r.text


def test_malformed_links_never_crash_the_home_page_or_the_form(env):
    client, deps = env
    r = client.get("/", params={"url": "http://[abc"})
    assert r.status_code == 200 and 'name="url" value=""' in r.text
    r = client.post("/runs", data={"url": "http://[abc"})
    assert r.status_code == 400 and deps.queue.submitted == []


def post_with(deps, peer, n=3, xff=None, cf=None):
    with TestClient(create_app(deps), client=(peer, 5000)) as c:
        out = []
        for i in range(n):
            h = {}
            if xff:
                h["X-Forwarded-For"] = xff(i)
            if cf:
                h["CF-Connecting-IP"] = cf(i)
            out.append(c.post("/runs", data={"url": f"https://cf-{peer}-{i}.example/t"}, follow_redirects=False,
                              headers=h).status_code)
        return out


def test_visitors_behind_cloudflare_get_their_own_limits(env):
    _, deps = env
    deps.settings = deps.settings.model_copy(update={"client_ip_header": "cf-connecting-ip"})
    assert post_with(deps, "10.1.2.3", xff=lambda i: f"203.0.113.{i}, 172.69.122.176",
                     cf=lambda i: f"203.0.113.{i}") == [303, 303, 303]


def test_a_direct_visitor_cannot_fake_the_cloudflare_header(env):
    _, deps = env
    deps.settings = deps.settings.model_copy(update={"client_ip_header": "cf-connecting-ip"})
    assert post_with(deps, "8.8.4.4", cf=lambda i: f"203.0.113.{i}")[-1] == 429
