# Fix My Tutorial

Run any coding tutorial, find what broke, and fix it with sources you can check.

[![CI](https://github.com/iamarunbrahma/fix-my-tutorial/actions/workflows/ci.yml/badge.svg)](https://github.com/iamarunbrahma/fix-my-tutorial/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Why

Coding tutorials rot. A guide written in 2023 breaks in 2026 because a library removed a function, renamed a package or changed a default, and learners hit an error the author never saw. AI assistants trained on those same pages repeat the broken code. NVIDIA's own OpenShell repo has an open issue titled ["Tutorial Rot"](https://github.com/NVIDIA/OpenShell/issues/2998).

Fix My Tutorial runs the tutorial exactly as written in a fresh sandbox, and when a step breaks it finds the changelog or issue that explains why, races several fixes in parallel, and proves the winner with a clean re-run from scratch.

## How it works

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

1. **Plan.** Nemotron 3 Super reads the page and turns it into ordered steps: shell commands, files to write, code cells, and servers to probe. The code is kept exactly as written, because running the original is how breakage is found.
2. **Run.** Each step runs in an isolated sandbox, with a checkpoint saved after each step. Python cells share one namespace, so notebook-style tutorials keep their variables.
3. **Diagnose.** When a step fails, Nemotron 3.5 Lightning classifies the error (removed API, renamed package, version conflict and so on), and Tavily finds the release note, migration guide or issue that explains it.
4. **Repair.** Nemotron 3 Ultra proposes up to three fixes (migrate to the new API, pin an older version, or work around it). When evidence was found, each fix must cite it. The fixes race in parallel sandboxes from the last good checkpoint.
5. **Prove it.** The repaired tutorial is replayed from scratch before it is called fixed. The report shows the diff, the cited reasons, and a badge.

| Role | Model | Price per 1M tokens (in / out) | Why this size |
|---|---|---|---|
| planner | `nvidia/nemotron-3-super-120b-a12b` | $0.30 / $0.90 | Long-context reading of a whole tutorial into a strict JSON plan |
| triage | `nvidia/Nemotron-3_5-Lightning` | $0.06 / $0.24 | Fast, cheap classification of an error log |
| judge | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | $0.06 / $0.24 | Cheap yes/no on fuzzy expectations ("prints the accuracy") |
| repair | `nvidia/Nemotron-3-Ultra-550b-a55b` | $1.00 / $3.00 | Hard reasoning over the error, the code and the evidence; only runs on failures |
| summary | `nvidia/nemotron-3-super-120b-a12b` | $0.30 / $0.90 | One plain-language paragraph about the run |

More detail: [docs/architecture.md](docs/architecture.md).

## How we use NVIDIA Nemotron

Four Nemotron sizes split the work so the expensive model only runs when something is actually broken:

- **Nemotron 3 Super** plans every run (about $0.0013 per plan) and writes the summary (about $0.0004).
- **Nemotron 3.5 Lightning** triages each failure (about $0.0002 per call).
- **Nemotron 3 Nano** judges fuzzy expectations (about $0.00004 per call); exact checks never reach a model.
- **Nemotron 3 Ultra** writes the fixes (about $0.01 per repair round) and is the bulk of the cost.

Every call uses JSON-schema structured output, validated with pydantic.

**Live evaluation** ([docs/eval-results.md](docs/eval-results.md)): 15 fixture tutorials, 13 broken by real library changes (pandas `append` and `iteritems`, NumPy `np.float`, scikit-learn `load_boston` and `OneHotEncoder(sparse=)`, openai 0.x, LangChain `langchain.llms`, Flask `before_first_request`, seaborn styles, chalk and node-fetch ESM, Express 5 wildcards, faker) and 2 that still work. On the final run, all 13 broken tutorials were fixed in the first repair round, and both working tutorials were left untouched. A fixed tutorial cost about $0.011 in model calls on average and took about a minute.

Earlier runs during the build scored 12, 11, 13 and 11 out of 13. Each miss traced back to a gap we then closed in code: the planner inventing output checks, marking outdated code as "not runnable" or dropping the tutorial's code block, and our judge rejecting the stand-in API server's placeholder reply. The model findings are in [docs/feedback.md](docs/feedback.md).

## Where Nebius Token Factory accelerated the work

- **One OpenAI-compatible API for four model sizes.** Routing is a lookup from role to model ID; the client, retries and spend accounting are shared.
- **JSON-schema output** made every stage a typed function. The planner, triage, judge and repair stages return validated pydantic objects, not text to parse.
- **Sandboxes (Contree) checkpoints and branches** fit the repair race directly: save an image after each step, then start several candidate fixes from the same image at once.

## Tavily

Research uses Tavily search, then Tavily extract with a focused query on the top results, so each piece of evidence is a short quote from a real page, with its URL. Quotes that never mention the failing symbol are dropped. Results are cached for 7 days, so tutorials that share a root cause (several pandas tutorials hitting `append`, say) reuse the same evidence. A repaired step typically uses 2 to 4 credits, and a run is capped at 30.

## Run it locally

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, a Nebius Token Factory key and a Tavily key.

```bash
uv sync
cp .env.example .env        # fill in NEBIUS_API_KEY, TAVILY_API_KEY and ADMIN_TOKEN; set SANDBOX_BACKEND=docker
uv run uvicorn fixmytutorial.web.app:app --reload
```

The Docker sandbox refuses to run until a probe from inside a container shows that private addresses (your LAN, the host, cloud metadata) are unreachable. On a Linux host, set up the firewalled network in [docs/deploy.md](docs/deploy.md). On a laptop, and only for tutorials you trust, set `DOCKER_ALLOW_PRIVATE_EGRESS=true` to skip the probe.

```bash
uv run pytest                       # offline tests with fakes, no API calls
uv run python -m fixmytutorial.eval # live evaluation over the fixture tutorials (uses real credits)
```

## Spend guard

A bank card sits behind the Token Factory account, so the app is built to never spend past its credits:

- Every model and Tavily call goes through a ledger that reserves an upper-bound cost first, and refuses the call if it would cross a cap.
- Caps: $28 lifetime, $3 a day, $0.50 per run, 30 Tavily credits per run and 5,000 lifetime.
- Calls that fail after they may have been billed are charged at their upper bound.
- If the ledger can't be restored after a restart, public runs pause instead of starting from zero.
- Public runs are limited to 3 per hour per visitor.

## Limitations

- CPU only. Tutorials that need a GPU, a database server, a GUI or a paid API are reported as "not runnable".
- Python and JavaScript only.
- Code that calls OpenAI-style APIs talks to a stand-in server inside the sandbox. It proves the SDK calls are correct today, not what a real model would answer.
- Fixes are checked by running the tutorial, not by reading it. A step with no visible output passes if it exits cleanly.

## Roadmap

- HTML and CSS, SQL, Java and C++ tutorials.
- A GitHub Action for docs teams that re-runs their tutorials on every dependency release.
- Scheduled re-checks of published tutorials, with alerts when one breaks.

## Built during the hackathon

All code was written between 2026-10-15 and 2026-10-27 for the Nebius x NVIDIA Global AI Hackathon.

## License

MIT. See [LICENSE](LICENSE).
