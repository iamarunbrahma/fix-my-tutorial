from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Language = Literal["python", "javascript"]
StepKind = Literal["shell", "write_file", "run_code", "serve"]
Strategy = Literal["migrate", "pin", "workaround"]
FailureCategory = Literal[
    "removed_api", "renamed_package", "version_conflict", "dead_resource",
    "changed_cli", "tutorial_typo", "environment", "timeout", "unknown",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Expectation(BaseModel):
    exit_code: int = 0
    stdout_contains: list[str] = Field(default_factory=list)
    description: str | None = None


class Probe(BaseModel):
    port: int
    path: str = "/"
    expect_status: int = 200


class PlannerStep(BaseModel):
    """Step as produced by the planner model."""
    id: str
    title: str
    kind: StepKind
    content: str
    path: str | None = None
    expect: Expectation = Field(default_factory=Expectation)
    probe: Probe | None = None
    implicit: bool = False
    source_excerpt: str = ""


class PlannerOutput(BaseModel):
    language: Language
    runnable: bool = True
    not_runnable_reason: str | None = None
    steps: list[PlannerStep] = Field(default_factory=list)


class Step(PlannerStep):
    fix_reason: str = ""
    fix_evidence_ids: list[str] = Field(default_factory=list)


class RunPlan(BaseModel):
    language: Language
    runtime: str
    runnable: bool = True
    not_runnable_reason: str | None = None
    note: str = ""
    steps: list[Step] = Field(default_factory=list)
    coverage: float = 1.0  # share of the page's code lines that made it into the plan
    truncated: bool = False  # the page or the plan was cut short


class CodeBlock(BaseModel):
    lang: str = ""
    code: str


class TutorialDoc(BaseModel):
    source: str
    title: str
    text: str
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    content_hash: str
    published: str | None = None
    truncated: bool = False  # the page was longer than we read


class ImageRef(BaseModel):
    backend: str
    ref: str


class StepResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    image: ImageRef | None = None


class FailureSignature(BaseModel):
    category: FailureCategory = "unknown"
    error_type: str = ""
    key_line: str = ""
    package: str | None = None
    symbol: str | None = None
    search_queries: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str
    url: str
    title: str
    quote: str


class EvidenceList(BaseModel):
    items: list[Evidence] = Field(default_factory=list)


class Edit(BaseModel):
    op: Literal["replace", "insert_after"] = "replace"
    step_id: str
    new_content: str
    new_kind: StepKind | None = None
    new_path: str | None = None
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    strategy: Strategy
    rationale: str
    edits: list[Edit]


class RepairProposal(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class Judgement(BaseModel):
    passed: bool
    reason: str = ""


class Summary(BaseModel):
    summary: str


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    REPAIRING = "repairing"
    REPAIRED = "repaired"
    SKIPPED = "skipped"


class Verdict(str, Enum):
    STILL_WORKS = "still_works"
    FIXED = "fixed"
    COULD_NOT_FIX = "could_not_fix"
    NOT_RUNNABLE = "not_runnable"
    ERROR = "error"


class RunStatus(str, Enum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    PLANNING = "planning"
    EXECUTING = "executing"
    REPAIRING = "repairing"
    REPLAYING = "replaying"
    DONE = "done"
    FAILED = "failed"


class RoundRecord(BaseModel):
    round: int
    candidates: list[Candidate] = Field(default_factory=list)
    passed: list[bool] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    winner_index: int | None = None


class RepairRecord(BaseModel):
    step_id: str
    signature: FailureSignature
    evidence: list[Evidence] = Field(default_factory=list)
    rounds: list[RoundRecord] = Field(default_factory=list)
    winner: Candidate | None = None
    stopped: str = ""  # "time" when the run's clock left no room for another round


class StepOutcome(BaseModel):
    step_id: str
    status: StepStatus
    stdout_tail: str = ""
    duration_s: float = 0.0


class RunRecord(BaseModel):
    id: str
    source: str
    public: bool = True
    status: RunStatus = RunStatus.QUEUED
    verdict: Verdict | None = None
    title: str = ""
    content_hash: str = ""
    published: str | None = None  # the tutorial page's own publish date (YYYY-MM-DD), when it states one
    plan: RunPlan | None = None
    final_plan: RunPlan | None = None
    outcomes: dict[str, StepOutcome] = Field(default_factory=dict)
    repairs: list[RepairRecord] = Field(default_factory=list)
    summary: str = ""
    reason: str = ""
    cost_usd: float = 0.0
    tavily_credits: int = 0
    created_at: datetime
    finished_at: datetime | None = None
