## Inspiration

Coding tutorials rot. A guide written in 2023 breaks in 2026 because a library removed a function, renamed a package or changed a default. The learner hits an error the author never saw, searches for an hour, and often gives up. AI assistants trained on those same pages repeat the broken code. NVIDIA's own OpenShell repo has an open issue titled "Tutorial Rot". We wanted a tool that answers one question for any tutorial: does this still work today, and if not, what exactly do I change?

## What it does

Paste a Python or JavaScript tutorial link (or its Markdown). Fix My Tutorial:

1. **Plans** it: Nemotron 3 Super reads the page and turns it into ordered steps, keeping the code exactly as written.
2. **Runs** every step in a fresh, isolated sandbox, saving a checkpoint after each step. You watch the steps light up live.
3. **Diagnoses** the first failure: Nemotron 3.5 Lightning classifies the error, and Tavily finds the release note, migration guide or issue that explains it.
4. **Repairs** it: Nemotron 3 Ultra proposes up to three fixes (migrate to the new API, pin an older version, or work around it), each citing its evidence. The fixes race in parallel sandboxes from the last good checkpoint.
5. **Proves** it: the repaired tutorial is replayed from scratch before it is called "Fixed". You get a report with the diff, the cited reasons and a badge.
6. **Dates** it: it finds the package release that broke the tutorial by running the broken step again with older releases, or, when those can't install on today's Python, from the cited release note. For example: "Broke in chalk 5.0.0, released 26 Nov 2021. chalk 4.1.2 still runs this step, and 5.0.0 fails the same way."

From the report, one click opens a pre-filled GitHub issue for the tutorial's author (filed from your own account), or copies the same note for any other site. A "Check this tutorial" bookmark checks a tutorial from its own page.

## How we built it

One Python service (FastAPI, asyncio, Server-Sent Events) with a typed pipeline: ingest, planner, executor, triage, research, repair, judge and report. Every model call goes to Nebius Token Factory through one OpenAI-compatible client with JSON-schema output, and the model is picked by role:

| Role | Model | Why |
|---|---|---|
| planner, summary | Nemotron 3 Super | long-context reading into a strict JSON plan |
| triage | Nemotron 3.5 Lightning | fast, cheap error classification |
| judge | Nemotron 3 Nano | cheap yes/no on fuzzy expectations |
| repair | Nemotron 3 Ultra | hard reasoning, only when something broke |

The expensive model only runs on failures, so a fixed tutorial costs about $0.012 in model calls. A spend guard reserves an upper-bound cost before every call and refuses anything that would cross a cap, because a real card sits behind the account.

## Challenges we ran into

Our live evaluation (13 tutorials broken by real library changes plus 2 that still work) caught the model doing things a schema can't catch: inventing output checks the page never showed, marking outdated code "not runnable", dropping the tutorial's main code block, collapsing a snippet into one line of escaped newlines, and never running a file the tutorial only mentions in prose. Each one could produce a false "still works" or a false "fixed", the worst outcomes for this product, so each became a deterministic guard with a test. The final run fixed 13 of 13 and left both working tutorials untouched.

Dating a break had its own surprises. The releases that would prove when pandas or NumPy broke (pandas 1.x, NumPy 1.23) can't even install on Python 3.12, so running alone can't answer for the most common cases. We added a targeted Tavily search for the release note and let Nemotron 3.5 Lightning read it, but it only counts when the version is a real PyPI or npm release and the quote (or the note's own address) states it. Lightning also sometimes echoed the installed-package list as JSON before its answer, which our parser picked up first; it now takes the first JSON object.

## Accomplishments that we're proud of

- Fixes are never taken on faith: every one is run, raced and replayed from scratch.
- A fix that only hides a failure (`|| true`, an empty `except`) is rejected before it runs.
- Partial checks are labelled "partly checked", never "verified".
- "When it broke" is either run or cited, never guessed, and the report says which.
- The public demo holds part of its budget until judging starts, so judges find it open.

## What we learned

Structured output guarantees the shape of an answer, not its faithfulness. Treat model-written plans and assertions as suggestions and verify them against the source.

## What's next

HTML/CSS, SQL, Java and C++ tutorials; a GitHub Action that re-runs a docs site's tutorials on every dependency release; scheduled re-checks with alerts when a published tutorial breaks.
