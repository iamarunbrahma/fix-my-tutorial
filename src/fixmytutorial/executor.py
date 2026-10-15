"""Run a RunPlan unit by unit in a sandbox, saving a checkpoint image after every unit.

A unit is one shell / write_file / serve step, or a group of consecutive Python run_code cells. Cells run through
/opt/fmt/run_cells.py in one process so variables flow between them; later cell groups re-run earlier cells
silently because sandbox processes do not survive between operations.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .models import ImageRef, RunPlan, Step, StepResult
from .sandbox import PRELUDE

Emit = Callable[[dict], Awaitable[None]]
MARK = "@@FMT "
INSTALL_RE = re.compile(r"\b(pip3?|uv pip|npm|npx|yarn|pnpm|apt-get|poetry|conda)\b\s+(install|i|add|ci|create)\b")
ESM_RE = re.compile(r"^\s*(import\s|export\s)", re.M)


def group_units(steps: list[Step]) -> list[list[Step]]:
    units: list[list[Step]] = []
    for s in steps:
        if s.kind == "run_code" and units and units[-1][0].kind == "run_code":
            units[-1].append(s)
        else:
            units.append([s])
    return units


def unit_index_of(plan: RunPlan, step_id: str) -> int:
    for i, unit in enumerate(group_units(plan.steps)):
        if any(s.id == step_id for s in unit):
            return i
    raise KeyError(step_id)


def parse_markers(stdout: str) -> tuple[dict[str, str], dict[str, str]]:
    status: dict[str, str] = {}
    slices: dict[str, str] = {}
    current, buf = None, []
    for line in stdout.splitlines():
        if line.startswith(MARK):
            try:
                m = json.loads(line[len(MARK):])
            except ValueError:
                buf.append(line)
                continue
            if m["status"] == "start":
                current, buf = m["cell"], []
                status[current] = "running"
            else:
                status[m["cell"]] = m["status"]
                slices[m["cell"]] = "\n".join(buf)
                current, buf = None, []
        elif current is not None:
            buf.append(line)
    if current is not None:
        slices[current] = "\n".join(buf)
    return status, slices


def is_install(step: Step) -> bool:
    return bool(INSTALL_RE.search(step.content))


def tail(text: str, n: int = 4000) -> str:
    return text[-n:]


@dataclass
class ExecState:
    image: ImageRef
    cells: list[Step] = field(default_factory=list)


@dataclass
class UnitResult:
    passed: bool
    results: dict[str, StepResult]
    failing_step_id: str | None
    state_after: ExecState | None
    reason: str = ""


@dataclass
class Trace:
    plan: RunPlan
    states: list[ExecState]
    results: dict[str, StepResult]
    failed_unit: int | None = None
    failing_step_id: str | None = None
    reason: str = ""


class Executor:
    def __init__(self, backend, settings, judge, emit: Emit | None = None):
        self.backend = backend
        self.s = settings
        self.judge = judge
        self._emit_fn = emit

    def with_emit(self, emit: Emit | None) -> "Executor":
        return Executor(self.backend, self.s, self.judge, emit)

    async def _emit(self, event: dict, quiet: bool) -> None:
        if self._emit_fn is not None and not quiet:
            await self._emit_fn(event)

    def _timeout(self, step: Step) -> int:
        return self.s.install_timeout_s if is_install(step) else self.s.step_timeout_s

    @staticmethod
    def _timed_out_note(r: StepResult, timeout: int) -> StepResult:
        note = f"\n[Fix My Tutorial] Step timed out after {timeout}s"
        return r.model_copy(update={"stderr": r.stderr + note})

    async def _single(self, step: Step, state: ExecState, language: str, run_id: str, public: bool) -> UnitResult:
        files: dict[str, bytes] = {}
        timeout = self._timeout(step)
        if step.kind == "write_file":
            files[f"/work/{step.path}"] = step.content.encode()
            body = f"echo {shlex.quote('wrote ' + (step.path or ''))}"
        elif step.kind == "run_code":  # JavaScript snippet
            ext = ".mjs" if ESM_RE.search(step.content) else ".cjs"
            files[f"/work/.fmt/{step.id}{ext}"] = step.content.encode()
            body = f"node /work/.fmt/{step.id}{ext}"
        elif step.kind == "serve":
            p = step.probe
            body = (f"( {step.content} ) > /tmp/serve.log 2>&1 &\nSERVER_PID=$!\n"
                    f"python3 /opt/fmt/probe.py --port {p.port} --path {shlex.quote(p.path)} "
                    f"--status {p.expect_status} --wait 40\nRC=$?\n"
                    "kill $SERVER_PID 2>/dev/null; sleep 0.5\necho '--- server log ---'\n"
                    "tail -n 60 /tmp/serve.log\nexit $RC")
            timeout += 45
        else:
            body = step.content
        r = await self.backend.run(state.image, f"{PRELUDE}\n{body}\n", files=files or None,
                                   timeout_s=timeout, save=True)
        if r.timed_out:
            r = self._timed_out_note(r, timeout)
            return UnitResult(False, {step.id: r}, step.id, None, f"timed out after {timeout}s")
        j = await self.judge.check(step, r.exit_code, r.stdout, run_id=run_id, public=public)
        if not j.passed:
            return UnitResult(False, {step.id: r}, step.id, None, j.reason)
        return UnitResult(True, {step.id: r}, None, ExecState(r.image or state.image, list(state.cells)))

    async def _cells(self, unit: list[Step], state: ExecState, run_id: str, public: bool) -> UnitResult:
        cells = state.cells + unit
        payload = json.dumps([{"id": c.id, "code": c.content} for c in cells]).encode()
        timeout = min(600, self.s.step_timeout_s + 60 * (len(cells) - 1))
        body = f"python3 /opt/fmt/run_cells.py /opt/fmt/cells.json --report-from {unit[0].id}"
        r = await self.backend.run(state.image, f"{PRELUDE}\n{body}\n", files={"/opt/fmt/cells.json": payload},
                                   timeout_s=timeout, save=True)
        if r.timed_out:
            r = self._timed_out_note(r, timeout)
        status, slices = parse_markers(r.stdout)
        results: dict[str, StepResult] = {}
        for c in unit:
            st = status.get(c.id)
            if st is None:
                break
            out = slices.get(c.id, "")
            if st == "ok":
                j = await self.judge.check(c, 0, out, run_id=run_id, public=public)
                if j.passed:
                    results[c.id] = StepResult(exit_code=0, stdout=out)
                    continue
                results[c.id] = StepResult(exit_code=1, stdout=out, stderr=f"Expected outcome not met: {j.reason}")
                return UnitResult(False, results, c.id, None, j.reason)
            reason = f"timed out after {timeout}s" if r.timed_out else "raised an error"
            results[c.id] = StepResult(exit_code=1, stdout=out, stderr=r.stderr, timed_out=r.timed_out,
                                       duration_s=r.duration_s)
            return UnitResult(False, results, c.id, None, reason)
        if len(results) < len(unit) or r.exit_code != 0:
            missing = next(c.id for c in unit if c.id not in results) if len(results) < len(unit) else unit[-1].id
            results[missing] = StepResult(exit_code=r.exit_code or 1, stdout=tail(r.stdout), stderr=r.stderr,
                                          timed_out=r.timed_out, duration_s=r.duration_s)
            return UnitResult(False, results, missing, None,
                              f"timed out after {timeout}s" if r.timed_out else "did not finish")
        return UnitResult(True, results, None, ExecState(r.image or state.image, cells))

    async def run_unit(self, unit: list[Step], state: ExecState, *, language: str, run_id: str,
                       public: bool) -> UnitResult:
        if unit[0].kind == "run_code" and language == "python":
            return await self._cells(unit, state, run_id, public)
        return await self._single(unit[0], state, language, run_id, public)

    async def execute(self, plan: RunPlan, states: list[ExecState], *, from_unit: int = 0,
                      stop_after: int | None = None, run_id: str, public: bool, phase: str = "run",
                      quiet: bool = False) -> Trace:
        units = group_units(plan.steps)
        states = list(states[: from_unit + 1])
        results: dict[str, StepResult] = {}
        last = len(units) - 1 if stop_after is None else min(stop_after, len(units) - 1)
        for i in range(from_unit, last + 1):
            unit = units[i]
            for s in unit:
                await self._emit({"type": "step", "step_id": s.id, "status": "running", "phase": phase}, quiet)
            ur = await self.run_unit(unit, states[i], language=plan.language, run_id=run_id, public=public)
            results.update(ur.results)
            for s in unit:
                if s.id in ur.results:
                    r = ur.results[s.id]
                    ok = s.id != ur.failing_step_id
                    output = tail(r.stdout + (("\n" + r.stderr) if r.stderr else ""))
                    await self._emit({"type": "step", "step_id": s.id, "status": "passed" if ok else "failed",
                                      "phase": phase, "output": output, "duration_s": r.duration_s}, quiet)
                else:
                    await self._emit({"type": "step", "step_id": s.id, "status": "pending", "phase": phase}, quiet)
            if not ur.passed:
                return Trace(plan, states, results, failed_unit=i, failing_step_id=ur.failing_step_id,
                             reason=ur.reason)
            states.append(ur.state_after)
        return Trace(plan, states, results)
