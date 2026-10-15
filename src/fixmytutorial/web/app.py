from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from ..config import Settings, get_settings
from ..events import EventBus
from ..ingest import Source
from ..jobs import JobQueue, RateLimiter
from ..ledger import Ledger
from ..models import RunRecord, RunStatus, Verdict, utcnow
from ..report import VERDICT_TEXT, badge_svg, build_changes, diff_lines, og_png
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
    """Only the Space persists to the Hub dataset (and owns the remote ledger). A laptop server keeps everything
    local, so a test run can never upload runs or touch the Space's spend records."""
    if not settings.space_id:
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
        heal = None
        if d.snapshot is not None and getattr(d.snapshot, "enabled", False):
            restored = await asyncio.to_thread(d.snapshot.restore)
            log.info("restored %d runs from snapshot", restored)
            heal = asyncio.create_task(heal_loop(d))
        elif d.settings.space_id:  # without snapshots the Space would forget its spend on every restart
            d.ledger.mark_unverified(NOT_CONFIGURED)
        await recover_unfinished(d)
        app.state.deps = d
        yield
        if heal is not None:
            heal.cancel()

    app = FastAPI(title="Fix My Tutorial", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        verdict_label=verdict_label, cat_label=lambda c: CATEGORY_TEXT.get(c, c), diff_lines=diff_lines
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
        return rate_key(candidate) or rate_key(peer) or peer

    def home_ctx(request: Request, **extra) -> dict:
        d = D(request)
        ok, why = d.ledger.public_runs_open()
        featured = [
            dict(e, verdict_label=verdict_label(e.get("verdict")))
            for e in load_index(d.settings).get("entries", [])
            if e.get("featured") and e.get("run_id")
        ][:3]
        return {
            "paused": None if ok else why,
            "per_hour": d.settings.rate_limit_runs_per_hour,
            "examples": featured,
            "error": None,
            "url": "",
            "markdown": "",
            **extra,
        }

    def not_found(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "notfound.html", {}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request, "home.html", home_ctx(request))

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
        ok, _ = d.ledger.public_runs_open()
        if not ok:
            return templates.TemplateResponse(
                request, "home.html", home_ctx(request, url=url, markdown=markdown), status_code=503
            )
        if url:
            cached = d.store.find_recent_run(url, d.settings.cache_ttl_s)
            if cached is not None:
                return RedirectResponse(f"/run/{cached.id}", status_code=303)
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
        initial = rec.model_dump_json().replace("</", "<\\/")
        return templates.TemplateResponse(request, "run.html", {"rec": rec, "initial": initial})

    @app.get("/run/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = 0):
        d = D(request)
        if d.store.get_run(run_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        last = request.headers.get("last-event-id", "")
        start = int(last) if last.isdigit() else after

        async def gen():
            async for seq, ev in d.bus.subscribe(run_id, start):
                if await request.is_disconnected():
                    break
                if ev["type"] == "ping":
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"id": str(seq), "event": ev["type"], "data": json.dumps(ev)}

        return EventSourceResponse(gen(), ping=20)

    @app.get("/r/{run_id}", response_class=HTMLResponse)
    async def report(request: Request, run_id: str):
        d = D(request)
        rec = d.store.get_run(run_id)
        if rec is None:
            return not_found(request)
        if rec.status not in (RunStatus.DONE, RunStatus.FAILED):
            return RedirectResponse(f"/run/{run_id}", status_code=303)
        base = d.settings.public_base_url.rstrip("/")
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "rec": rec,
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
            },
        }

    return app


app = create_app()
