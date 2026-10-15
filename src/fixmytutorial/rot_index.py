"""Rot Index: discover candidate tutorials, run them in batch, export the public board data.

  python -m fixmytutorial.rot_index discover   # Tavily searches -> rot_index/candidates.yaml (for hand curation)
  python -m fixmytutorial.rot_index run [--force] [--concurrency 4]
  python -m fixmytutorial.rot_index export     # -> rot_index/results.json
  python -m fixmytutorial.rot_index sync [--bootstrap]  # merge + upload local runs and ledger (one commit)
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
DISCOVER_QUERIES = [
    "pandas tutorial DataFrame append rows loop", "scikit-learn linear regression tutorial load_boston",
    "openai python tutorial ChatCompletion.create", "langchain tutorial from langchain.llms import OpenAI",
    "flask tutorial before_first_request", "matplotlib tutorial plt.style.use seaborn",
    "pandas iteritems tutorial", "sklearn OneHotEncoder sparse=False tutorial", "numpy dtype np.float tutorial",
    "pandas read_csv error_bad_lines tutorial", "seaborn distplot tutorial", "keras fit_generator tutorial",
    "tensorflow tf.Session placeholder tutorial", "python asyncio get_event_loop run_until_complete tutorial",
    "beautifulsoup requests scraping tutorial 2020", "fastapi tutorial beginners 2021",
    "express js tutorial 404 page app.get('*')", "node-fetch require tutorial", "faker js require tutorial",
    "chalk node tutorial require", "express body-parser tutorial", "axios node tutorial 2020",
    "node.js read file tutorial callback", "jest testing tutorial node 2020", "dotenv node tutorial",
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
        out.append({"url": e["url"], "title": e.get("title") or rec.title, "language": e["language"],
                    "source": e["source"], "year": e["year"], "topic": e.get("topic", ""), "verdict": verdict,
                    "categories": sorted({r.signature.category for r in rec.repairs}),
                    "changes": len(build_changes(rec)), "run_id": rec.id, "featured": bool(e.get("featured"))})
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
        for r in await search.search(q, run_id=f"rot-discover-{i}", public=False, max_results=8):
            seen.setdefault(r["url"], {"url": r["url"], "title": r["title"], "query": q})
    CANDIDATES.write_text(yaml.safe_dump({"candidates": list(seen.values())}, sort_keys=False, allow_unicode=True))
    print(f"wrote {len(seen)} candidates to {CANDIDATES}")


def check_batch_backend(settings) -> None:
    """Rot Index tutorials are third-party code: never run them on a Docker network allowed to reach private
    addresses. Contree (Token Factory Sandboxes) or a firewalled Docker network only."""
    if settings.sandbox_backend == "docker" and settings.docker_allow_private_egress:
        raise SystemExit("Refusing: DOCKER_ALLOW_PRIVATE_EGRESS is for trusted fixtures only. Use "
                         "SANDBOX_BACKEND=contree or a firewalled Docker network for the Rot Index.")


async def run_batch(force: bool, concurrency: int) -> None:
    s = get_settings()
    check_batch_backend(s)
    store, ledger = _core(s)
    bus = EventBus(store)
    pipeline = Pipeline(settings=s, llm=LLM(s, ledger), search=SearchClient(s, ledger, store),
                        backend=make_backend(s), store=store, ledger=ledger, bus=bus)
    entries = yaml.safe_load(LIST.read_text())["tutorials"]
    done = {r.source for r in store.list_runs(limit=5000)
            if not r.public and r.status == RunStatus.DONE}
    sem = asyncio.Semaphore(concurrency)

    async def one(e: dict) -> None:
        if not force and e["url"] in done:
            return
        async with sem:
            rec = RunRecord(id="ri-" + secrets.token_urlsafe(6), source=e["url"], public=False, created_at=utcnow())
            store.save_run(rec)
            rec = await pipeline.run(rec, Source(url=e["url"]))
            print(f"{rec.verdict.value:14} ${rec.cost_usd:.3f}  {e['url']}")

    await asyncio.gather(*(one(e) for e in entries))
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
        raise SystemExit("Sync refused: the remote ledger could not be read and merged (use --bootstrap only for "
                         "the very first upload).")
    print("synced local runs and merged ledger to", s.hf_dataset_repo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["discover", "run", "export", "sync"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    if a.command == "discover":
        asyncio.run(discover())
    elif a.command == "run":
        asyncio.run(run_batch(a.force, a.concurrency))
    elif a.command == "export":
        export()
    else:
        sync(a.bootstrap)


if __name__ == "__main__":
    main()
