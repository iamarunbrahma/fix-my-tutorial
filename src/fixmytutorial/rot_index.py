"""Rot Index: discover candidate tutorials, run them in batch, export the public board data.

  python -m fixmytutorial.rot_index discover   # Tavily searches -> rot_index/candidates.yaml (for hand curation)
  python -m fixmytutorial.rot_index run [--force] [--concurrency 4] [--limit N]
  python -m fixmytutorial.rot_index export     # -> rot_index/results.json
  python -m fixmytutorial.rot_index sync [--bootstrap]  # upload local runs and events (never the ledger)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from datetime import date
from pathlib import Path

import yaml

from .config import get_settings
from .events import EventBus
from .ingest import Source
from .ledger import Ledger
from .llm import LLM
from .models import RunRecord, RunStatus, Verdict, utcnow
from .pipeline import Pipeline
from .report import build_changes
from .sandbox import make_backend
from .search import SearchClient
from .snapshot import Snapshotter
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / "rot_index" / "list.yaml"
CANDIDATES = ROOT / "rot_index" / "candidates.yaml"
RESULTS = ROOT / "rot_index" / "results.json"
TUTORIAL_DOMAINS = [
    "realpython.com", "freecodecamp.org", "digitalocean.com", "geeksforgeeks.org", "medium.com", "dev.to",
    "towardsdatascience.com", "machinelearningmastery.com", "stackabuse.com", "datacamp.com", "analyticsvidhya.com",
    "blog.logrocket.com", "sitepoint.com", "programiz.com", "w3schools.com", "kdnuggets.com", "pythonbasics.org",
    "javascripttutorial.net", "twilio.com", "auth0.com", "hackernoon.com",
]
DISCOVER_QUERIES = [
    "pandas DataFrame append rows tutorial", "scikit-learn linear regression boston housing tutorial",
    "openai python ChatCompletion tutorial", "langchain getting started OpenAI tutorial",
    "flask before_first_request tutorial", "matplotlib style seaborn plot tutorial", "pandas iteritems loop tutorial",
    "OneHotEncoder sparse tutorial", "numpy dtype float tutorial", "pandas read_csv error_bad_lines tutorial",
    "seaborn distplot tutorial", "keras fit_generator tutorial", "python asyncio get_event_loop tutorial",
    "beautifulsoup requests web scraping tutorial", "fastapi beginner tutorial", "flask hello world tutorial",
    "python requests api tutorial", "pandas groupby tutorial", "matplotlib bar chart tutorial",
    "scikit-learn train_test_split tutorial", "express hello world tutorial", "express 404 page tutorial",
    "node-fetch tutorial", "faker js fake data tutorial", "chalk node colors tutorial", "express body-parser tutorial",
    "axios node tutorial", "node.js read file tutorial", "jest testing node tutorial", "dotenv node tutorial",
]


def export_entries(entries: list[dict], store: Store) -> list[dict]:
    latest: dict[str, RunRecord] = {}
    for rec in store.list_runs(limit=5000):  # newest first
        if not rec.public and rec.status in (RunStatus.DONE, RunStatus.FAILED):
            latest.setdefault(rec.source, rec)
    out = []
    for e in entries:
        rec = latest.get(e["url"])
        if rec is None or rec.verdict in (None, Verdict.ERROR):
            continue
        verdict = rec.verdict.value
        if e.get("review") == "downgrade" and verdict == "fixed":
            verdict = "could_not_fix"
        b = rec.broke_in
        out.append({"url": e["url"], "title": e.get("title") or rec.title, "language": e["language"],
                    "source": e["source"], "year": e["year"] or (int(rec.published[:4]) if rec.published else ""),
                    "topic": e.get("topic", ""), "verdict": verdict,
                    "categories": sorted({r.signature.category for r in rec.repairs}),
                    "changes": len(build_changes(rec)), "run_id": rec.id, "featured": bool(e.get("featured")),
                    "published": rec.published,
                    "broke_in": {"package": b.package, "version": b.first_bad, "date": b.first_bad_date.isoformat(),
                                 "how": b.how} if b else None})
    return out


def _core(settings):
    store = Store(settings.db_path)
    ledger = Ledger(settings.ledger_db_path, settings)
    return store, ledger


async def discover() -> None:
    s = get_settings()
    store, ledger = _core(s)
    search = SearchClient(s, ledger, store)
    seen: dict[str, dict] = {}
    for i, q in enumerate(DISCOVER_QUERIES):
        for r in await search.search(q, run_id=f"rot-discover-{i}", public=False, max_results=8,
                                     include_domains=TUTORIAL_DOMAINS):
            seen.setdefault(r["url"], {"url": r["url"], "title": r["title"], "query": q})
    CANDIDATES.write_text(yaml.safe_dump({"candidates": list(seen.values())}, sort_keys=False, allow_unicode=True))
    print(f"wrote {len(seen)} candidates to {CANDIDATES}")


def check_batch_backend(settings) -> None:
    """Rot Index tutorials are third-party code: never run them on a Docker network allowed to reach private
    addresses. Contree (Token Factory Sandboxes) or a firewalled Docker network only."""
    if settings.sandbox_backend == "docker" and settings.docker_allow_private_egress:
        raise SystemExit("Refusing: DOCKER_ALLOW_PRIVATE_EGRESS is for trusted fixtures only. Use "
                         "SANDBOX_BACKEND=contree or a firewalled Docker network for the Rot Index.")


def pick_entries(entries: list[dict], done: set[str], force: bool, limit: int | None) -> list[dict]:
    """Tutorials still to run, taken round-robin across topics, so a short slice covers many kinds of breakage."""
    by_topic: dict[str, list[dict]] = {}
    for e in entries:
        if force or e["url"] not in done:
            by_topic.setdefault(e.get("topic", ""), []).append(e)
    queues, out = list(by_topic.values()), []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out if limit is None else out[:limit]


async def run_batch(force: bool, concurrency: int, limit: int | None = None) -> None:
    s = get_settings()
    check_batch_backend(s)
    store, ledger = _core(s)
    bus = EventBus(store)
    backend = make_backend(s)
    # Runs the sandbox safety probe once, up front: an unsafe sandbox stops the batch before any tutorial is
    # downloaded or planned, instead of after every one of them has been paid for.
    await backend.base_image("python")
    pipeline = Pipeline(settings=s, llm=LLM(s, ledger), search=SearchClient(s, ledger, store),
                        backend=backend, store=store, ledger=ledger, bus=bus)
    entries = yaml.safe_load(LIST.read_text())["tutorials"]
    done = {r.source for r in store.list_runs(limit=5000)
            if not r.public and r.status == RunStatus.DONE}
    sem = asyncio.Semaphore(concurrency)
    todo = pick_entries(entries, done, force, limit)
    print(f"running {len(todo)} of {len(entries)} tutorials")

    async def one(e: dict) -> None:
        async with sem:
            rec = RunRecord(id="ri-" + secrets.token_urlsafe(6), source=e["url"], public=False, created_at=utcnow())
            store.save_run(rec)
            rec = await pipeline.run(rec, Source(url=e["url"]))
            print(f"{rec.verdict.value:14} ${rec.cost_usd:.3f}  {e['url']}")

    await asyncio.gather(*(one(e) for e in todo))
    t = ledger.totals()
    print(f"lifetime model spend: ${t.lifetime_usd:.2f}; tavily credits: {t.lifetime_credits}")


def export() -> None:
    s = get_settings()
    store, _ = _core(s)
    entries = yaml.safe_load(LIST.read_text())["tutorials"]
    out = export_entries(entries, store)
    RESULTS.write_text(json.dumps({"generated_at": date.today().isoformat(), "entries": out}, indent=1))
    print(f"exported {len(out)} entries to {RESULTS}")


def sync(bootstrap: bool = False) -> None:
    s = get_settings()
    store, ledger = _core(s)
    ok = Snapshotter(s, store, ledger).save_all([r for r in store.list_runs(limit=5000) if not r.public],
                                                bootstrap=bootstrap)
    if not ok:
        raise SystemExit("Sync failed: the Hub dataset could not be reached.")
    print("synced local runs to", s.hf_dataset_repo, "(the Space keeps its own spend ledger)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["discover", "run", "export", "sync"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="run at most N tutorials (round-robin across topics)")
    a = ap.parse_args()
    if a.command == "discover":
        asyncio.run(discover())
    elif a.command == "run":
        asyncio.run(run_batch(a.force, a.concurrency, a.limit))
    elif a.command == "export":
        export()
    else:
        sync(a.bootstrap)


if __name__ == "__main__":
    main()
