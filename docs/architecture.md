# Architecture

Fix My Tutorial is one Python 3.12 service (FastAPI, asyncio) in one Docker container. Every model call goes to Nebius Token Factory. Tutorial code runs in a sandbox: Token Factory Sandboxes (Contree microVMs) in production, or local Docker for development.

```
Browser ──HTTP/SSE──> FastAPI (web)
                         │
                         ├── jobs: in-process queue, max 4 concurrent runs
                         │
                         └── pipeline (per run)
                               ingest ──> planner ──> executor ──┬──> report
                                                    │            │
                                         on failure ▼            │
                                 triage ─> research ─> repair ─> judge
                                                    │
                     sandbox interface ─────────────┘
                       ├── ContreeBackend  (Token Factory Sandboxes, production)
                       └── DockerBackend   (local dev and fallback)

Shared: llm (Token Factory client + routing), ledger (spend guard),
        store (SQLite), events (per-run event bus feeding SSE)
```

## Modules

All modules live in `src/fixmytutorial/`.

| Module | Job |
|---|---|
| `config` | Settings from the environment: pinned model IDs, prices, spend caps, timeouts |
| `llm` | One OpenAI-compatible client for Token Factory; picks the Nemotron model by role; JSON-schema output validated with pydantic; our own retries; every call goes through the ledger |
| `ledger` | Records every model and Tavily call and refuses work that would cross a cap (`BudgetExceeded`). Fails closed: a call that may have been billed is charged at its upper bound |
| `search` | Tavily search and extract, with a 7-day cache in SQLite |
| `ingest` | URL or Markdown into clean text plus ordered code blocks. Blocks private addresses (DNS pinned to the checked IP), caps body size and decompression |
| `planner` | Nemotron 3 Super reads the tutorial and returns a `RunPlan`: ordered steps (`shell`, `write_file`, `run_code`, `serve`) with expectations. Output checks the page never shows are dropped |
| `sandbox` | `SandboxBackend` interface, the Docker and Contree backends, and the in-sandbox helpers |
| `executor` | Walks the plan unit by unit, saves a checkpoint image after each unit, emits step events |
| `judge` | Decides whether a step met its expectation: exit code and literal output first (whitespace-insensitive), Nemotron 3 Nano only for fuzzy expectations |
| `triage` | Nemotron 3.5 Lightning turns a failure log into a `FailureSignature` (category, key line, package, symbol) |
| `research` | Tavily search plus extract-with-query on official docs, changelogs and issue trackers; returns quoted, cited evidence |
| `repair` | Nemotron 3 Ultra proposes up to 3 candidate fixes per round (migrate, pin, workaround); they race in parallel from the last good checkpoint; up to 3 rounds |
| `pipeline` | Runs the stages above for one run and decides the verdict |
| `jobs` | In-process run queue with queue-position events |
| `report` | Diff, verdict, badge SVG and OG image for a finished run |
| `store` | SQLite persistence for runs, events and the cache |
| `snapshot` | Copies runs and the spend ledger to a private Hugging Face dataset so a restarted Space keeps its history and true lifetime spend |
| `events` | Per-run event bus backed by the store; feeds Server-Sent Events |
| `web` | Pages, SSE stream, rate limits, admin ledger view |
| `rot_index` | Batch runner and data for the public Rot Index board |
| `eval` | Runs every fixture tutorial through the real pipeline and scores it |

## Model routing

| Role | Model | Why |
|---|---|---|
| planner | `nvidia/nemotron-3-super-120b-a12b` | Long-context reading of the tutorial into a strict JSON plan |
| triage | `nvidia/Nemotron-3_5-Lightning` | Fast, cheap classification of error logs |
| judge | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Cheap yes/no on fuzzy expectations |
| repair | `nvidia/Nemotron-3-Ultra-550b-a55b` | Hard reasoning over error, code and evidence; only runs on failures |
| summary | `nvidia/nemotron-3-super-120b-a12b` | One-paragraph plain-language summary of the run |

## Run state machine

```
queued ─> ingesting ─> planning ─┬─> (not runnable) ─────────────────────────────> done
                                 │
                                 └─> executing ─┬─> (all steps pass, no repairs) ─> done  [still_works]
                                       ▲        │
                                       │        └─> repairing ─┬─> (no candidate passes) ─> done  [could_not_fix]
                                       │                       │
                                       └── winner applied ─────┘
                                                (continue from the next unit)

after the last unit passes with repairs:  replaying ─┬─> clean ─> done  [fixed]
                                                    └─> one more repair, replay again ─> done  [fixed | could_not_fix]

any unexpected error ─> failed  [error]
```

Verdicts: `still_works`, `fixed`, `could_not_fix`, `not_runnable`, `error`.

Bounds: at most 4 repaired steps per run, 3 repair rounds per step, 3 candidates per round, 40 steps per tutorial, 180 s per step (300 s for installs), 12 minutes per run.

Consecutive `run_code` steps form one unit: the Python cell runner executes them in one shared namespace, so notebook-style tutorials keep their variables, and a checkpoint is saved after each unit.

## Event types

Each run streams JSON events over SSE (`/run/{run_id}/events`). Every event has a `type`.

| type | Fields | Meaning |
|---|---|---|
| `queue` | `position` | Waiting for a free run slot |
| `stage` | `stage` | Run status changed (`ingesting`, `planning`, `executing`, `repairing`, `replaying`) |
| `plan` | `title`, `language`, `runtime`, `note`, `steps[]` | The plan, re-sent after each applied fix |
| `step` | `step_id`, `status`, `phase`, `output?`, `duration_s?` | `running`, `passed`, `failed`, `pending`, `repairing` or `repaired`; `phase` is `run` or `replay` |
| `diagnosis` | `step_id`, `category`, `key_line`, `package`, `symbol` | Triage result for a failing step |
| `evidence` | `step_id`, `items[]` | Cited sources (url, title, quote) found by research |
| `candidates` | `step_id`, `round`, `candidates[]` | The fixes proposed for this round |
| `candidate_result` | `step_id`, `round`, `index`, `passed`, `error` | Outcome of one racing candidate |
| `verdict` | `verdict`, `reason`, `summary`, `cost_usd` | Final result |
| `done` | | Stream end |
| `ping` | | Keep-alive while nothing new has happened |

## Sandbox contract

```python
class SandboxBackend(Protocol):
    name: str

    async def base_image(self, language: str) -> ImageRef: ...

    async def run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None = None,
                  timeout_s: int = 180, save: bool = True, env: dict[str, str] | None = None) -> StepResult: ...
```

- `base_image` returns a prepared image for `python` (3.12) or `javascript` (Node 22) with our helpers in `/opt/fmt`.
- `run` executes a shell script in a fresh sandbox started from `image`, writing `files` first. With `save=True` the resulting filesystem becomes a new `ImageRef` (the checkpoint); with `save=False` the sandbox is thrown away.
- `StepResult` holds `exit_code`, `stdout`, `stderr` (output keeps only the last 64 KB), `timed_out`, `duration_s` and `image`.
- Parallel repair runs several `run` calls from the same checkpoint at once. Each candidate gets its own branch; the winner's image becomes the new checkpoint.
- No API key is ever passed into a sandbox. Tutorials that call an OpenAI-style API talk to a stand-in server inside the sandbox (`127.0.0.1:8099`) that returns canned responses.

Both backends pass the same contract tests (`tests/sandbox_contract.py`).

| Backend | Checkpoint | Branch | Isolation |
|---|---|---|---|
| `ContreeBackend` | Contree image UUID after a saved run | Run from the same UUID | Token Factory Sandboxes microVM |
| `DockerBackend` | `docker commit` | `docker run` from the committed image | Dropped capabilities, no-new-privileges, pids limit, dedicated bridge network; refuses to run if the sandbox can reach private addresses (see `docs/deploy.md`) |
