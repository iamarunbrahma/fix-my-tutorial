"""Run every fixture tutorial through the real pipeline (real Nemotron, Tavily and sandbox) and score it.

Usage: python -m fixmytutorial.eval [--only NAME ...] [--concurrency 3]
Exit code 1 if a still-working control gets changed, or fewer than 10 rot fixtures reach "fixed".
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import get_settings
from .events import EventBus
from .ingest import Source
from .ledger import Ledger
from .llm import LLM
from .models import RunRecord, utcnow
from .pipeline import Pipeline
from .sandbox import make_backend
from .search import SearchClient
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "tutorials"


@dataclass
class EvalRow:
    name: str
    expected: str
    verdict: str
    category: str
    rounds: int
    seconds: float
    cost_usd: float
    credits: int
    reason: str


def load_expected() -> dict[str, dict]:
    return yaml.safe_load((FIXTURES / "expected.yaml").read_text())


def score(rows: list[EvalRow]) -> tuple[bool, str]:
    changed_controls = [r for r in rows if r.expected == "still_works" and r.verdict != "still_works"]
    rot = [r for r in rows if r.expected == "fixed"]
    fixed = sum(1 for r in rot if r.verdict == "fixed")
    ok = not changed_controls and fixed >= min(10, len(rot))
    return ok, f"fixed {fixed}/{len(rot)} rot fixtures; {len(changed_controls)} control(s) changed"


def table(rows: list[EvalRow]) -> str:
    lines = ["| fixture | expected | verdict | category | rounds | seconds | cost $ | tavily | note |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.name} | {r.expected} | {r.verdict} | {r.category} | {r.rounds} | {r.seconds:.0f} | "
                     f"{r.cost_usd:.4f} | {r.credits} | {r.reason[:80]} |")
    return "\n".join(lines)


async def run_all(names: list[str], concurrency: int) -> list[EvalRow]:
    s = get_settings()
    data = s.data_dir / "eval"
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "eval.sqlite3")
    ledger = Ledger(s.ledger_db_path, s)
    bus = EventBus(store)
    pipeline = Pipeline(settings=s, llm=LLM(s, ledger), search=SearchClient(s, ledger, store),
                        backend=make_backend(s), store=store, ledger=ledger, bus=bus)
    expected = load_expected()
    sem = asyncio.Semaphore(concurrency)

    async def one(name: str) -> EvalRow:
        async with sem:
            text = (FIXTURES / f"{name}.md").read_text()
            rec = RunRecord(id=f"eval-{name}-{int(time.time())}", source=f"fixture:{name}", public=False,
                            created_at=utcnow())
            t0 = time.monotonic()
            rec = await pipeline.run(rec, Source(markdown=text))
            return EvalRow(name=name, expected=expected[name]["verdict"], verdict=rec.verdict.value,
                           category=rec.repairs[0].signature.category if rec.repairs else "",
                           rounds=sum(len(r.rounds) for r in rec.repairs), seconds=time.monotonic() - t0,
                           cost_usd=rec.cost_usd, credits=rec.tavily_credits, reason=rec.reason)

    return list(await asyncio.gather(*(one(n) for n in names)))


def write_report(report: str, root: Path = ROOT) -> Path:
    path = root / "docs" / "eval-results.md"
    path.parent.mkdir(parents=True, exist_ok=True)  # docs/ is local-only, so a fresh clone doesn't have it
    path.write_text(report)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    names = args.only or sorted(load_expected())
    rows = asyncio.run(run_all(names, args.concurrency))
    ok, msg = score(rows)
    report = f"# Evaluation results\n\n{table(rows)}\n\n**{msg}**\n"
    write_report(report)
    print(report)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
