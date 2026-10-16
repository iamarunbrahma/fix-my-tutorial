from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from sse_starlette.sse import EventSourceResponse

from ..author import author_note, github_repo_for, issue_url
from ..config import Settings, get_settings
from ..events import EventBus
from ..ingest import Source
from ..jobs import JobQueue, RateLimiter, StreamLimiter
from ..ledger import Ledger
from ..models import RunRecord, RunStatus, Verdict, utcnow
from ..report import (
    VERDICT_TEXT, badge_svg, break_stats, broke_in_source, broke_in_text, build_changes, diff_lines, og_png,
    partial_note,
)
from ..snapshot import NOT_CONFIGURED
from ..store import Store

HERE = Path(__file__).parent
log = logging.getLogger("fixmytutorial.web")
CATEGORY_TEXT = {
    "removed_api": "A function or class was removed or renamed",
    "renamed_package": "A package or module was renamed",
    "version_conflict": "Package versions no longer fit together",
    "dead_resource": "A file, dataset or URL no longer exists",
    "changed_cli": "A command or flag changed",
    "tutorial_typo": "A mistake in the tutorial itself",
    "environment": "Something missing in the environment",
    "timeout": "A step never finished",
    "unknown": "Something else",
}


@dataclass
class Deps:
    settings: Settings
    store: Store
    ledger: Ledger
    bus: EventBus
    queue: object
    limiter: RateLimiter
    snapshot: object | None = None
    streams: StreamLimiter | None = None


def build_deps(settings: Settings) -> Deps:
    from ..llm import LLM
    from ..pipeline import Pipeline
    from ..sandbox import make_backend
    from ..search import SearchClient

    store = Store(settings.db_path)
    ledger = Ledger(settings.ledger_db_path, settings)
    bus = EventBus(store)
    snapshot = snapshot_for(settings, store, ledger)
    pipeline = Pipeline(
        settings=settings,
        llm=LLM(settings, ledger),
        search=SearchClient(settings, ledger, store),
        backend=make_backend(settings),
        store=store,
        ledger=ledger,
        bus=bus,
        snapshot=snapshot,
    )
    return Deps(
        settings=settings,
        store=store,
        ledger=ledger,
        bus=bus,
        queue=JobQueue(pipeline, store, bus, settings),
        limiter=RateLimiter(settings.rate_limit_runs_per_hour),
        snapshot=snapshot,
    )


def snapshot_for(settings: Settings, store: Store, ledger: Ledger):
    """Only the public demo persists to the Hub dataset (and owns the remote ledger). A laptop server keeps
    everything local, so a test run can never upload runs or touch the demo's spend records."""
    if not settings.public_demo:
        return None
    from ..snapshot import Snapshotter
    return Snapshotter(settings, store, ledger, owns_ledger=True)


async def heal_loop(d: Deps) -> None:
    """While public runs are paused by a Hub error, keep retrying the load or save that failed."""
    while True:
        await asyncio.sleep(d.settings.snapshot_heal_s)
        if d.ledger.unverified():
            try:
                await asyncio.to_thread(d.snapshot.heal)
            except Exception:
                log.exception("snapshot heal failed")


@lru_cache(maxsize=None)
def asset(path: str) -> str:
    """A static file's URL with a short content hash, so a redeploy never serves visitors a stale copy."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


# The largest legitimate form: 60,000 characters of multi-byte text, URL-encoded (about 720 KB), plus a link.
MAX_REQUEST_BYTES = 1024 * 1024


class _BodyTooLarge(BaseException):
    """BaseException on purpose: form parsing turns any Exception into a generic 400 and keeps going."""


class BodySizeLimit:
    """Refuse request bodies over max_bytes before they are buffered, whether the size is declared or chunked."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            return await self._refuse(send)
        seen, started = 0, False

        async def counted_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            if not started:
                await self._refuse(send)

    @staticmethod
    async def _refuse(send) -> None:
        body = b"That request is too large."
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


SANDBOX_LABELS = {"contree": "Token Factory Sandboxes", "docker": "isolated Docker sandboxes"}


def sandbox_label(request: Request) -> str:
    """What runs the tutorials, in words, so the pages never claim Sandboxes while running on Docker."""
    return SANDBOX_LABELS.get(request.app.state.deps.settings.sandbox_backend, "isolated sandboxes")


def rate_key(addr: str) -> str | None:
    """Normalize an address for rate limiting; IPv6 clients usually hold a whole /64, so bucket by that."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def web_link(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme.lower() in ("http", "https") and bool(u.hostname)
    except ValueError:  # e.g. "http://[abc"
        return False


def load_index(settings: Settings) -> dict:
    try:
        return json.loads(Path(settings.rot_index_path).read_text())
    except (OSError, ValueError):
        return {"generated_at": None, "entries": []}


def verdict_label(v: str | Verdict | None) -> str:
    try:
        return VERDICT_TEXT[Verdict(v)]
    except (ValueError, KeyError):
        return "Unknown"


async def recover_unfinished(d: Deps) -> None:
    for rec in d.store.list_runs(limit=500):
        if rec.status not in (RunStatus.DONE, RunStatus.FAILED):
            rec.status, rec.verdict = RunStatus.FAILED, Verdict.ERROR
            rec.reason = "The server restarted during this run. Please run it again."
            rec.finished_at = utcnow()
            d.store.save_run(rec)
            await d.bus.publish(
                rec.id,
                {
                    "type": "verdict",
                    "verdict": "error",
                    "reason": rec.reason,
                    "summary": "",
                    "cost_usd": rec.cost_usd,
                },
            )
            await d.bus.publish(rec.id, {"type": "done"})


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        d = deps or build_deps(get_settings())
        if d.streams is None:
            d.streams = StreamLimiter(d.settings.max_streams_per_client, d.settings.max_streams_total)
        heal = None
        if d.snapshot is not None and getattr(d.snapshot, "enabled", False):
            restored = await asyncio.to_thread(d.snapshot.restore)
            log.info("restored %d runs from snapshot", restored)
            heal = asyncio.create_task(heal_loop(d))
        elif d.settings.public_demo:  # without snapshots the demo would forget its spend on every restart
            d.ledger.mark_unverified(NOT_CONFIGURED)
        await recover_unfinished(d)
        app.state.deps = d
        yield
        if heal is not None:
            heal.cancel()

    app = FastAPI(title="Fix My Tutorial", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(BodySizeLimit, max_bytes=MAX_REQUEST_BYTES)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        verdict_label=verdict_label, cat_label=lambda c: CATEGORY_TEXT.get(c, c), diff_lines=diff_lines,
        partial_note=partial_note, sandbox_label=sandbox_label, asset=asset, broke_in_text=broke_in_text,
        broke_in_source=broke_in_source,
    )

    def D(request: Request) -> Deps:
        return request.app.state.deps

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        try:
            behind_proxy = ipaddress.ip_address(peer).is_private
        except ValueError:
            behind_proxy = False
        # Our proxy appends the real client to the last header line; everything before it is client-supplied.
        # A direct client writes the whole header, so it is ignored then.
        hops = [h.strip() for line in request.headers.getlist("x-forwarded-for") for h in line.split(",")]
        hops = [h for h in hops if h]
        candidate = hops[-1] if behind_proxy and hops else peer
        # Some hosts sit behind a CDN that appends its own address last (Render: Cloudflare). The CDN's own header
        # carries the visitor and is overwritten on every request, so it is trusted only when we are behind a proxy.
        trusted = D(request).settings.client_ip_header
        if behind_proxy and trusted and request.headers.get(trusted):
            candidate = request.headers[trusted].strip()
        return rate_key(candidate) or rate_key(peer) or peer

    def home_ctx(request: Request, **extra) -> dict:
        d = D(request)
        ok, why = d.ledger.public_runs_open()
        featured = [
            dict(e, verdict_label=verdict_label(e.get("verdict")))
            for e in load_index(d.settings).get("entries", [])
            if e.get("featured") and e.get("run_id")
        ][:3]
        base = d.settings.public_base_url.rstrip("/")
        return {
            "paused": None if ok else why,
            "per_hour": d.settings.rate_limit_runs_per_hour,
            "examples": featured,
            "bookmarklet": f"javascript:location.href='{base}/?url='+encodeURIComponent(location.href)",
            "recent": None,
            "error": None,
            "url": "",
            "markdown": "",
            **extra,
        }

    def not_found(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "notfound.html", {}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, url: str = ""):
        # A shared ?url= link only fills in the form: starting a run (and spending) always takes a click.
        url = url.strip()
        if len(url) > 2000 or not web_link(url):
            url = ""
        recent = D(request).store.find_recent_run(url, D(request).settings.cache_ttl_s) if url else None
        return templates.TemplateResponse(request, "home.html", home_ctx(request, url=url, recent=recent))

    @app.post("/runs")
    async def create_run(request: Request, url: str = Form(""), markdown: str = Form("")):
        d = D(request)
        url, markdown = url.strip(), markdown.strip()
        if not url and not markdown:
            return templates.TemplateResponse(
                request,
                "home.html",
                home_ctx(request, error="Paste a tutorial link or its Markdown."),
                status_code=400,
            )
        if url and not web_link(url):
            return templates.TemplateResponse(
                request, "home.html", home_ctx(request, url=url, error="Only http and https links are supported."),
                status_code=400,
            )
        if len(markdown) > d.settings.max_tutorial_chars:
            return templates.TemplateResponse(
                request,
                "home.html",
                home_ctx(
                    request,
                    markdown=markdown[:2000],
                    error="That Markdown is too long (limit 60,000 characters).",
                ),
                status_code=400,
            )
        if url:  # a recent report costs nothing to show, so it works even while public runs are paused
            cached = d.store.find_recent_run(url, d.settings.cache_ttl_s)
            if cached is not None:
                return RedirectResponse(f"/run/{cached.id}", status_code=303)
        ok, _ = d.ledger.public_runs_open()
        if not ok:
            return templates.TemplateResponse(
                request, "home.html", home_ctx(request, url=url, markdown=markdown), status_code=503
            )
        if not d.limiter.allow(client_ip(request)):
            msg = (
                f"You've started {d.settings.rate_limit_runs_per_hour} runs in the last hour. "
                "Try again a little later, or browse the Rot Index."
            )
            return templates.TemplateResponse(
                request,
                "home.html",
                home_ctx(request, url=url, markdown=markdown, error=msg),
                status_code=429,
            )
        source = Source(url=url or None, markdown=None if url else markdown)
        rec = RunRecord(id=secrets.token_urlsafe(8), source=source.label(), public=True, created_at=utcnow())
        d.queue.submit(rec, source)
        return RedirectResponse(f"/run/{rec.id}", status_code=303)

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        rec = D(request).store.get_run(run_id)
        if rec is None:
            return not_found(request)
        initial = rec.model_dump_json().replace("<", "\\u003c")  # no markup can reach the HTML parser
        return templates.TemplateResponse(request, "run.html", {"rec": rec, "initial": initial})

    @app.get("/run/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = 0):
        d = D(request)
        if d.store.get_run(run_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        last = request.headers.get("last-event-id", "")
        start = int(last) if last.isdigit() else after
        key = client_ip(request)
        if not d.streams.acquire(key):  # a non-200 answer also stops the browser from reconnecting
            return JSONResponse({"error": "Too many live pages open. Close some tabs and reload."}, status_code=429)
        released = False

        def release() -> None:  # runs from whichever finishes first: the stream or the response
            nonlocal released
            if not released:
                released = True
                d.streams.release(key)

        async def gen():
            try:
                async for seq, ev in d.bus.subscribe(run_id, start):
                    if await request.is_disconnected():
                        break
                    if ev["type"] == "ping":
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"id": str(seq), "event": ev["type"], "data": json.dumps(ev)}
            finally:
                release()

        return EventSourceResponse(gen(), ping=20, background=BackgroundTask(release))

    @app.get("/r/{run_id}", response_class=HTMLResponse)
    async def report(request: Request, run_id: str):
        d = D(request)
        rec = d.store.get_run(run_id)
        if rec is None:
            return not_found(request)
        if rec.status not in (RunStatus.DONE, RunStatus.FAILED):
            return RedirectResponse(f"/run/{run_id}", status_code=303)
        base = d.settings.public_base_url.rstrip("/")
        author = None
        if rec.verdict in (Verdict.FIXED, Verdict.COULD_NOT_FIX):
            title, body = author_note(rec, f"{base}/r/{rec.id}")
            repo = github_repo_for(rec.source)
            author = {"note": f"**{title}**\n\n{body}", "repo": "/".join(repo) if repo else None,
                      "issue_url": issue_url(*repo, title, body, f"{base}/r/{rec.id}") if repo else None}
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "rec": rec,
                "author": author,
                "changes": build_changes(rec),
                "verdict_text": verdict_label(rec.verdict),
                "base": base,
                "badge_md": f"[![Fix My Tutorial]({base}/badge/{rec.id}.svg)]({base}/r/{rec.id})",
                "badge_html": f'<a href="{base}/r/{rec.id}"><img src="{base}/badge/{rec.id}.svg" alt="Fix My Tutorial"></a>',
            },
        )

    @app.get("/badge/{run_id}.svg")
    async def badge(request: Request, run_id: str):
        rec = D(request).store.get_run(run_id)
        if rec is None:
            return Response(status_code=404)
        return Response(badge_svg(rec), media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})

    @app.get("/og/{run_id}.png")
    async def og(request: Request, run_id: str):
        rec = D(request).store.get_run(run_id)
        if rec is None:
            return Response(status_code=404)
        return Response(og_png(rec), media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @app.get("/index", response_class=HTMLResponse)
    async def rot_index(request: Request):
        data = load_index(D(request).settings)
        entries = data.get("entries", [])
        total = len(entries)
        counts = Counter(e.get("verdict") for e in entries)
        pct = {
            v: round(100 * counts.get(v, 0) / total) if total else 0
            for v in ("still_works", "fixed", "could_not_fix", "not_runnable")
        }
        causes = Counter(c for e in entries for c in e.get("categories", [])).most_common()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "entries": entries,
                "total": total,
                "pct": pct,
                "causes": causes,
                "breaks": break_stats(entries),
                "generated_at": data.get("generated_at"),
            },
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/admin/ledger")
    async def admin_ledger(request: Request, token: str = ""):
        d = D(request)
        if not d.settings.admin_token or not secrets.compare_digest(token, d.settings.admin_token):
            return JSONResponse({"error": "not found"}, status_code=404)
        t = d.ledger.totals()
        ok, why = d.ledger.public_runs_open()
        return {
            "lifetime_usd": round(t.lifetime_usd, 4),
            "today_public_usd": round(t.today_public_usd, 4),
            "lifetime_tavily_credits": t.lifetime_credits,
            "public_runs_open": ok,
            "reason": why,
            "caps": {
                "lifetime": d.settings.cap_lifetime_usd,
                "daily": d.settings.cap_daily_usd,
                "run": d.settings.cap_run_usd,
                "public": d.ledger.public_cap_usd(),
            },
            "rate_limit_key": client_ip(request),  # behind a proxy this must be the visitor's own address
            "reserve": {
                "usd": d.settings.demo_reserve_usd,
                "until": d.settings.demo_reserve_until.isoformat(),
                "held": d.ledger.public_cap_usd() < d.settings.cap_lifetime_usd,
            },
        }

    return app


app = create_app()
