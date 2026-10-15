# Judging reserve, "Tell the author", check-this-page link, and "When did it break?"

Approved by Arun on 2026-10-15 (chat): notes guess + run check for the break finder, $7.50 of the demo's $15 held
until 1 December, features 4 and 6 as described. Executed inline (TDD per task), ledger in
`.superpowers/sdd/2026-10-15-fix-my-tutorial/progress.md`.

## Global constraints

- Plain user-facing copy, no em dashes, no jargon.
- Nothing here may change a run's verdict. The break finder is best effort: any error, timeout or budget stop
  inside it leaves `broke_in` empty and the run as it was.
- No new paid services. The break finder adds Nemotron 3.5 Lightning calls (role `triage`, through the ledger),
  sandbox runs, and about 2 Tavily credits only when running older releases can't settle it; registry lookups are
  free public JSON (PyPI, npm).
- Visitors file GitHub issues from their own account. The app never posts anything.
- A `?url=` link only prefills the form. It never starts a run or spends money.

## Task 1: Judging reserve

- `Settings.demo_reserve_usd = 7.5`, `Settings.demo_reserve_until = "2026-12-01"` (UTC date).
- `Ledger.public_cap_usd()`: in the public demo before `demo_reserve_until`, `cap_lifetime_usd - demo_reserve_usd`;
  otherwise `cap_lifetime_usd`. The laptop (not `public_demo`) never holds a reserve.
- `reserve_llm(public=True)` refuses a call that would cross `public_cap_usd()` (`BudgetExceeded("reserve", ...)`);
  non-public calls still use the full cap.
- `public_runs_open()` pauses at the reserve with "Public runs are paused until 1 December. You can still browse the
  Rot Index and open past reports."
- `/admin/ledger` reports `reserve_usd` and `reserve_until`.
- Tests (`tests/test_ledger.py`): `test_demo_holds_the_reserve_until_december`,
  `test_reserve_opens_on_the_unlock_date`, `test_laptop_has_no_reserve`, `test_public_calls_cannot_spend_the_reserve`.

## Task 2: Check-this-page link

- `GET /?url=<link>` prefills the link field when it is http(s) and at most 2,000 characters; anything else is ignored.
- If a recent report exists for that link (`store.find_recent_run`), the home page links it above the form.
- The home page offers a "Check this tutorial" bookmark: `javascript:location.href='<base>/?url='+encodeURIComponent(location.href)`.
- Tests (`tests/test_web.py`): `test_url_query_prefills_the_form_without_starting_a_run`,
  `test_url_query_ignores_non_web_links`, `test_url_query_links_a_recent_report`, `test_home_offers_the_bookmark`.

## Task 3: Tell the author

- New `src/fixmytutorial/author.py`:
  - `github_repo_for(url) -> tuple[str, str] | None` for github.com/o/r, raw.githubusercontent.com/o/r,
    o.github.io/r (root site -> o/o.github.io), colab.research.google.com/github/o/r, nbviewer.org/github/o/r
    (and nbviewer.jupyter.org). Owner and repo must match `[A-Za-z0-9_.-]{1,100}`; GitHub's own pages
    (orgs, topics, settings, ...) are not repos.
  - `author_note(rec, report_url) -> tuple[str, str]`: issue title and Markdown body (what broke, when it broke,
    the change with reasons and sources, or what we tried, and the report link).
  - `issue_url(owner, repo, title, body, limit=7000) -> str`: github.com/o/r/issues/new?title=&body=, shortening
    the body on a line boundary (always keeping the report link) so the URL stays under `limit`.
- Report page, for Fixed and Could not fix: "Tell the author" card with "Open an issue on o/r" (when on GitHub) and
  "Copy note for the author" (always).
- Tests (`tests/test_author.py`, `tests/test_web.py`): repo detection table, note contents for fixed and
  could-not-fix runs, URL length bound with the report link kept, report page shows the issue link only for GitHub
  tutorials and the card only for Fixed / Could not fix.

## Task 4: Publish date at ingest

- `published_date(html) -> str | None` in `ingest.py`: `article:published_time`, `datePublished` (meta, JSON-LD),
  `<meta name="date|publish-date|pubdate">`, first `<time datetime>`. ISO `YYYY-MM-DD`, years 1995..today.
- `TutorialDoc.published` is set for HTML pages; `RunRecord.published` copies it in the pipeline.
- Tests (`tests/test_ingest.py`): each source format, garbage dates ignored, non-HTML sources give None.

## Task 5: Break finder core (`src/fixmytutorial/breakage.py`)

- Models (in `models.py`): `BreakPoint(package, first_bad, first_bad_date, last_good=None, last_good_date=None,
  how: "ran" | "release_notes", evidence_id=None, step_id)`; `RunRecord.broke_in: BreakPoint | None`;
  `BreakGuess(package="", version="", evidence_id="")` for the model reply.
- Eligible failures: category in removed_api, renamed_package, changed_cli, version_conflict; the failing step is
  not the first unit and is not itself an install step.
- `installed_versions(language, text) -> dict[str, str]` parses `pip list --format=freeze` / `npm ls --depth=0`.
- `fetch_releases(language, package, http) -> list[Release]`: PyPI JSON / npm registry document, final releases
  only (no pre-releases, yanked or empty), ascending by version, with the first upload date.
- Guess: Lightning names the release from the cited sources. Accepted only if the package is installed, the version
  is a known release no newer than the installed one, and the cited quote contains that version. Otherwise the
  fallback guess is the first release of the installed major series (minor series for 0.x).
- Probe: insert `pip install -q "pkg==v"` / `npm install --no-save --no-audit --no-fund pkg@v` as a shell step
  right before the failing unit and run from the saved checkpoint through the failing unit. Outcome `pass` when the
  failing step passes, `no_install` when the install step fails, else `fail`.
- Search: probe the guess and the release before it in parallel. before passes + guess fails -> `ran`.
  Both pass -> binary search between the guess and the installed version (known bad) within
  `break_max_probes = 5`. Before fails -> no answer. Before cannot install -> `release_notes` only when the guess
  came from the sources, else no answer.
- `find_break(...)` never raises (returns None) and is bounded by the time it is given.
- Tests (`tests/test_breakage.py`): parsing, registry filtering (PyPI + npm via `httpx.MockTransport`), guess
  validation (unknown version, version not in quote, newer than installed, not installed), confirmed by running,
  binary search, contradiction gives None, old version not installable gives release_notes, fallback guess, not
  eligible, never raises.

## Task 6: Pipeline, live page and report

- `_repair_until_clean` remembers the first eligible failure (plan, trace, step, signature, evidence, versions).
- After a Fixed or Could not fix verdict, `_run` emits stage `dating`, runs `find_break` within
  `min(break_finder_s=240, time left - 45 s)`, stores `record.broke_in`, emits `{"type": "broke_in", ...}`, and
  passes it to the summary facts.
- `run.js`: stage label "Finding the release that broke it"; the diagnosis panel shows the result.
- `report.py`: `broke_in_text(rec) -> str` ("Broke in pandas 2.0.0, released 3 Apr 2023. pandas 1.5.3 still runs
  this step." plus "Published Mar 2022, so it worked for about 13 months." or "That release came out before this
  page was published, so it was already out of date then."); shown on the report and used in the author note.
- Tests (`tests/test_pipeline.py`, `tests/test_report.py`, `tests/test_web.py`).

## Task 7: Rot Index

- `export_entries` adds `published`, `broke_in` (package, version, date, how) and fills an empty year from the
  publish date.
- Index page: "Broke in" line under each broken result, "Releases that broke the most tutorials" list, and the
  median months from publishing to breaking when at least 3 entries have both dates.
- Tests (`tests/test_rot_index.py`, `tests/test_web.py`).

## Task 8: Verify and document

- `npm run build` in `ui/` for new classes; full suite and ruff.
- Live check on Docker with two trusted fixtures (pandas append, chalk require) and record what the finder said.
- README, architecture, video script notes, deploy checklist (every homepage example opens on the Space).

## Review focus

1. A registry or model failure inside the finder must never change the verdict or crash the run.
2. A model-invented version must never be shown (validation against the registry and the quote).
3. Old releases that cannot install on Python 3.12 must not be reported as "broke here".
4. The GitHub issue URL must stay within browser limits and never lose the report link.
5. `?url=` must never start a run.
