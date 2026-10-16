from __future__ import annotations

from .ledger import BudgetExceeded
from .models import Judgement, Step
from .prompts import JUDGE_SYSTEM

# Part of the fixed reply from the in-sandbox stand-in OpenAI server (sandbox/assets/openai_stub.py).
STUB_MARKER = "Fix My Tutorial stand-in server"


def squash(text: str) -> str:
    """Collapse whitespace runs so column alignment never decides a match."""
    return " ".join(text.split())


def code_lines(text: str) -> list[tuple[int, str]]:
    """Code compared line by line: blank lines and spacing inside a line don't matter, indentation does."""
    return [(len(ln) - len(ln.lstrip()), squash(ln)) for ln in text.splitlines() if ln.strip()]


def deterministic_check(step: Step, exit_code: int, stdout: str) -> bool | None:
    if exit_code != step.expect.exit_code:
        return False
    out = squash(stdout)
    if any(squash(s) not in out for s in step.expect.stdout_contains):
        return False
    if step.kind == "run_code" and step.expect.description and not step.expect.stdout_contains:
        return None
    return True


class Judge:
    def __init__(self, llm):
        self.llm = llm

    async def check(self, step: Step, exit_code: int, stdout: str, *, run_id: str, public: bool) -> Judgement:
        d = deterministic_check(step, exit_code, stdout)
        if d is False and exit_code == step.expect.exit_code and self.llm is not None:
            # the code ran but the output isn't the page's text exactly: a renamed heading or a different dict order
            # is not a break, so the lenient judge compares it with what the tutorial shows
            shown = "\n".join(step.expect.stdout_contains)[:1500]
            user = (
                f"Expected outcome: output like this, as the tutorial shows it:\n{shown}\n\n"
                f"Code:\n{step.content[:2000]}\n\nActual output (last 3000 characters):\n{stdout[-3000:]}"
            )
            try:
                return await self.llm.call(
                    "judge",
                    JUDGE_SYSTEM,
                    user,
                    Judgement,
                    run_id=run_id,
                    public=public,
                    max_tokens=300,
                    temperature=0,
                )
            except BudgetExceeded:
                raise
            except Exception:
                return Judgement(passed=False, reason="expected output not found")
        if d is not None:
            reason = (
                ""
                if d
                else (
                    f"exit code {exit_code}"
                    if exit_code != step.expect.exit_code
                    else "expected output not found"
                )
            )
            return Judgement(passed=d, reason=reason)
        if STUB_MARKER in stdout:  # model text is a placeholder, so only "the API call worked" can be checked
            return Judgement(passed=True, reason="reached the stand-in API")
        if self.llm is None:
            return Judgement(passed=True, reason="exit code 0")
        user = (
            f"Expected outcome: {step.expect.description}\n\nCode:\n{step.content[:2000]}\n\n"
            f"Actual output (last 3000 characters):\n{stdout[-3000:]}"
        )
        try:
            return await self.llm.call(
                "judge",
                JUDGE_SYSTEM,
                user,
                Judgement,
                run_id=run_id,
                public=public,
                max_tokens=300,
                temperature=0,
            )
        except BudgetExceeded:
            raise
        except Exception:
            return Judgement(passed=True, reason="exit code 0 (judge unavailable)")
