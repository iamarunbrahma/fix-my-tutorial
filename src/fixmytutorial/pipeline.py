"""One run, end to end: ingest, plan, execute, then triage, research and repair until clean, then replay."""
from __future__ import annotations

import asyncio
import json
import logging

from .breakage import BreakTarget, Registry, eligible, find_break, make_probe
from .executor import ExecState, Executor, Trace, unit_index_of
from .ingest import IngestError, Source, ingest
from .judge import Judge, code_lines, squash
from .ledger import BudgetExceeded
from .llm import LLMOutputError
from .models import RunPlan, RunRecord, RunStatus, Step, StepOutcome, StepStatus, Summary, Verdict, utcnow
from .planner import plan_tutorial
from .prompts import SUMMARY_SYSTEM
from .repair import Repairer, run_deadline
from .report import broke_in_text, partial_note
from .research import research
from .sandbox import PRELUDE
from .triage import triage

log = logging.getLogger("fixmytutorial.pipeline")


def step_view(s: Step) -> dict:
    return {"id": s.id, "title": s.title, "kind": s.kind, "implicit": s.implicit, "content": s.content[:2000],
            "path": s.path}


def _contains(block: list[tuple[int, str]], lines: list[tuple[int, str]]) -> bool:
    """The lines appear in a row in the block, indented the same relative to each other."""
    def rel(seq: list[tuple[int, str]]) -> list[tuple[int, str]]:
        return [(i - seq[0][0], t) for i, t in seq]
    n = len(lines)
    return n == 0 or any(rel(block[k:k + n]) == rel(lines) for k in range(len(block) - n + 1))


class Pipeline:
    def __init__(self, *, settings, llm, search, backend, store, ledger, bus, snapshot=None, registry=None):
        self.s = settings
        self.llm = llm
        self.search = search
        self.backend = backend
        self.store = store
        self.ledger = ledger
        self.bus = bus
        self.snapshot = snapshot
        self.judge = Judge(llm)
        self.registry = registry or Registry()
        self._break_targets: dict[str, BreakTarget] = {}  # run id -> the first failure worth dating

    async def run(self, record: RunRecord, source: Source) -> RunRecord:
        async def emit(ev: dict) -> None:
            if ev.get("type") == "step" and ev.get("phase", "run") == "run" and ev.get("status") != "running":
                prev = record.outcomes.get(ev["step_id"])
                record.outcomes[ev["step_id"]] = StepOutcome(
                    step_id=ev["step_id"], status=StepStatus(ev["status"]),
                    stdout_tail=ev.get("output", prev.stdout_tail if prev else ""),
                    duration_s=ev.get("duration_s", prev.duration_s if prev else 0.0))
            await self.bus.publish(record.id, ev)

        executor = Executor(self.backend, self.s, self.judge, emit)
        run_deadline.set(asyncio.get_running_loop().time() + self.s.run_wall_clock_s)
        try:
            async with asyncio.timeout(self.s.run_wall_clock_s):
                await self._run(record, source, emit, executor)
                await self._date_break(record, emit, executor)  # Could not fix ends before the summary
        except (BudgetExceeded, IngestError) as e:
            record.verdict, record.reason = Verdict.ERROR, str(e)
        except TimeoutError:
            minutes = max(1, int(self.s.run_wall_clock_s // 60))
            record.verdict = Verdict.ERROR
            record.reason = f"This run took longer than {minutes} minute{'s' if minutes != 1 else ''} and was stopped."
        except LLMOutputError:
            record.verdict, record.reason = Verdict.ERROR, "The model returned an unusable answer. Please try again."
        except Exception:
            log.exception("run %s failed", record.id)
            record.verdict, record.reason = Verdict.ERROR, "Something went wrong on our side. Please try again."
        finally:
            self._break_targets.pop(record.id, None)
            if record.verdict is None:
                record.verdict = Verdict.ERROR
            totals = self.ledger.totals(record.id)
            record.cost_usd = round(totals.run_usd, 4)
            record.tavily_credits = totals.run_credits
            record.status = RunStatus.FAILED if record.verdict == Verdict.ERROR else RunStatus.DONE
            record.finished_at = utcnow()
            self.store.save_run(record)
            await self.bus.publish(record.id, {"type": "verdict", "verdict": record.verdict.value,
                                               "reason": record.reason, "summary": record.summary,
                                               "cost_usd": record.cost_usd, "note": partial_note(record)})
            await self.bus.publish(record.id, {"type": "done"})
            if self.snapshot is not None:
                await asyncio.to_thread(self.snapshot.save_run, record)
        return record

    async def _stage(self, record: RunRecord, status: RunStatus, emit) -> None:
        record.status = status
        self.store.save_run(record)
        await emit({"type": "stage", "stage": status.value})

    async def _run(self, record: RunRecord, source: Source, emit, executor: Executor) -> None:
        rid, pub = record.id, record.public
        await self._stage(record, RunStatus.INGESTING, emit)
        doc = await ingest(source, self.search, run_id=rid, public=pub, max_chars=self.s.max_tutorial_chars)
        record.title, record.content_hash, record.published = doc.title, doc.content_hash, doc.published
        await self._stage(record, RunStatus.PLANNING, emit)
        plan = await plan_tutorial(doc, self.llm, self.s, run_id=rid, public=pub)
        record.plan = plan
        await self._emit_plan(plan, record, emit)
        if not plan.runnable:
            record.verdict = Verdict.NOT_RUNNABLE
            record.reason = plan.not_runnable_reason or "This tutorial can't run in our sandbox."
            return
        base = [ExecState(await self.backend.base_image(plan.language))]
        await self._stage(record, RunStatus.EXECUTING, emit)
        trace = await executor.execute(plan, base, run_id=rid, public=pub)
        plan = await self._repair_until_clean(record, plan, trace, executor, emit, self.s.max_repaired_steps)
        if plan is None:
            return
        if not record.repairs:
            record.verdict, record.final_plan = Verdict.STILL_WORKS, plan
        else:
            await self._stage(record, RunStatus.REPLAYING, emit)
            replay = await executor.execute(plan, base, run_id=rid, public=pub, phase="replay")
            if replay.failed_unit is not None:
                plan = await self._repair_until_clean(record, plan, replay, executor, emit, 1)
                if plan is None:
                    return
                replay = await executor.execute(plan, base, run_id=rid, public=pub, phase="replay")
            record.final_plan = plan
            if replay.failed_unit is None and self._only_restores_the_page(record, plan, doc):
                # the "fix" just undid our own misreading of the page: the tutorial works as written
                record.verdict, record.repairs = Verdict.STILL_WORKS, []
            elif replay.failed_unit is None:
                record.verdict = Verdict.FIXED
            else:
                record.verdict = Verdict.COULD_NOT_FIX
                record.reason = f"The repaired tutorial did not pass a clean re-run (step {replay.failing_step_id})."
        await self._date_break(record, emit, executor)  # before the summary, so it can say which release broke it
        record.summary = await self._summary(record)

    @staticmethod
    def _only_restores_the_page(record: RunRecord, plan: RunPlan, doc) -> bool:
        original = {s.id: (s.content, s.kind, s.path) for s in record.plan.steps} if record.plan else {}
        changed = [s for s in plan.steps if original.get(s.id) != (s.content, s.kind, s.path)]
        page = squash("\n".join([doc.text, *(b.code for b in doc.code_blocks)]))
        blocks = [code_lines(b.code) for b in doc.code_blocks]

        def shown(content: str) -> bool:
            if squash(content) not in page:
                return False
            mine = code_lines(content)  # where a code block shows it, the indentation must match as well
            hits = [b for b in blocks if squash(content) in squash(" ".join(t for _, t in b))]
            return not hits or any(_contains(b, mine) for b in hits)

        return bool(changed) and all(shown(s.content) for s in changed)

    async def _emit_plan(self, plan: RunPlan, record: RunRecord, emit) -> None:
        await emit({"type": "plan", "title": record.title, "language": plan.language, "runtime": plan.runtime,
                    "note": plan.note, "steps": [step_view(s) for s in plan.steps]})

    async def _repair_until_clean(self, record: RunRecord, plan: RunPlan, trace: Trace, executor: Executor, emit,
                                  limit: int) -> RunPlan | None:
        rid, pub = record.id, record.public
        repaired = 0
        retried: set[str] = set()
        while trace.failed_unit is not None:
            failing = trace.failing_step_id
            first = trace.results.get(failing)
            if failing not in retried and first is not None and not first.timed_out:
                retried.add(failing)  # a flaky download or install gets one more try before anything is "fixed"
                trace = await executor.execute(plan, trace.states, from_unit=trace.failed_unit, run_id=rid,
                                               public=pub)
                continue
            if repaired >= limit:
                record.verdict, record.final_plan = Verdict.COULD_NOT_FIX, plan
                record.reason = f"Too many steps needed repairs (limit {self.s.max_repaired_steps})."
                return None
            await self._stage(record, RunStatus.REPAIRING, emit)
            await emit({"type": "step", "step_id": failing, "status": "repairing", "phase": "run"})
            step = next(s for s in plan.steps if s.id == failing)
            try:
                sig = await triage(step, trace.results[failing], language=plan.language, llm=self.llm, run_id=rid,
                                   public=pub)
                await emit({"type": "diagnosis", "step_id": failing, "category": sig.category,
                            "key_line": sig.key_line, "package": sig.package, "symbol": sig.symbol})
                evidence = await research(sig, search=self.search, run_id=rid, public=pub,
                                          id_prefix=f"r{len(record.repairs) + 1}e")
                await emit({"type": "evidence", "step_id": failing, "items": [e.model_dump() for e in evidence]})
                versions = await self._versions(plan.language, trace.states[trace.failed_unit])
                if record.id not in self._break_targets and eligible(plan, trace, sig):
                    self._break_targets[record.id] = BreakTarget(plan, trace, failing, sig, evidence, versions)
                rec, win = await Repairer(self.llm, executor, self.s).repair_step(
                    plan=plan, trace=trace, signature=sig, evidence=evidence, versions=versions, run_id=rid,
                    public=pub, emit=emit)
            except BaseException:  # budget, wall clock or model error: don't leave the step spinning on the page
                await emit({"type": "step", "step_id": failing, "status": "failed", "phase": "run"})
                raise
            record.repairs.append(rec)
            if win is None:
                await emit({"type": "step", "step_id": failing, "status": "failed", "phase": "run"})
                record.verdict, record.final_plan = Verdict.COULD_NOT_FIX, plan
                record.reason = (f"We ran out of time to try more fixes for step {failing}." if rec.stopped == "time"
                                 else f"Couldn't repair step {failing}: {sig.key_line}")
                return None
            repaired += 1
            before = {s.id: s.content for s in plan.steps}
            plan = win.plan
            await self._emit_plan(plan, record, emit)
            edited = {s.id for s in plan.steps if before.get(s.id) != s.content}
            for sid, r in win.trace.results.items():
                status = ("failed" if sid == win.trace.failing_step_id
                          else "repaired" if sid == failing or sid in edited else "passed")
                await emit({"type": "step", "step_id": sid, "status": status, "phase": "run",
                            "output": (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-4000:],
                            "duration_s": r.duration_s})
            await self._stage(record, RunStatus.EXECUTING, emit)
            if win.trace.failed_unit is not None:  # the fix worked; a later cell of the same group broke next
                trace = win.trace
            else:
                trace = await executor.execute(plan, win.trace.states, from_unit=unit_index_of(plan, failing) + 1,
                                               run_id=rid, public=pub)
        return plan

    async def _date_break(self, record: RunRecord, emit, executor: Executor) -> None:
        """Which package release broke the tutorial. Best effort, and it never changes the verdict."""
        target = self._break_targets.pop(record.id, None)
        if target is None or not self.s.break_finder or record.verdict not in (Verdict.FIXED, Verdict.COULD_NOT_FIX):
            return
        deadline = run_deadline.get()
        left = self.s.break_finder_s if deadline is None else deadline - asyncio.get_running_loop().time() - 45
        time_s = min(self.s.break_finder_s, left)
        if time_s < 30:
            return
        await emit({"type": "stage", "stage": "dating"})
        lang, rid, pub = target.plan.language, record.id, record.public
        record.broke_in = await find_break(
            target, language=lang, llm=self.llm, registry=self.registry,
            probe_for=lambda pkg: make_probe(executor, target, lang, pkg, run_id=rid, public=pub),
            max_probes=self.s.break_max_probes, time_s=time_s, run_id=rid, public=pub, search=self.search)
        if record.broke_in is not None:
            await emit({"type": "broke_in", **record.broke_in.model_dump(mode="json"), "text": broke_in_text(record)})

    async def _versions(self, language: str, state: ExecState) -> str:
        cmd = ("pip list --format=freeze 2>/dev/null | head -150" if language == "python"
               else "node --version; npm ls --depth=0 2>/dev/null | head -100")
        try:
            r = await self.backend.run(state.image, f"{PRELUDE}\n{cmd}\n", timeout_s=60, save=False)
            return r.stdout[:4000]
        except Exception:
            return ""

    async def _summary(self, record: RunRecord) -> str:
        plan = record.final_plan or record.plan
        facts = {"title": record.title, "verdict": record.verdict.value,
                 "language": plan.language if plan else None, "runtime": plan.runtime if plan else None,
                 "repairs": [{"step": r.step_id, "error": r.signature.key_line, "category": r.signature.category,
                              "fix": r.winner.rationale if r.winner else None,
                              "strategy": r.winner.strategy if r.winner else None,
                              "evidence": [f"{e.title}: {e.quote[:300]}" for e in r.evidence[:3]]}
                             for r in record.repairs],
                 "broke_in": broke_in_text(record) or None}
        try:
            out = await self.llm.call("summary", SUMMARY_SYSTEM, json.dumps(facts), Summary, run_id=record.id,
                                      public=record.public, max_tokens=400)
            return out.summary
        except Exception:
            return ""
