"""Nemotron Ultra proposes candidate fixes; candidates race in parallel sandboxes from the last good checkpoint."""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable

from .executor import Trace, group_units, unit_index_of
from .models import (
    Candidate,
    Edit,
    Evidence,
    FailureSignature,
    RepairProposal,
    RepairRecord,
    RoundRecord,
    RunPlan,
    Step,
)
from .judge import code_lines
from .llm import LLMOutputError
from .planner import default_probe, is_data_file, safe_path
from .prompts import REPAIR_SYSTEM
from .triage import failure_output, key_line

STRATEGY_RANK = {"migrate": 0, "pin": 1, "workaround": 2}
UNCITED_OK = {"tutorial_typo", "timeout", "environment"}
# Ways a "fix" can make a step pass without making it work.
HIDE_RES = [re.compile(r"\|\|\s*(?:true\b|:(?=\s|$)|exit\s+0\b)", re.M), re.compile(r";\s*exit\s+0\b"),
            re.compile(r"except[^\n:]*:\s*pass\b")]
Emit = Callable[[dict], Awaitable[None]]
# Event-loop time when the current run must end (set by the pipeline for each run).
run_deadline: ContextVar[float | None] = ContextVar("run_deadline", default=None)


def apply_edits(plan: RunPlan, edits: list[Edit]) -> RunPlan:
    steps = [s.model_copy(deep=True) for s in plan.steps]
    for e in edits:
        idx = next((i for i, s in enumerate(steps) if s.id == e.step_id), None)
        if idx is None:
            raise ValueError(f"Edit targets unknown step {e.step_id!r}")
        if e.op == "replace":
            s = steps[idx]
            kind = e.new_kind or s.kind  # a fix may change the kind, e.g. a blocking command into a server step
            path = (safe_path(e.new_path) or s.path or "file.txt") if kind == "write_file" else None
            probe = None
            if kind == "serve":
                probe = s.probe if s.kind == "serve" and s.probe else default_probe(e.new_content, plan.language)
            steps[idx] = s.model_copy(
                update={
                    "content": e.new_content,
                    "kind": kind,
                    "path": path,
                    "probe": probe,
                    "fix_reason": "; ".join(x for x in (s.fix_reason, e.reason) if x),
                    "fix_evidence_ids": s.fix_evidence_ids
                    + [i for i in e.evidence_ids if i not in s.fix_evidence_ids],
                }
            )
        else:
            n = sum(1 for s in steps if s.id.startswith(f"{e.step_id}_fix")) + 1
            kind = e.new_kind or "shell"
            steps.insert(
                idx + 1,
                Step(
                    id=f"{e.step_id}_fix{n}",
                    title="Added by Fix My Tutorial",
                    kind=kind,
                    content=e.new_content,
                    path=(safe_path(e.new_path) or f"added_{n}.txt") if kind == "write_file" else None,
                    implicit=True,
                    fix_reason=e.reason,
                    fix_evidence_ids=list(e.evidence_ids),
                ),
            )
    return plan.model_copy(update={"steps": steps})


INLINE_DATA = re.compile(r"[A-Za-z0-9+/]{300,}=*")


def validate_candidate(c: Candidate, plan: RunPlan, evidence_ids: set[str], category: str) -> str | None:
    if not c.edits:
        return "no edits"
    by_id = {s.id: s for s in plan.steps}
    for e in c.edits:
        if e.step_id not in by_id:
            return f"unknown step {e.step_id}"
        if not e.new_content.strip():
            return "empty edit"
        if evidence_ids and category not in UNCITED_OK and not any(i in evidence_ids for i in e.evidence_ids):
            return "edit has no citation"
        old = by_id[e.step_id].content if e.op == "replace" else ""
        if e.op == "replace" and _same_code(e.new_content, old) and e.new_kind in (None, by_id[e.step_id].kind):
            return "edit changes nothing"
        if _hidden_failures(e.new_content) > _hidden_failures(old):
            return "edit hides failures"
        if set(INLINE_DATA.findall(e.new_content)) - set(INLINE_DATA.findall(old)):
            return "edit pastes inline data"  # a base64 image or file copied from somewhere is not the tutorial's
        target = by_id[e.step_id]
        kind = e.new_kind or (target.kind if e.op == "replace" else None)
        path = e.new_path or (target.path if e.op == "replace" else None)
        own_file = e.op == "replace" and target.kind == "write_file" and target.path == path
        if kind == "write_file" and is_data_file(path) and not own_file:
            return "edit invents a data file"  # a fix must never stand in for data the tutorial can't get
        old_lines = [ln for ln in old.splitlines() if ln.strip()]
        if len(old_lines) >= 4 and len([ln for ln in e.new_content.splitlines() if ln.strip()]) < len(old_lines) / 2:
            return "edit removes most of the step"
        if e.op == "replace" and _deleted_code(old, e.new_content) >= 5:
            return "edit deletes the tutorial's code"  # a migration swaps lines; it doesn't drop a block of them
    return None


def _deleted_code(old: str, new: str) -> int:
    """Code lines an edit drops without putting anything in their place (comments and blank lines don't count)."""
    def code(text: str) -> list[str]:
        return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("#", "//"))]
    ops = SequenceMatcher(None, code(old), code(new), autojunk=False).get_opcodes()
    return sum(i2 - i1 for tag, i1, i2, _, _ in ops if tag in ("delete", "replace")) - sum(
        j2 - j1 for tag, _, _, j1, j2 in ops if tag in ("insert", "replace"))


def _same_code(a: str, b: str) -> bool:
    return code_lines(a) == code_lines(b)  # indentation counts: in Python it is the code


def _hidden_failures(text: str) -> int:
    return sum(len(rx.findall(text)) for rx in HIDE_RES)


def _sig(unit: list[Step]) -> list[tuple]:
    return [(s.id, s.kind, s.content, s.path) for s in unit]


def first_changed_unit(old: RunPlan, new: RunPlan) -> int:
    ou, nu = group_units(old), group_units(new)
    for i, (a, b) in enumerate(zip(ou, nu)):
        if _sig(a) != _sig(b):
            return i
    return min(len(ou), len(nu))


@dataclass
class RaceResult:
    candidate: Candidate
    plan: RunPlan
    trace: Trace | None
    passed: bool
    error: str


def pick(results: list[RaceResult]) -> RaceResult | None:
    ok = [r for r in results if r.passed]
    if not ok:
        return None
    return min(ok, key=lambda r: (STRATEGY_RANK[r.candidate.strategy], len(r.candidate.edits)))


def _render_context(
    plan: RunPlan,
    failing_step_id: str,
    output: str,
    sig: FailureSignature,
    evidence: list[Evidence],
    versions: str,
    attempts: list[dict],
) -> str:
    steps = "\n\n".join(
        f"[{s.id}] kind={s.kind}{' path=' + s.path if s.path else ''}{'  <-- FAILING' if s.id == failing_step_id else ''}"
        f"\n{s.content[:1500]}"
        for s in plan.steps
    )
    ev = "\n\n".join(f"[{e.id}] {e.title} ({e.url})\n{e.quote}" for e in evidence) or "(no evidence found)"
    tried = (
        "\n".join(f"- {a.get('strategy')}: {a.get('edits')} -> {a.get('error')}" for a in attempts)
        or "(none)"
    )
    return (
        f"Language: {plan.language} {plan.runtime}\n\n=== RUN PLAN ===\n{steps}\n\n"
        f"=== ERROR OUTPUT (last 80 lines) ===\n"
        + "\n".join(output.splitlines()[-80:])
        + f"\n\n=== FAILURE SIGNATURE ===\n{sig.model_dump_json()}\n\n=== INSTALLED VERSIONS ===\n{versions[:3000]}"
        f"\n\n=== EVIDENCE ===\n{ev}\n\n=== PREVIOUS FAILED ATTEMPTS ===\n{tried}"
    )


class Repairer:
    def __init__(self, llm, executor, settings):
        self.llm = llm
        self.executor = executor
        self.s = settings

    async def propose(
        self, *, plan, failing_step_id, output, signature, evidence, versions, attempts, run_id, public
    ) -> list[Candidate]:
        user = _render_context(plan, failing_step_id, output, signature, evidence, versions, attempts)
        try:
            prop = await self.llm.call(
                "repair",
                REPAIR_SYSTEM,
                user,
                RepairProposal,
                run_id=run_id,
                public=public,
                max_tokens=16000,  # the model reasons before answering; 9,000 ran out mid-answer on long tutorials
                temperature=0.3,
            )
        except LLMOutputError:  # a cut-off or malformed answer costs this round, not the whole run
            attempts.append({"strategy": "(none)", "edits": [],
                             "error": "your previous answer was cut off or was not valid JSON: send fewer, shorter edits"})
            return []
        ids = {e.id for e in evidence}
        valid = []
        for c in prop.candidates[: self.s.candidates_per_round]:
            why = validate_candidate(c, plan, ids, signature.category)
            if why is None:
                valid.append(c)
            else:  # tell the next round why, so it does not repeat the mistake
                attempts.append(
                    {
                        "strategy": c.strategy,
                        "edits": [f"{e.op} {e.step_id}: {e.new_content[:300]}" for e in c.edits],
                        "error": f"rejected before running: {why}",
                    }
                )
        return valid

    async def race(self, *, plan, trace, candidates, failing_step_id, run_id, public) -> list[RaceResult]:
        async def one(c: Candidate) -> RaceResult:
            try:
                cplan = apply_edits(plan, c.edits)
            except ValueError as e:
                return RaceResult(c, plan, None, False, str(e))
            start = min(first_changed_unit(plan, cplan), trace.failed_unit)
            target = unit_index_of(cplan, failing_step_id)
            t = await self.executor.execute(
                cplan,
                trace.states,
                from_unit=start,
                stop_after=target,
                run_id=run_id,
                public=public,
                quiet=True,
            )
            # The fix is judged on its own step: a later cell of the same group breaking for another reason is the
            # next repair, not a reason to reject this one (the final clean replay still has to pass).
            if t.failed_unit is None or (failing_step_id in t.results and t.failing_step_id != failing_step_id):
                return RaceResult(c, cplan, t, True, "")
            r = t.results.get(t.failing_step_id)
            return RaceResult(c, cplan, t, False, key_line(failure_output(r)) if r else t.reason)

        return list(await asyncio.gather(*(one(c) for c in candidates)))

    async def repair_step(
        self, *, plan, trace, signature, evidence, versions, run_id, public, emit: Emit
    ) -> tuple[RepairRecord, RaceResult | None]:
        failing = trace.failing_step_id
        record = RepairRecord(step_id=failing, signature=signature, evidence=evidence)
        output = failure_output(trace.results[failing])
        attempts: list[dict] = []
        for rnd in range(1, self.s.repair_rounds + 1):
            deadline = run_deadline.get()
            if deadline is not None and asyncio.get_running_loop().time() + self.s.step_timeout_s + 60 > deadline:
                record.stopped = "time"  # end as "could not fix" rather than be cut off mid-round
                break
            cands = await self.propose(
                plan=plan,
                failing_step_id=failing,
                output=output,
                signature=signature,
                evidence=evidence,
                versions=versions,
                attempts=attempts,
                run_id=run_id,
                public=public,
            )
            rr = RoundRecord(round=rnd, candidates=cands)
            await emit(
                {
                    "type": "candidates",
                    "step_id": failing,
                    "round": rnd,
                    "candidates": [c.model_dump() for c in cands],
                }
            )
            if not cands:
                record.rounds.append(rr)
                attempts.append(
                    {"strategy": "(none)", "edits": [], "error": "no valid candidates were proposed"}
                )
                continue
            results = await self.race(
                plan=plan,
                trace=trace,
                candidates=cands,
                failing_step_id=failing,
                run_id=run_id,
                public=public,
            )
            rr.passed = [r.passed for r in results]
            rr.errors = [r.error for r in results]
            for i, r in enumerate(results):
                await emit(
                    {
                        "type": "candidate_result",
                        "step_id": failing,
                        "round": rnd,
                        "index": i,
                        "passed": r.passed,
                        "error": r.error,
                    }
                )
            win = pick(results)
            if win is not None:
                rr.winner_index = results.index(win)
                await emit({"type": "winner", "step_id": failing, "round": rnd, "index": rr.winner_index})
                record.rounds.append(rr)
                record.winner = win.candidate
                return record, win
            record.rounds.append(rr)
            attempts += [
                {
                    "strategy": r.candidate.strategy,
                    "edits": [f"{e.op} {e.step_id}: {e.new_content[:300]}" for e in r.candidate.edits],
                    "error": r.error,
                }
                for r in results
            ]
        return record, None
