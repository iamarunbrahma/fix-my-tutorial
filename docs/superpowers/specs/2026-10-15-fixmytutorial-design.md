# Fix My Tutorial - Design Spec

- **Date:** 2026-10-15
- **Event:** Nebius x NVIDIA Global AI Hackathon (Devpost)
- **Submission deadline:** 2026-10-30 10:30 PM IST (10:00 AM PT). Internal target: submit by 2026-10-27.
- **Track:** Coding and Agentic Engineering. Also eligible for the Best Use of Tavily bonus and Most Valuable Feedback.
- **Owner:** Arun Brahma (solo entrant). Claude builds, tests and produces the demo video.

---

## 1. Summary

Coding tutorials rot. A guide written in 2023 breaks in 2026 because a library removed a function, renamed a package, or changed its defaults. Learners hit the error, search for hours, and often give up. AI coding assistants trained on those same tutorials repeat the broken code. NVIDIA's own OpenShell repo has an open issue literally titled "Tutorial Rot" (#2998).

**Fix My Tutorial** takes any Python or JavaScript tutorial URL and:

1. turns the prose into an executable plan (Nemotron 3 Super),
2. runs it step by step in an isolated Token Factory Sandbox, saving a snapshot after each step,
3. when a step breaks, labels the failure (Nemotron 3.5 Lightning), finds the changelog or migration note that explains it (Tavily), and proposes three candidate fixes (Nemotron 3 Ultra),
4. runs all three fixes in parallel from the last good snapshot and keeps the one that passes,
5. replays the whole patched tutorial from scratch to prove it works, then publishes a report: a verdict, a code diff where every change carries its reason and citation, and an embeddable "Verified working" badge.

A public **Rot Index** shows the result of running Fix My Tutorial on about 100 popular tutorials: how many still work, why the rest broke, and the verified fix for each.

## 2. Goals and success criteria

| Goal | Measurable success |
|---|---|
| Works on real rotted tutorials | On the 15-tutorial fixture set, at least 10 reach "Fixed" with a clean end-to-end replay, and 0 false "Fixed" verdicts |
| Uses Nebius + NVIDIA meaningfully | Every run makes runtime calls to Token Factory with at least two Nemotron sizes; code execution runs on Token Factory Sandboxes |
| Credible impact story | Rot Index of about 100 tutorials published with headline stats and root-cause breakdown |
| Complete product | Hosted URL works for judges until 2026-12-16 with no login |
| Never charges the card | Lifetime model spend stays under the $35 hard cap enforced in code |
| Submission complete | Public MIT repo, README, under-3-minute YouTube video, Devpost form, feedback section, all done by 2026-10-27 |

## 3. Hackathon fit

### 3.1 Hard requirements (from Official Rules) and how we meet them

| Requirement | How |
|---|---|
| Runs on Nebius Token Factory or AI Cloud | Runtime calls to the Token Factory inference API on every run; execution on Token Factory Sandboxes |
| Uses at least one NVIDIA open model | Nemotron 3.5 Lightning, Nemotron 3 Nano, Nemotron 3 Super, Nemotron 3 Ultra |
| Fits one track | Coding and Agentic Engineering: "agents that write, run, and test code in Token Factory" |
| Working demo URL | Hugging Face Space (Docker), no login, free for judges through the judging period |
| Public repo with OSS license visible in About | GitHub `iamarunbrahma/fix-my-tutorial`, MIT license |
| README with setup and Nemotron / Token Factory usage | Dedicated sections; see section 13 |
| Video under 3 minutes, public on YouTube | Pitch-style video; names Token Factory and Nemotron with audio; see section 13 |
| Feedback on Token Factory, AI Cloud, NVIDIA models | `docs/feedback.md`, kept as a running log during the build |
| English | All materials in English |
| No secrets in repo | `.env` gitignored, secret scan before every push |

### 3.2 Judging criteria mapping (each scored 1-5, equal weight)

- **Technological implementation:** a fixed pipeline with explicit model routing (cheap fast models for triage, Ultra only for repair), Sandboxes checkpoint-and-branch for parallel repair, and an end-to-end clean replay before any "Fixed" verdict.
- **Design:** a live run view that makes the agent's work legible (steps lighting up, diagnosis card, racing repair branches), a clear verdict, and shareable outputs (report, badge).
- **Potential impact:** learners lose hours on broken tutorials; DevRel teams cannot re-test every guide on every release. The Rot Index quantifies the problem with real data.
- **Quality of the idea:** not a code-completion wrapper. It executes third-party prose as a program and repairs it with cited evidence. Checked against existing tools: Doc Detective requires maintainers to hand-write tests; content-freshness checkers do not execute code.

### 3.3 Prizes in reach

A project can win one overall prize, or one track prize plus one bonus. Targets: Grand / 2nd / 3rd, or Coding track winner plus Best Use of Tavily. The feedback write-up also competes for Most Valuable Feedback.

## 4. Scope

### 4.1 In scope

- **Languages:** Python (pip) and JavaScript (Node.js with npm).
- **Inputs:** a public URL (blog post, docs page, GitHub README, GitHub-hosted `.ipynb`), or pasted Markdown.
- **Runtime:** CPU-only Linux sandbox with network access for package installs.
- **LLM tutorials:** tutorials that call an OpenAI-compatible API run against a stand-in server inside the sandbox (section 7.4), so they can be verified without a real key.
- **Rot Index:** about 100 tutorials (about 70 Python, 30 JavaScript).

### 4.2 Out of scope (roadmap in README)

- HTML/CSS browser tutorials, SQL, Java, C++ (next languages; the engine is language-agnostic so each needs an image plus a "works" check).
- GPU-only tutorials (verdict "Not runnable").
- Tutorials needing real paid credentials other than OpenAI-compatible APIs (verdict "Not runnable").
- Automated pull requests or contacting tutorial authors.
- User accounts.

## 5. User experience

### 5.1 Pages

1. **Home** `/` - a URL box ("Paste a Python or JavaScript tutorial"), a paste-Markdown option, three example tutorials, and a link to the Rot Index.
2. **Live run** `/run/{run_id}`:
   - Left column: the plan's steps in tutorial order, each with a status chip: pending, running, passed, failed, repairing, repaired, skipped.
   - Right column: the selected step's terminal output, streamed live.
   - **Diagnosis card** on failure: failure category, the error line, and the cited source (title, domain, quoted passage).
   - **Repair panel:** up to 3 candidate fixes shown racing side by side. The winner is highlighted and the run continues.
   - **Replay banner:** "Replaying the fixed tutorial from scratch to confirm," with its own progress.
3. **Report** `/r/{run_id}`:
   - Verdict, runtime versions used, date verified, and time and cost of the run.
   - **Code diff only:** each change shows before/after code, a one-line reason, a citation link, and whether it is a migration (preferred) or a version pin (fallback).
   - Embed snippet for the badge, a "Run again" button, and a link to the original tutorial.
   - An Open Graph preview image for social sharing.
4. **Rot Index** `/index`:
   - Headline stats (% still working, % fixed, % could not fix, % not runnable).
   - Root-cause breakdown and a filterable table (language, source site, publish year, verdict) linking to each report.
5. **Badge** `/badge/{run_id}.svg` - for example "Fix My Tutorial · verified working · Python 3.12 · Oct 2026", or "fixed version available".

### 5.2 Verdicts

| Verdict | Meaning |
|---|---|
| Still works | Every step passed on the first run |
| Fixed (N changes) | Some steps failed, repairs passed, and a clean end-to-end replay of the patched plan passed |
| Could not fix | Repair budget exhausted; the report shows the root cause found and the attempts made |
| Not runnable | Needs a GPU, paid credentials, or hardware; detected at planning time or from the error |

### 5.3 Copyright rule

We never republish a tutorial's prose. Reports show only the changed code lines with minimal context, plus a link to the original. The planner may read the full page; storage keeps only the plan, logs, and diffs.

## 6. Architecture

One Python 3.12 service (FastAPI, asyncio), deployed as a single Docker container.

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

### 6.1 Modules

Each module has one job and a narrow interface.

| Module | Job | Interface (sketch) |
|---|---|---|
| `config` | Settings from env; pinned model IDs, prices, caps | `Settings` (pydantic-settings) |
| `llm` | Token Factory OpenAI-compatible client, routing by role, JSON-schema output, retries | `await llm.call(role, messages, schema) -> parsed` |
| `ledger` | Records every model and Tavily call, enforces caps | `ledger.reserve(role, est_tokens)`, `ledger.commit(usage)`, raises `BudgetExceeded` |
| `ingest` | URL or Markdown into clean text plus code blocks; content hash | `await ingest(source) -> TutorialDoc` |
| `planner` | Tutorial into `RunPlan` | `await plan(doc) -> RunPlan` |
| `sandbox` | Run a step from an image, return result plus new image | `await backend.run(image, step, files, timeout) -> StepResult` |
| `executor` | Walks the plan, checkpoints, emits events | `await execute(plan, start_image) -> ExecutionTrace` |
| `triage` | Failure log into `FailureSignature` | `await triage(step, result) -> FailureSignature` |
| `research` | Signature into cited evidence | `await research(sig, env) -> list[Evidence]` |
| `repair` | Context into 3 candidate patches; races them | `await repair(ctx) -> RepairOutcome` |
| `judge` | Did the step meet its expectation? | `await judge(step, result) -> Judgement` |
| `report` | Diff, verdict, badge SVG, OG image | `build_report(run) -> Report` |
| `store` | SQLite persistence; snapshot to HF dataset | repository functions |
| `web` | Pages, SSE, rate limiting | FastAPI routers |
| `rot_index` | Batch runner and board data | CLI: `python -m fixmytutorial.rot_index run list.yaml` |

### 6.2 Data model (pydantic, persisted as JSON in SQLite)

```python
class Step(BaseModel):
    id: str                      # "s3"
    title: str                   # short human label
    kind: Literal["shell", "write_file", "run_code", "serve"]
    content: str                 # command, file body, or code
    path: str | None = None      # for write_file / run_code target file
    expect: Expectation          # exit_code, stdout_contains, description
    probe: Probe | None = None   # for serve: port, path, expect_status
    source_excerpt: str          # the tutorial snippet this came from (kept short, used for diff)

class RunPlan(BaseModel):
    language: Literal["python", "javascript"]
    runtime: str                 # "3.12" or "22"
    runnable: bool
    not_runnable_reason: str | None
    steps: list[Step]

class FailureSignature(BaseModel):
    category: Literal["removed_api", "renamed_package", "version_conflict",
                      "dead_resource", "changed_cli", "tutorial_typo",
                      "environment", "unknown"]
    error_type: str              # "ImportError"
    key_line: str                # the decisive error line
    package: str | None
    symbol: str | None
    search_queries: list[str]    # 1-3 queries for research

class Evidence(BaseModel):
    id: str
    url: str
    title: str
    quote: str                   # at most 600 chars

class Edit(BaseModel):
    step_id: str
    new_content: str
    strategy: Literal["migrate", "pin", "workaround"]
    reason: str                  # one sentence
    evidence_ids: list[str]

class Candidate(BaseModel):
    edits: list[Edit]
    rationale: str
```

Run state machine: `queued -> ingesting -> planning -> executing -> (repairing <-> executing)* -> replaying -> done | failed`.

## 7. Execution layer

### 7.1 Sandbox interface

```python
class SandboxBackend(Protocol):
    async def base_image(self, language: str, runtime: str) -> ImageRef: ...
    async def run(self, image: ImageRef, command: str, *, files: dict[str, bytes],
                  cwd: str, timeout_s: int, save: bool, env: dict[str, str]) -> StepResult: ...
```

`StepResult` holds `exit_code`, `stdout`, `stderr` (each capped at 64 KB in our store), `timed_out`, `duration_s`, and `image: ImageRef | None` (set when `save=True` and a new image is returned).

### 7.2 ContreeBackend (production)

Built on `contree-sdk==0.3.6` (pinned, as in the Nebius docs):

- Base images via `client.images.oci("docker.io/library/python:3.12-slim")` and `...node:22-slim`. They are prepared once with our runner script and the stand-in OpenAI server, saved, and the resulting UUIDs recorded in config.
- Each step: `image.run(shell=..., files=..., cwd="/work", disposable=not save, timeout=...)`, then check `exit_code`; when `save=True`, continue from `result.uuid` via `client.images.use(uuid, strict=True)`.
- Parallel repair: `asyncio.gather` over candidate runs from the same checkpoint UUID, mirroring the documented "try two coding fixes" recipe.
- Networking stays on (default) for package installs. Our API keys are never passed into a sandbox.
- Limits respected: 50 simultaneous operations during beta (we cap at 12); 180-day image retention.

### 7.3 DockerBackend (dev and fallback)

Same interface on a local Docker daemon: `docker commit` gives checkpoints and `docker run` from a committed image gives branches. Used for development, tests, and as the production fallback (on the Oracle VM) only if Sandboxes beta access is not granted by 2026-10-20.

### 7.4 In-sandbox helpers (baked into base images)

- **Cell runner (Python):** `run_cells.py` executes `run_code` cells in one shared namespace, in order, printing a JSON marker line before and after each cell (`@@FMT {"cell": "s4", "status": "ok"}`), with a traceback on failure. This keeps notebook-style tutorials (variables flowing between blocks) correct even though sandbox processes do not persist between operations. On repair, branches re-run the cells from the last setup checkpoint.
- **JavaScript:** steps are mostly files plus commands (`npm install`, `node app.js`, `npm run build`, `npm test`), run as shell steps.
- **Serve steps:** start the server in the background, wait up to 30 s for the port, request `probe.path`, check status, then stop it.
- **Stand-in OpenAI-compatible server:** a tiny local server on `127.0.0.1:8099` implementing `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` and `/v1/models` with deterministic canned responses. `OPENAI_BASE_URL`, `OPENAI_API_BASE` and `OPENAI_API_KEY=sk-fixmytutorial-stub` are set in the sandbox environment. This verifies SDK usage (the biggest LLM-tutorial rot source, e.g. `openai.ChatCompletion` removed in openai 1.0) without any real key.
- **Stdin:** steps that call `input()` get an empty stdin; the planner rewrites prompts into constants where needed and records that as an edit.

## 8. Agent pipeline

### 8.1 Model routing

Model IDs are pinned in config after listing `GET /v1/models` with the project key. Prices are Token Factory list prices on 2026-10-15, per 1M tokens (input / output).

| Role | Model | Price | Why |
|---|---|---|---|
| planner | Nemotron 3 Super 120B-A12B | $0.30 / $0.90 | Long-context reading of the tutorial into a strict JSON plan |
| triage | Nemotron 3.5 Lightning 30B-A3B | $0.06 / $0.24 | Fast, cheap classification of error logs |
| judge | Nemotron 3 Nano 30B-A3B | $0.06 / $0.24 | Cheap yes/no on fuzzy expectations |
| repair | Nemotron 3 Ultra 550B-A55B | $1.00 / $3.00 | Hard reasoning over error, code and evidence; only on failures |
| report summary | Nemotron 3 Super | $0.30 / $0.90 | One-paragraph human summary of the run |

All structured outputs use JSON schema (`response_format`), validated with pydantic; one automatic retry with the validation error appended, then the stage fails cleanly.

### 8.2 Stages

1. **Ingest.**
   - URL: Tavily Extract (advanced) returns cleaned page content.
   - GitHub URLs: fetched as raw files (`.md`, `.ipynb` parsed into cells).
   - Fallback: `httpx` with a readability extraction, blocking private and loopback IPs.
   - Output: title, published date if found, text, ordered code blocks, and a content hash used for the cache key.
2. **Plan (Super).**
   - Input: the tutorial text plus code blocks.
   - Output: `RunPlan`. Rules in the prompt:
     - preserve the tutorial's code verbatim (no silent fixes at planning time);
     - add missing implicit steps (e.g. `pip install` for imported packages, creating `package.json`) and mark them as "implicit";
     - mark GPU or credential needs as not runnable;
     - prefer unpinned installs, so we test "today's" versions.
3. **Setup checkpoint.** Install and file-write steps run one per operation with `save=True`, giving a checkpoint image after each.
4. **Execute.**
   - `run_code` cells run through the cell runner; shell and serve steps run directly.
   - The executor streams per-step events.
   - On the first failing step it stops and goes to repair.
5. **Triage (Lightning).** Input: the failing step, the last 150 lines of output, and installed versions (`pip freeze` or `npm ls --depth=0`). Output: `FailureSignature` with 1-3 search queries.
6. **Research (Tavily).**
   - Run `search` (basic depth, 5 results) for each query, preferring official docs, changelogs, release notes, GitHub issues and Stack Overflow.
   - Then `extract` the top 3 URLs and keep the passages that mention the symbol or package.
   - Output: up to 6 `Evidence` items.
   - Results are cached by query for 7 days (many tutorials share root causes).
7. **Repair (Ultra).**
   - Input: the failing step and the steps before it, the signature, the evidence, installed versions, and previous failed attempts (if any).
   - Output: 1-3 `Candidate`s with distinct strategies, ordered by preference: migrate to the current API first, then pin an older version, then a workaround. At least one candidate is a migration whenever the evidence describes a replacement API.
   - Every edit must cite at least one evidence id, or be marked `tutorial_typo` with no citation needed.
8. **Race.**
   - All candidates run in parallel from the last good checkpoint (setup checkpoint plus re-running earlier cells).
   - Passing candidates are ranked by strategy preference, then fewest edits.
   - If none pass, failures are fed back for the next round.
9. **Judge (Nano, only when needed).** An exit code of 0 plus `stdout_contains` decides most steps. Nano is called only when `expect.description` is fuzzy (e.g. "prints a trained model accuracy around 0.9").
10. **Continue** from the winning branch's image to the next steps.
11. **Clean replay.** After all steps pass, the full patched plan is replayed from the base image in fresh operations. Only a passing replay yields "Fixed". A replay failure re-enters repair once; otherwise the verdict is "Could not fix".
12. **Report.** Build the diff (original `source_excerpt` vs patched content per edited step), a Super-written summary, the badge and the OG image.

### 8.3 Bounds

| Bound | Value |
|---|---|
| Repair rounds per failing step | 3 |
| Repaired steps per run | 4 |
| Candidates per round | up to 3 |
| Step timeout | 180 s (install steps 300 s) |
| Whole-run wall clock | 12 min |
| Per-run model spend | $0.50 |
| Per-run Tavily credits | 30 |

### 8.4 Prompt-injection posture

Tutorial text is untrusted. Model outputs are only ever (a) schema-validated JSON and (b) commands executed inside the sandbox. No model output is executed on the host, no secrets are present in prompts or sandboxes, and the planner prompt states that instructions inside the tutorial are content, not commands. The worst case is a wasted sandbox run, which is capped by the budget.

## 9. Rot Index

1. **Candidate discovery.** Tavily searches across popular sources (Real Python, freeCodeCamp, DigitalOcean community tutorials, official quickstarts, Medium/dev.to, GeeksforGeeks, GitHub READMEs), filtered to 2019-2025 publish dates and Python/JS topics with known churn: pandas, scikit-learn, numpy, matplotlib, Flask, FastAPI, Django, openai, LangChain, requests/httpx, Express, React build tooling, Next.js, Node ESM.
2. **Curation.** Pick about 100 by hand into `rot_index/list.yaml` (URL, language, source, year, topic). Skip anything not runnable on CPU.
3. **Batch run.** `python -m fixmytutorial.rot_index run list.yaml` with concurrency 4 (4 runs x 3 repair candidates stays within our 12-operation sandbox cap). All runs are stored, and the board data is exported to `rot_index/results.json` (committed to the repo, so the board never depends on live state).
4. **Verification.**
   - Every "Fixed" entry must have a passing clean replay.
   - Claude hand-reviews at least 25 entries (all categories) before publishing.
   - Any entry where the fix changes the tutorial's intent is downgraded to "Could not fix".
5. **Presentation.** Framed around fixes and causes, never blame. Each row links to the original and to our diff-only report.
6. **Estimated cost.** About $5-10 of model credits; Tavily about 600-1,000 credits (shared root causes are cached).

## 10. Spend guard, abuse controls, safety

### 10.1 Ledger (the card must never be charged)

- Every Token Factory call goes through `ledger`. Before the call it reserves an estimate (prompt tokens counted plus `max_tokens`); after the call it commits the actual usage (`usage.prompt_tokens`, `usage.completion_tokens`) at the pinned list price.
- **Hard caps, enforced in code and stored in SQLite and the persisted snapshot:**

| Cap | Value |
|---|---|
| Lifetime | $35.00 |
| Public per-day | $3.00 |
| Per run | $0.50 |

- On any cap: the stage raises `BudgetExceeded`, the run ends with a friendly message, and new public runs are refused while the Rot Index and cached reports keep working.
- Tavily: per-run credit cap (30) and a lifetime credit cap set below available credits.
- Credits available: $1 trial + $25 promo (code received 2026-10-15; applied once billing is saved) + $25 Builders Program (pending) = $51. The $35 cap leaves margin for list-price drift.
- An admin page `/admin/ledger` (token-protected) shows spend by role and day.

### 10.2 Public demo controls

- Rate limit: 3 new runs per hour per IP, and 4 concurrent runs globally (extra runs queue with a visible position).
- Cache: same URL plus the same content hash within 24 h serves the existing report; "Run again" respects the rate limit.
- The three homepage examples and all Rot Index entries are pre-run, so judges see full results instantly even if the budget is paused.

### 10.3 Account-side reminders

Calendar events: weekly Token Factory balance check (every Monday until 2026-12-16), and "Remove card from Nebius Token Factory" on 2026-12-16.

### 10.4 Secrets

`NEBIUS_API_KEY`, `NEBIUS_PROJECT_ID`, `TAVILY_API_KEY`, `HF_TOKEN` and `ADMIN_TOKEN` live only in `.env` (gitignored) and Hugging Face Space secrets. A pre-push secret scan uses `gitleaks`.

## 11. Hosting and persistence

- **Production:** Hugging Face Space (Docker SDK, free CPU tier, HTTPS) under the `heliosbrahma` account. Single container running `uvicorn`.
- **Keep-alive:** a GitHub Actions cron pings `/healthz` every 12 h through 2026-12-16, because free Spaces sleep after 48 h without traffic.
- **Persistence:** SQLite on the container disk for live state. Finished reports and the ledger are snapshotted to a private Hugging Face dataset repo (`heliosbrahma/fixmytutorial-data`) after each run and restored on boot, so shared report links survive restarts. Rot Index results are also committed as JSON in the code repo.
- **Fallback:** if Sandboxes beta access is not granted by 2026-10-20, deploy the same container on the existing Oracle Cloud VM with `DockerBackend`, behind Caddy HTTPS on a DuckDNS subdomain. No other changes.

## 12. Testing and evaluation

- **Unit tests (pytest):**
  - `llm` with a fake transport;
  - `ledger` cap math, including concurrent reservations;
  - `ingest` on saved HTML and `.ipynb` fixtures;
  - the planner schema;
  - cell-runner marker parsing;
  - the repair ranking;
  - report diff rendering;
  - rate limiter.
- **Sandbox contract tests:** the same suite runs against `DockerBackend` and `ContreeBackend` (the latter marked `@pytest.mark.live`): run, save, branch, parallel branches, timeout, output cap.
- **Fixture tutorials:** 15 in `tests/fixtures/tutorials/` (10 Python, 5 JavaScript), each a small Markdown tutorial with a known rot cause, an expected verdict and an expected fix category. Examples:
  - `sklearn.datasets.load_boston` (removed in 1.2)
  - `DataFrame.append` (removed in pandas 2.0)
  - `np.float` (removed in numpy 1.24)
  - `openai.ChatCompletion.create` (removed in openai 1.0)
  - `from langchain.llms import OpenAI` (moved)
  - Flask `before_first_request` (removed in 2.3)
  - `matplotlib` `seaborn` style name change
  - `create-react-app` deprecated
  - Node `require` of an ESM-only package (`node-fetch` 3)
  - Express 5 route syntax change
  - plus 2 tutorials that still work, as negative controls
- **Eval script:** `python -m fixmytutorial.eval` reports per-fixture verdict, fix strategy, rounds, wall time, model cost and Tavily credits; it fails CI on any false "Fixed" verdict.
- **Manual QA:** browser-harness walkthrough of every page on desktop and mobile widths before recording the video.

## 13. Deliverables

### 13.1 Repository layout

```
fix-my-tutorial/
  README.md  LICENSE (MIT)  Dockerfile  pyproject.toml  .env.example  .gitignore
  src/fixmytutorial/{config,llm,ledger,ingest,planner,sandbox/,executor,triage,
                     research,repair,judge,report,store,web/,rot_index,eval}.py
  sandbox_images/{python,node}/  (runner script, stand-in OpenAI server, build notes)
  rot_index/{list.yaml,results.json}
  tests/  docs/{feedback.md, architecture.md, superpowers/specs/, superpowers/plans/}
  .github/workflows/{ci.yml,keepalive.yml}
```

### 13.2 README sections

What it does (with GIF), why (tutorial rot plus Rot Index stats), live demo link, how it works (diagram plus model-routing table), **how we use NVIDIA Nemotron**, **where Token Factory (inference + Sandboxes) accelerated the workflow**, Tavily usage, setup (local with Docker, and production), running tests and eval, spend guard, limitations, roadmap (HTML, SQL, Java, C++), license.

### 13.3 Demo video (under 3:00, pitch not tutorial)

1. **0:00-0:20** Problem: a famous tutorial fails today; the "Tutorial Rot" issue in NVIDIA's own repo.
2. **0:20-1:30** Live run: paste URL, steps run in Token Factory Sandboxes, failure, diagnosis with the Tavily-cited changelog, three Nemotron Ultra fixes racing, winner, clean replay, "Fixed".
3. **1:30-2:05** Report and badge; the Rot Index headline stat.
4. **2:05-2:40** How it is built: Nemotron routing table, Sandboxes checkpoint/branch, spend guard; Nebius Token Factory and Nemotron named aloud.
5. **2:40-2:55** Who it is for (learners, DevRel teams) and the call to action.

Produced with the established pipeline (Sarvam TTS narration, browser-harness screen capture, ffmpeg). The look and voice are confirmed with Arun before final render. Royalty-free or no music; no third-party trademarks beyond factual mentions.

### 13.4 Devpost submission

Title, tagline, description (what, why, how, Nemotron and Token Factory usage), track = Coding and Agentic Engineering, Built With tags (Nebius Token Factory, NVIDIA Nemotron, Tavily, Python, FastAPI, Hugging Face Spaces), demo URL, repo URL, YouTube URL, and the feedback section from `docs/feedback.md`. No IRL city event was attended, so that field is left empty.

## 14. Schedule and cut lines

| Date (2026) | Work | Exit check |
|---|---|---|
| Oct 15 | Spec, plan, accounts: billing (Arun), promo applied, API key, Sandboxes beta request, Tavily key | `whoami` and one Nemotron call succeed |
| Oct 16 | Scaffold, config, llm, ledger, sandbox interface, DockerBackend, Contree smoke test | Contract tests pass on Docker |
| Oct 17-18 | ingest, planner, executor, cell runner, events/SSE | 2 still-working fixtures reach "Still works" |
| Oct 19-20 | triage, research, repair race, judge, clean replay, eval | 6 or more Python fixtures "Fixed", 0 false fixes |
| Oct 21 | JavaScript support, serve steps, stand-in OpenAI server | JS fixtures pass eval bar |
| Oct 22-23 | UI: home, live run, report, badge, OG image | Manual QA pass |
| Oct 24-25 | Rot Index curation, batch run, board page, review; Devpost draft | Board published with 25 or more hand-checked entries |
| Oct 26 | Deploy Space, keep-alive, README, record video | Hosted URL passes QA from a clean browser |
| Oct 27 | Submit on Devpost | Submission confirmation |
| Oct 28-30 | Buffer | - |

**Cut order if behind:**

1. OG image (use a static card)
2. Rot Index down to 50 entries
3. JavaScript serve steps
4. Nano judge (exit code and stdout checks only)
5. JavaScript entirely (Python-only submission)

Clean replay and the spend guard are never cut.

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Sandboxes beta access delayed | Request on day 1; DockerBackend from day 2; Oracle VM fallback decision on Oct 20 |
| Card charged by overage | Code-enforced ledger caps well under credits; demo rate limits; pre-run examples; weekly balance checks; card-removal reminder |
| Model output schema drift | JSON-schema mode, pydantic validation, one retry with the error, golden tests on prompts |
| False "Fixed" verdicts | Mandatory clean replay; eval fails on any false fix; hand review of Rot Index |
| Fix changes the tutorial's meaning | Repair prompt forbids changing intent; Super summary flags semantic changes; such entries are downgraded |
| Tavily credits run out | Query cache, per-run cap, lifetime cap; free tier plus Builders credits |
| HF Space sleeps during judging | 12-hourly keep-alive cron; pre-run examples load from committed JSON |
| Copyright on third-party tutorials | Diff-only reports, short excerpts, always link to the original |
| Token Factory model IDs or prices change | IDs listed from `/v1/models` and pinned; prices in config; ledger uses pinned list prices |

## 16. External dependencies still pending

- Token Factory billing details (Arun, in progress), then promo code redemption.
- Sandboxes beta access (requested after billing).
- Builders Program approval for +$25 Token Factory and +$25 Tavily credits (review within 2 business days of 2026-10-15).
- Tavily API key (free tier account; Builders credits applied when issued).
