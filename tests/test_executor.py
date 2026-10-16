import json

from fixmytutorial.executor import ExecState, Executor, group_units, parse_markers
from fixmytutorial.judge import Judge
from fixmytutorial.models import Expectation, ImageRef, Probe, RunPlan, Step
from tests.fakes import FakeBackend, cells_handler


def plan(*steps, language="python"):
    return RunPlan(language=language, runtime="3.12", steps=list(steps))


def S(id_, kind, content, **kw):
    return Step(id=id_, title=id_, kind=kind, content=content, **kw)


BASE = [ExecState(ImageRef(backend="fake", ref="base"))]


def make(handler, settings, events=None):
    async def emit(e):
        if events is not None:
            events.append(e)
    backend = FakeBackend(handler)
    return Executor(backend, settings, Judge(None), emit=emit), backend


def test_group_units():
    p = plan(S("s1", "shell", "a"), S("s2", "run_code", "b"), S("s3", "run_code", "c"), S("s4", "shell", "d"))
    assert [[s.id for s in u] for u in group_units(p.steps)] == [["s1"], ["s2", "s3"], ["s4"]]


def test_parse_markers():
    out = '@@FMT {"cell": "s2", "status": "start"}\nhello\n@@FMT {"cell": "s2", "status": "ok"}\n' \
          '@@FMT {"cell": "s3", "status": "start"}\nboom'
    status, slices = parse_markers(out)
    assert status == {"s2": "ok", "s3": "running"} and slices["s2"] == "hello" and slices["s3"] == "boom"


async def test_all_pass_returns_full_states(settings):
    events = []
    ex, backend = make(cells_handler(), settings, events)
    p = plan(S("s1", "shell", "pip install pandas"), S("s2", "write_file", "x=1", path="a.py"),
             S("s3", "run_code", "x=1"), S("s4", "run_code", "print(x)"))
    t = await ex.execute(p, BASE, run_id="r", public=True)
    assert t.failed_unit is None and len(t.states) == 4
    assert backend.calls[1]["files"] == {"/work/a.py": b"x=1"}
    assert backend.calls[0]["timeout_s"] == settings.install_timeout_s
    assert all(c["script"].startswith("source /opt/fmt/prelude.sh") for c in backend.calls)
    assert [e["status"] for e in events if e["step_id"] == "s4"] == ["running", "passed"]


async def test_failing_cell_stops_and_reports(settings):
    ex, _ = make(cells_handler(fail_cell="s3"), settings)
    p = plan(S("s1", "shell", "pip install x"), S("s2", "run_code", "a=1"), S("s3", "run_code", "a.nope()"),
             S("s4", "shell", "echo after"))
    t = await ex.execute(p, BASE, run_id="r", public=True)
    assert (t.failed_unit, t.failing_step_id) == (1, "s3")
    assert t.results["s3"].exit_code == 1 and "AttributeError" in t.results["s3"].stderr
    assert len(t.states) == 2 and "s4" not in t.results


async def test_timeout_marks_failure_with_key_line(settings):
    ex, _ = make(lambda script, files, image: (124, "", "", True), settings)
    t = await ex.execute(plan(S("s1", "shell", "python app.py")), BASE, run_id="r", public=True)
    assert t.failing_step_id == "s1"
    assert "timed out after" in t.results["s1"].stderr


async def test_judge_failure_marks_step_failed(settings):
    ex, _ = make(cells_handler(outputs={"s1": "41"}), settings)
    p = plan(S("s1", "run_code", "print(41)", expect=Expectation(stdout_contains=["42"])))
    t = await ex.execute(p, BASE, run_id="r", public=True)
    assert t.failing_step_id == "s1"


async def test_cells_are_cumulative_across_groups(settings):
    ex, backend = make(cells_handler(), settings)
    p = plan(S("s1", "run_code", "a=1"), S("s2", "shell", "echo mid"), S("s3", "run_code", "print(a)"))
    await ex.execute(p, BASE, run_id="r", public=True)
    last = backend.calls[-1]
    assert [c["id"] for c in json.loads(last["files"]["/opt/fmt/cells.json"])] == ["s1", "s3"]
    assert "--report-from s3" in last["script"]


async def test_serve_step_uses_probe(settings):
    ex, backend = make(lambda s, f, i: (0, "probe: GET / -> 200", ""), settings)
    p = plan(S("s1", "serve", "flask run", probe=Probe(port=5000, path="/")))
    t = await ex.execute(p, BASE, run_id="r", public=True)
    assert t.failed_unit is None
    assert "probe.py --port 5000" in backend.calls[0]["script"]


async def test_javascript_snippet_written_and_run_with_node(settings):
    ex, backend = make(lambda s, f, i: (0, "hi", ""), settings)
    p = plan(S("s1", "run_code", "import x from 'y'\nconsole.log(1)"), language="javascript")
    await ex.execute(p, BASE, run_id="r", public=True)
    assert "/work/.fmt/s1.mjs" in backend.calls[0]["files"] and "node /work/.fmt/s1.mjs" in backend.calls[0]["script"]


async def test_resume_from_unit_uses_given_state(settings):
    ex, backend = make(cells_handler(), settings)
    p = plan(S("s1", "shell", "a"), S("s2", "shell", "b"))
    states = [BASE[0], ExecState(ImageRef(backend="fake", ref="after-s1"))]
    t = await ex.execute(p, states, from_unit=1, run_id="r", public=True)
    assert backend.calls[0]["image"] == "after-s1" and t.failed_unit is None


async def test_quiet_mode_emits_nothing(settings):
    events = []
    ex, _ = make(cells_handler(), settings, events)
    await ex.execute(plan(S("s1", "shell", "a")), BASE, run_id="r", public=True, quiet=True)
    assert events == []


async def test_shell_steps_fail_when_any_command_fails(settings):
    ex, backend = make(lambda s, f, i: (0, "", ""), settings)
    await ex.execute(plan(S("s1", "shell", "curl -fsSL https://dead.example/install.sh | bash")), BASE,
                     run_id="r", public=True)
    script = backend.calls[0]["script"]
    assert "set -eo pipefail" in script and script.index("set -eo pipefail") < script.index("curl")


def test_bash_really_fails_a_dead_pipe_with_these_options():
    import subprocess
    r = subprocess.run(["bash", "-c", "set -eo pipefail\nfalse | cat\necho reached"], capture_output=True, text=True)
    assert r.returncode != 0 and "reached" not in r.stdout


async def test_cell_groups_leave_time_for_repair(settings):
    ex, backend = make(cells_handler(), settings)
    cells = [S(f"s{i}", "run_code", f"x{i} = {i}") for i in range(1, 12)]
    await ex.execute(plan(*cells), BASE, run_id="r", public=True)
    assert max(c["timeout_s"] for c in backend.calls) <= 2 * settings.step_timeout_s
