import asyncio
import json
from datetime import date

import httpx
import pytest

from fixmytutorial.breakage import (
    BreakTarget,
    Registry,
    Release,
    eligible,
    find_break,
    installed_versions,
    make_probe,
)
from fixmytutorial.executor import Executor
from fixmytutorial.judge import Judge
from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from fixmytutorial.models import Evidence, FailureSignature, RunPlan, Step
from tests.fakes import FakeBackend, FakeChatClient

FREEZE = "numpy==2.1.0\npandas==2.2.3\npython-dateutil==2.9.0\n"
NPM_LS = "work@ /work\n├── @scope/pkg@1.2.3\n└── chalk@5.3.0\n"
REL = [
    Release(version=v, date=date.fromisoformat(d))
    for v, d in [
        ("1.5.3", "2023-01-19"),
        ("2.0.0", "2023-04-03"),
        ("2.0.1", "2023-04-24"),
        ("2.0.2", "2023-05-29"),
        ("2.0.3", "2023-06-28"),
        ("2.1.0", "2023-08-30"),
        ("2.2.3", "2024-09-20"),
    ]
]
EVID = [
    Evidence(
        id="r1e1",
        url="https://pandas.pydata.org/whatsnew/v2.0.0",
        title="What's new in 2.0.0",
        quote="DataFrame.append was removed in pandas 2.0.0. Use pandas.concat instead.",
    )
]
SIG = FailureSignature(
    category="removed_api",
    error_type="AttributeError",
    package="pandas",
    symbol="DataFrame.append",
    key_line="AttributeError: 'DataFrame' object has no attribute 'append'",
)
GUESS = json.dumps({"package": "pandas", "version": "2.0.0", "evidence_id": "r1e1"})
PLAN = RunPlan(
    language="python",
    runtime="3.12",
    steps=[
        Step(id="s1", title="Install", kind="shell", content="pip install pandas"),
        Step(id="s2", title="Frame", kind="run_code", content="import pandas as pd\ndf = pd.DataFrame()"),
        Step(id="s3", title="Append", kind="run_code", content="df = df.append({'a': 1}, ignore_index=True)"),
    ],
)


def test_installed_versions():
    assert installed_versions("python", FREEZE) == {
        "numpy": "2.1.0",
        "pandas": "2.2.3",
        "python-dateutil": "2.9.0",
    }
    assert installed_versions("javascript", NPM_LS) == {"@scope/pkg": "1.2.3", "chalk": "5.3.0"}


PYPI = {
    "releases": {
        "1.5.2": [{"upload_time_iso_8601": "2022-11-22T10:00:00Z", "yanked": False}],
        "1.5.3": [
            {"upload_time_iso_8601": "2023-01-20T10:00:00Z", "yanked": False},
            {"upload_time_iso_8601": "2023-01-19T10:00:00Z", "yanked": False},
        ],
        "2.0.0rc0": [{"upload_time_iso_8601": "2023-02-01T00:00:00Z", "yanked": False}],
        "2.0.0": [{"upload_time_iso_8601": "2023-04-03T10:00:00Z", "yanked": False}],
        "2.0.1": [{"upload_time_iso_8601": "2023-04-24T10:00:00Z", "yanked": True}],
        "2.0.2": [],
        "2.1.0": [{"upload_time_iso_8601": "2023-08-30T10:00:00Z", "yanked": False}],
        "not-a-version": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z", "yanked": False}],
    }
}
NPM = {
    "versions": {"4.1.2": {}, "5.0.0": {}, "5.0.0-beta.1": {}, "5.3.0": {}},
    "time": {
        "created": "2013-08-03T00:00:00Z",
        "modified": "2024-01-01T00:00:00Z",
        "4.1.2": "2021-07-30T12:00:00Z",
        "5.0.0": "2021-11-26T10:00:00Z",
        "5.0.0-beta.1": "2021-11-01T00:00:00Z",
        "5.3.0": "2023-06-29T00:00:00Z",
    },
}


async def test_pypi_releases_are_final_dated_and_sorted():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=PYPI)

    rel = await Registry(httpx.AsyncClient(transport=httpx.MockTransport(handler))).releases(
        "python", "pandas"
    )
    assert [r.version for r in rel] == ["1.5.2", "1.5.3", "2.0.0", "2.1.0"]
    assert rel[1].date == date(2023, 1, 19) and seen == ["https://pypi.org/pypi/pandas/json"]


async def test_npm_releases_and_scoped_names():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=NPM)

    reg = Registry(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rel = await reg.releases("javascript", "chalk")
    assert [r.version for r in rel] == ["4.1.2", "5.0.0", "5.3.0"] and rel[1].date == date(2021, 11, 26)
    await reg.releases("javascript", "@scope/pkg")
    assert seen == ["https://registry.npmjs.org/chalk", "https://registry.npmjs.org/@scope%2Fpkg"]


async def test_registry_never_asks_for_odd_package_names():
    def handler(req):
        raise AssertionError("no request expected")

    reg = Registry(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reg.releases("python", "../etc") == [] and await reg.releases("javascript", "a b") == []


class FakeRegistry:
    def __init__(self, rel=REL, error=None):
        self.rel, self.error, self.calls = rel, error, []

    async def releases(self, language, package):
        self.calls.append(package)
        if self.error:
            raise self.error
        return list(self.rel)


def target(evidence=EVID, versions=FREEZE, sig=SIG):
    return BreakTarget(
        plan=PLAN, trace=None, step_id="s3", signature=sig, evidence=evidence, versions=versions
    )


async def find(
    settings,
    tmp_path,
    outcomes,
    reply: str | Exception = GUESS,
    registry=None,
    time_s: float = 30,
    search=None,
    **kw,
):
    calls = []

    async def probe(version):
        calls.append(version)
        out = outcomes[version]
        if out == "hang":
            await asyncio.sleep(60)
        return out

    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([reply]))
    bp = await find_break(
        target(**kw),
        language="python",
        llm=llm,
        registry=registry or FakeRegistry(),
        probe_for=lambda package: probe,
        max_probes=6,
        time_s=time_s,
        run_id="r",
        public=True,
        search=search,
    )
    return bp, calls


async def test_the_named_release_is_confirmed_by_running(settings, tmp_path):
    bp, calls = await find(settings, tmp_path, {"1.5.3": "pass", "2.0.0": "same"})
    assert (bp.package, bp.first_bad, bp.last_good, bp.how, bp.evidence_id) == (
        "pandas",
        "2.0.0",
        "1.5.3",
        "ran",
        "r1e1",
    )
    assert (
        bp.first_bad_date == date(2023, 4, 3)
        and bp.last_good_date == date(2023, 1, 19)
        and bp.step_id == "s3"
    )
    assert sorted(calls) == ["1.5.3", "2.0.0"]


async def test_a_wrong_guess_is_corrected_by_a_binary_search(settings, tmp_path):
    outcomes = {
        "1.5.3": "pass",
        "2.0.0": "pass",
        "2.0.1": "pass",
        "2.0.2": "same",
        "2.0.3": "same",
        "2.1.0": "same",
    }
    bp, calls = await find(settings, tmp_path, outcomes)
    assert (bp.first_bad, bp.last_good, bp.how, bp.evidence_id) == ("2.0.2", "2.0.1", "ran", None)
    assert len(calls) <= 6


async def test_an_old_release_that_cannot_install_falls_back_to_the_release_notes(settings, tmp_path):
    bp, _ = await find(settings, tmp_path, {"1.5.3": "no_install", "2.0.0": "same"})
    assert (bp.first_bad, bp.last_good, bp.how, bp.evidence_id) == ("2.0.0", None, "release_notes", "r1e1")


async def test_a_cited_deprecation_is_corrected_by_newer_releases_that_still_run(settings, tmp_path):
    # the review, 16 Oct: "sparse was renamed to sparse_output in 1.2" was dated 1.2.0, but 1.3.2 still ran the
    # tutorial; it broke when the old name was removed in 1.4.0. Releases before the cited one couldn't install.
    outcomes = {"1.5.3": "no_install", "2.0.0": "no_install", "2.0.1": "pass", "2.0.2": "same", "2.0.3": "same",
                "2.1.0": "same"}
    bp, calls = await find(settings, tmp_path, outcomes)
    assert (bp.first_bad, bp.last_good, bp.how) == ("2.0.2", "2.0.1", "ran")
    assert len(calls) <= 6


async def test_a_cited_version_newer_releases_agree_with_keeps_the_release_notes(settings, tmp_path):
    outcomes = {"1.5.3": "no_install", "2.0.0": "no_install", "2.0.1": "same"}
    bp, calls = await find(settings, tmp_path, outcomes)
    assert (bp.first_bad, bp.how, bp.evidence_id) == ("2.0.0", "release_notes", "r1e1")
    assert "2.0.1" in calls


async def test_no_answer_when_the_earlier_release_fails_the_same_way(settings, tmp_path):
    bp, _ = await find(settings, tmp_path, {"1.5.3": "same", "2.0.0": "same"})
    assert bp is None


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps({"package": "pandas", "version": "9.9.9", "evidence_id": "r1e1"}),  # not a real release
        json.dumps(
            {"package": "pandas", "version": "2.0.0", "evidence_id": "nope"}
        ),  # cites no source we found
        json.dumps({"package": "pandas", "version": "", "evidence_id": ""}),  # the sources don't say
    ],
)
async def test_versions_the_sources_do_not_state_are_never_shown(settings, tmp_path, reply):
    bp, _ = await find(settings, tmp_path, {"1.5.3": "no_install", "2.0.0": "same"}, reply=reply)
    assert bp is None  # untestable here, and no source to fall back on


async def test_a_version_the_source_never_states_is_not_trusted(settings, tmp_path):
    evidence = [
        EVID[0].model_copy(
            update={
                "url": "https://stackoverflow.com/q/123",
                "quote": "DataFrame.append was removed. Use pandas.concat instead.",
            }
        )
    ]
    bp, _ = await find(settings, tmp_path, {"1.5.3": "no_install", "2.0.0": "same"}, evidence=evidence)
    assert bp is None


async def test_a_version_stated_by_the_release_note_address_is_trusted(settings, tmp_path):
    evidence = [
        EVID[0].model_copy(update={"quote": "DataFrame.append was removed. Use pandas.concat instead."})
    ]
    bp, _ = await find(settings, tmp_path, {"1.5.3": "no_install", "2.0.0": "same"}, evidence=evidence)
    assert (bp.first_bad, bp.how, bp.source.url) == (
        "2.0.0",
        "release_notes",
        "https://pandas.pydata.org/whatsnew/v2.0.0",
    )


async def test_without_release_notes_the_start_of_the_major_series_is_tried(settings, tmp_path):
    reply = json.dumps({"package": "pandas", "version": "", "evidence_id": ""})
    bp, calls = await find(settings, tmp_path, {"1.5.3": "pass", "2.0.0": "same"}, reply=reply)
    assert (bp.first_bad, bp.last_good, bp.how, bp.evidence_id) == ("2.0.0", "1.5.3", "ran", None)


async def test_a_package_that_is_not_installed_is_ignored(settings, tmp_path):
    reply = json.dumps({"package": "polars", "version": "1.0.0", "evidence_id": "r1e1"})
    registry = FakeRegistry()
    bp, calls = await find(
        settings, tmp_path, {}, reply=reply, registry=registry, sig=SIG.model_copy(update={"package": None})
    )
    assert bp is None and registry.calls == [] and calls == []


async def test_the_installed_release_is_known_bad_without_running_it(settings, tmp_path):
    bp, calls = await find(settings, tmp_path, {"1.5.3": "pass"}, versions="pandas==2.0.0\n")
    assert (bp.first_bad, bp.last_good, bp.how) == ("2.0.0", "1.5.3", "ran") and calls == ["1.5.3"]


async def test_registry_and_model_errors_never_escape(settings, tmp_path):
    bp, _ = await find(settings, tmp_path, {}, registry=FakeRegistry(error=httpx.ConnectError("down")))
    assert bp is None
    bp, _ = await find(
        settings, tmp_path, {"1.5.3": "pass", "2.0.0": "same"}, reply=RuntimeError("model down")
    )
    assert bp is not None and bp.how == "ran"  # the guess falls back to the start of the major series


async def test_running_out_of_time_keeps_the_release_notes_answer(settings, tmp_path):
    bp, _ = await find(settings, tmp_path, {"1.5.3": "hang", "2.0.0": "hang"}, time_s=0.2)
    assert (bp.first_bad, bp.how, bp.evidence_id) == ("2.0.0", "release_notes", "r1e1")


def _trace(failed_unit, failing):
    from fixmytutorial.executor import Trace

    return Trace(plan=PLAN, states=[], results={}, failed_unit=failed_unit, failing_step_id=failing)


def test_eligible_failures():
    assert eligible(PLAN, _trace(1, "s3"), SIG)
    assert not eligible(PLAN, _trace(1, "s3"), SIG.model_copy(update={"category": "dead_resource"}))
    assert not eligible(PLAN, _trace(0, "s1"), SIG)  # the install step itself
    assert not eligible(PLAN, _trace(None, None), SIG)


def probe_backend(state):
    def handler(script, files, image):
        if "pip install -q" in script:
            version = script.split("pandas==")[1].split()[0].strip("'\"")
            if version == "1.0.0":
                return (1, "", "ERROR: No matching distribution found for pandas==1.0.0")
            state["v"] = version
            return (0, "", "")
        if "run_cells.py" in script:
            cells = json.loads(files["/opt/fmt/cells.json"])
            out = []
            for c in cells:
                out.append(f'@@FMT {{"cell": "{c["id"]}", "status": "start"}}')
                if "append" in c["code"] and state.get("v", "2.2.3") >= "2":
                    out.append(f'@@FMT {{"cell": "{c["id"]}", "status": "error"}}')
                    err = (
                        "ImportError: numpy ABI mismatch"
                        if state.get("v") == "3.0.0"
                        else "AttributeError: 'DataFrame' object has no attribute 'append'"
                    )
                    return (1, "\n".join(out), "Traceback (most recent call last):\n" + err)
                out.append(f'@@FMT {{"cell": "{c["id"]}", "status": "ok"}}')
            return (0, "\n".join(out), "")
        return (0, "ok", "")

    return handler


async def test_probe_runs_the_failing_step_with_one_release_from_the_checkpoint(settings, tmp_path):
    state: dict = {}
    backend = FakeBackend(probe_backend(state))
    led = Ledger(tmp_path / "l.db", settings)
    executor = Executor(backend, settings, Judge(LLM(settings, led, client=FakeChatClient([]))))
    from fixmytutorial.executor import ExecState
    from fixmytutorial.models import ImageRef

    trace = await executor.execute(
        PLAN, [ExecState(ImageRef(backend="fake", ref="base"))], run_id="r", public=True
    )
    assert trace.failed_unit == 1 and trace.failing_step_id == "s3"
    t = BreakTarget(plan=PLAN, trace=trace, step_id="s3", signature=SIG, evidence=EVID, versions=FREEZE)
    probe = make_probe(executor, t, "python", "pandas", run_id="r", public=True)
    first = len(backend.calls)
    assert await probe("1.5.3") == "pass"
    assert backend.calls[first]["image"] == trace.states[1].image.ref  # starts from the saved checkpoint
    # wheels only: an old release with no wheel for this Python fails fast instead of compiling for minutes
    assert "pip install -q --only-binary=:all: pandas==1.5.3" in backend.calls[first]["script"]
    assert await probe("2.0.0") == "same"
    assert await probe("1.0.0") == "no_install"
    assert await probe("3.0.0") == "other"  # fails, but not with the error we are dating


OPENAI_REL = [
    Release(version=v, date=date.fromisoformat(d))
    for v, d in [
        ("0.27.0", "2023-03-01"),
        ("0.28.1", "2023-09-20"),
        ("1.0.0", "2023-11-06"),
        ("1.5.0", "2023-12-17"),
        ("2.0.0", "2025-09-30"),
        ("2.3.0", "2026-08-01"),
    ]
]


async def test_steps_back_a_series_when_the_change_is_older(settings, tmp_path):
    reply = json.dumps({"package": "pandas", "version": "", "evidence_id": ""})
    outcomes = {"1.5.0": "same", "2.0.0": "same", "0.28.1": "pass", "1.0.0": "same"}
    bp, calls = await find(
        settings,
        tmp_path,
        outcomes,
        reply=reply,
        registry=FakeRegistry(OPENAI_REL),
        versions="pandas==2.3.0\n",
    )
    assert (bp.first_bad, bp.last_good, bp.how) == ("1.0.0", "0.28.1", "ran") and len(calls) == 4


async def test_the_package_is_read_from_the_failing_step_when_nobody_names_it(settings, tmp_path):
    reply = json.dumps({"package": "", "version": "", "evidence_id": ""})
    step_plan = PLAN.model_copy(
        update={
            "steps": PLAN.steps[:2]
            + [PLAN.steps[2].model_copy(update={"content": "import pandas as pd\ndf = df.append({'a': 1})"})]
        }
    )
    calls = []

    async def probe(version):
        calls.append(version)
        return {"1.5.3": "pass", "2.0.0": "same"}[version]

    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([reply]))
    t = BreakTarget(
        plan=step_plan,
        trace=None,
        step_id="s3",
        signature=SIG.model_copy(update={"package": None}),
        evidence=EVID,
        versions=FREEZE,
    )
    bp = await find_break(
        t,
        language="python",
        llm=llm,
        registry=FakeRegistry(),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
    )
    assert bp.package == "pandas" and bp.first_bad == "2.0.0"


class FakeSearch:
    def __init__(self, results, extracted):
        self.results, self.extracted, self.calls = results, extracted, []

    async def search(self, query, *, run_id, public, max_results=5, **kw):
        self.calls.append(("search", query))
        return self.results

    async def extract(self, urls, *, run_id, public, query=None, **kw):
        self.calls.append(("extract", tuple(urls)))
        return [{"url": u, "raw_content": self.extracted[u]} for u in urls if u in self.extracted]


NOTES_URL = "https://numpy.org/doc/stable/release/1.24.0-notes.html"
NP_REL = [
    Release(version=v, date=date.fromisoformat(d))
    for v, d in [
        ("1.23.5", "2022-11-20"),
        ("1.24.0", "2022-12-18"),
        ("1.26.4", "2024-02-05"),
        ("2.0.0", "2024-06-16"),
        ("2.3.1", "2026-06-01"),
    ]
]
NP_SIG = FailureSignature(
    category="removed_api",
    error_type="AttributeError",
    package="numpy",
    symbol="numpy.float",
    key_line="AttributeError: module 'numpy' has no attribute 'float'.",
)
NP_EVID = [
    Evidence(
        id="r1e1",
        url="https://stackoverflow.com/q/1",
        title="np.float error",
        quote="np.float was a deprecated alias for the builtin float. Use float instead.",
    )
]


async def numpy_find(settings, tmp_path, replies, outcomes, search):
    async def probe(version):
        return outcomes[version]

    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient(replies))
    t = BreakTarget(
        plan=PLAN, trace=None, step_id="s3", signature=NP_SIG, evidence=NP_EVID, versions="numpy==2.3.1\n"
    )
    return await find_break(
        t,
        language="python",
        llm=llm,
        registry=FakeRegistry(NP_REL),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
        search=search,
    )


async def test_release_notes_are_searched_when_running_cannot_answer(settings, tmp_path):
    search = FakeSearch(
        [{"url": NOTES_URL, "title": "NumPy 1.24.0 Release Notes", "content": ""}],
        {
            NOTES_URL: "## Expired deprecations\n\nThe deprecation for the aliases np.object, np.bool, "
            "np.float, np.complex, np.str, and np.int is expired.\n\nOther text."
        },
    )
    empty = json.dumps({"package": "numpy", "version": "", "evidence_id": ""})
    named = json.dumps({"package": "numpy", "version": "1.24.0", "evidence_id": "b1"})
    outcomes = {"1.26.4": "same", "2.0.0": "same", "1.23.5": "no_install", "1.24.0": "same"}
    # the run's own sources state no version: read twice, then the release note is searched and read
    bp = await numpy_find(settings, tmp_path, [empty, empty, named], outcomes, search)
    assert (bp.first_bad, bp.how, bp.evidence_id) == ("1.24.0", "release_notes", "b1")
    assert bp.source.url == NOTES_URL and "np.float" in bp.source.quote
    assert [c[0] for c in search.calls] == ["search", "extract"]


async def test_no_release_notes_search_when_running_answers(settings, tmp_path):
    search = FakeSearch([], {})
    bp, _ = await find(
        settings,
        tmp_path,
        {"1.5.3": "pass", "2.0.0": "same"},
        reply=json.dumps({"package": "pandas", "version": "", "evidence_id": ""}),
        search=search,
    )
    assert bp.how == "ran" and search.calls == []


async def test_a_searched_version_that_running_contradicts_is_not_shown(settings, tmp_path):
    search = FakeSearch(
        [{"url": NOTES_URL, "title": "NumPy 1.24.0 Release Notes", "content": ""}],
        {NOTES_URL: "The deprecation for np.float is expired in 2.0.0."},
    )
    empty = json.dumps({"package": "numpy", "version": "", "evidence_id": ""})
    named = json.dumps({"package": "numpy", "version": "2.0.0", "evidence_id": "b1"})
    outcomes = {"1.26.4": "same", "2.0.0": "same", "1.23.5": "no_install", "1.24.0": "same"}
    assert (
        await numpy_find(settings, tmp_path, [empty, named], outcomes, search) is None
    )  # 1.26.4 already fails


async def test_a_probe_error_counts_as_not_runnable_and_keeps_the_other_result(settings, tmp_path):
    calls = []

    async def probe(version):
        calls.append(version)
        if version == "1.5.3":
            raise RuntimeError("docker create failed")
        return "same"

    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([GUESS]))
    bp = await find_break(
        target(),
        language="python",
        llm=llm,
        registry=FakeRegistry(),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
    )
    assert (bp.first_bad, bp.how) == ("2.0.0", "release_notes")


async def test_a_budget_stop_in_one_probe_cancels_the_other(settings, tmp_path):
    from fixmytutorial.ledger import BudgetExceeded

    finished = []

    async def probe(version):
        if version == "1.5.3":
            raise BudgetExceeded("run", "This run reached its spending limit.")
        await asyncio.sleep(0.3)
        finished.append(version)
        return "same"

    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([GUESS]))
    await find_break(
        target(),
        language="python",
        llm=llm,
        registry=FakeRegistry(),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
    )
    await asyncio.sleep(0.5)
    assert finished == []  # nothing keeps running (or spending) after the finder returns


async def test_probe_stops_at_the_broken_cell_of_a_cell_group(settings, tmp_path):
    from fixmytutorial.executor import ExecState
    from fixmytutorial.models import ImageRef

    plan = PLAN.model_copy(
        update={
            "steps": PLAN.steps
            + [
                Step(
                    id="s4",
                    title="Later",
                    kind="run_code",
                    content="raise SystemExit('a later cell that always fails')",
                )
            ]
        }
    )
    state: dict = {}
    payloads = []
    inner = probe_backend(state)

    def handler(script, files, image):
        if "run_cells.py" in script:
            payloads.append([c["id"] for c in json.loads(files["/opt/fmt/cells.json"])])
        return inner(script, files, image)

    backend = FakeBackend(handler)
    executor = Executor(
        backend,
        settings,
        Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([]))),
    )
    trace = await executor.execute(
        plan, [ExecState(ImageRef(backend="fake", ref="base"))], run_id="r", public=True
    )
    t = BreakTarget(plan=plan, trace=trace, step_id="s3", signature=SIG, evidence=EVID, versions=FREEZE)
    assert await make_probe(executor, t, "python", "pandas", run_id="r", public=True)("1.5.3") == "pass"
    assert payloads[-1] == ["s2", "s3"]  # cells after the broken one are not run


async def test_the_package_is_read_from_the_file_the_failing_step_runs(settings, tmp_path):
    js_plan = RunPlan(
        language="javascript",
        runtime="22",
        steps=[
            Step(id="s1", title="Install", kind="shell", content="npm install chalk express"),
            Step(
                id="s2",
                title="Write",
                kind="write_file",
                path="index.js",
                content="const chalk = require('chalk');\nconsole.log(chalk.red('hi'));",
            ),
            Step(id="s3", title="Run", kind="shell", content="node index.js"),
        ],
    )
    calls = []

    async def probe(version):
        calls.append(version)
        return {"4.1.2": "pass", "5.0.0": "same"}[version]

    rel = [
        Release(version="4.1.2", date=date(2021, 7, 30)),
        Release(version="5.0.0", date=date(2021, 11, 26)),
        Release(version="5.6.2", date=date(2025, 9, 8)),
    ]
    llm = LLM(
        settings,
        Ledger(tmp_path / "l.db", settings),
        client=FakeChatClient([json.dumps({"package": "", "version": "", "evidence_id": ""})]),
    )
    sig = FailureSignature(
        category="removed_api", error_type="TypeError", key_line="TypeError: chalk.red is not a function"
    )
    t = BreakTarget(
        plan=js_plan,
        trace=None,
        step_id="s3",
        signature=sig,
        evidence=EVID,
        versions="work@ /work\n+-- chalk@5.6.2\n`-- express@4.21.2\n",
    )
    bp = await find_break(
        t,
        language="javascript",
        llm=llm,
        registry=FakeRegistry(rel),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
    )
    assert (bp.package, bp.first_bad, bp.last_good, bp.how) == ("chalk", "5.0.0", "4.1.2", "ran")


def test_version_matching_is_strict():
    from fixmytutorial.breakage import _mentions

    assert not _mentions("Step 2: install pandas", "2")  # a bare number states no version
    assert not _mentions("Try 2.0.0rc0 first", "2.0.0")
    assert _mentions("removed in pandas 2.0.", "2.0") and _mentions("removed in 2.0.0 (April 2023)", "2.0.0")
    from fixmytutorial.breakage import _address

    assert _mentions(_address("https://pandas.pydata.org/docs/whatsnew/v2.0.0.html"), "2.0.0")
    assert _mentions(_address("https://numpy.org/doc/stable/release/1.24.0-notes.html"), "1.24.0")


async def test_the_runs_own_sources_are_read_a_second_time_when_the_first_read_passes(settings, tmp_path):
    # the live demo, 16 Oct: pandas' v2.0.0 what's-new page was a source, Lightning passed on it once (it names 2.0.0
    # two times in three), old pandas can't install on Python 3.12, and the report lost "When it broke"
    empty = json.dumps({"package": "", "version": "", "evidence_id": ""})
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([empty, GUESS]))

    async def probe(version):
        return "no_install"

    bp = await find_break(target(), language="python", llm=llm, registry=FakeRegistry(), probe_for=lambda p: probe,
                          max_probes=6, time_s=30, run_id="r", public=True, search=None)
    assert (bp.first_bad, bp.how, bp.evidence_id) == ("2.0.0", "release_notes", "r1e1")


async def test_the_release_notes_are_read_a_second_time_when_the_first_read_passes(settings, tmp_path):
    search = FakeSearch(
        [{"url": "https://ex.com/a", "title": "append removed", "content": ""}],
        {"https://ex.com/a": "As of Pandas version 2.0.0, the append() method has been removed."},
    )
    empty = json.dumps({"package": "", "version": "", "evidence_id": ""})
    named = json.dumps({"package": "", "version": "2.0.0", "evidence_id": "b1"})
    calls = []

    async def probe(version):
        calls.append(version)
        return "no_install"

    llm = LLM(
        settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([empty, named])
    )  # no evidence yet: the first guess needs no call
    t = BreakTarget(plan=PLAN, trace=None, step_id="s3", signature=SIG, evidence=EVID[:0], versions=FREEZE)
    bp = await find_break(
        t,
        language="python",
        llm=llm,
        registry=FakeRegistry(),
        probe_for=lambda p: probe,
        max_probes=6,
        time_s=30,
        run_id="r",
        public=True,
        search=search,
    )
    assert (bp.first_bad, bp.how, bp.evidence_id) == ("2.0.0", "release_notes", "b1")
