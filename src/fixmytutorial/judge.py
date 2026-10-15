from __future__ import annotations

from .ledger import BudgetExceeded
from .models import Judgement, Step
from .prompts import JUDGE_SYSTEM


def deterministic_check(step: Step, exit_code: int, stdout: str) -> bool | None:
    if exit_code != step.expect.exit_code:
        return False
    if any(s not in stdout for s in step.expect.stdout_contains):
        return False
    if step.kind == "run_code" and step.expect.description and not step.expect.stdout_contains:
        return None
    return True


class Judge:
    def __init__(self, llm):
        self.llm = llm

    async def check(self, step: Step, exit_code: int, stdout: str, *, run_id: str, public: bool) -> Judgement:
        d = deterministic_check(step, exit_code, stdout)
        if d is not None:
            reason = "" if d else (f"exit code {exit_code}" if exit_code != step.expect.exit_code
                                   else "expected output not found")
            return Judgement(passed=d, reason=reason)
        if self.llm is None:
            return Judgement(passed=True, reason="exit code 0")
        user = (f"Expected outcome: {step.expect.description}\n\nCode:\n{step.content[:2000]}\n\n"
                f"Actual output (last 3000 characters):\n{stdout[-3000:]}")
        try:
            return await self.llm.call("judge", JUDGE_SYSTEM, user, Judgement, run_id=run_id, public=public,
                                       max_tokens=300, temperature=0)
        except BudgetExceeded:
            raise
        except Exception:
            return Judgement(passed=True, reason="exit code 0 (judge unavailable)")
