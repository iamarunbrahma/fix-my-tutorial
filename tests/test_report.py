import io

from PIL import Image

from fixmytutorial.models import (
    Candidate, Edit, Evidence, FailureSignature, RepairRecord, RunPlan, RunRecord, RunStatus, Step, Verdict, utcnow,
)
from fixmytutorial.report import badge_svg, build_changes, diff_lines, og_png

EV = Evidence(id="r1e1", url="https://pandas.pydata.org/x", title="pandas 2.0 notes", quote="append removed")


def record(verdict=Verdict.FIXED):
    plan = RunPlan(language="python", runtime="3.12", steps=[
        Step(id="s1", title="Install", kind="shell", content="pip install pandas"),
        Step(id="s2", title="Append rows", kind="run_code", content="df = df.append(row)\nprint(df)"),
    ])
    final = RunPlan(language="python", runtime="3.12", steps=[
        plan.steps[0],
        Step(id="s1_fix1", title="Added by Fix My Tutorial", kind="shell", content="pip install pyarrow",
             implicit=True, fix_reason="pandas 3 needs pyarrow", fix_evidence_ids=["r1e1"]),
        plan.steps[1].model_copy(update={"content": "df = pd.concat([df, row])\nprint(df)",
                                         "fix_reason": "pandas 2.0 removed DataFrame.append",
                                         "fix_evidence_ids": ["r1e1"]}),
    ])
    win = Candidate(strategy="migrate", rationale="use concat", edits=[
        Edit(step_id="s2", new_content="x", reason="r", evidence_ids=["r1e1"]),
        Edit(op="insert_after", step_id="s1", new_content="pip install pyarrow", reason="r", evidence_ids=["r1e1"])])
    return RunRecord(id="abc", source="https://t.example/post", title="Pandas tips", status=RunStatus.DONE,
                     verdict=verdict, plan=plan, final_plan=final, created_at=utcnow(), finished_at=utcnow(),
                     repairs=[RepairRecord(step_id="s2", signature=FailureSignature(), evidence=[EV], winner=win)])


def test_build_changes_replace_and_added():
    changes = build_changes(record())
    assert [c.step_id for c in changes] == ["s1_fix1", "s2"]
    added, replaced = changes
    assert added.added and added.before == "" and added.strategy == "migrate"
    assert not replaced.added and "-df = df.append(row)" in replaced.diff and "+df = pd.concat" in replaced.diff
    assert replaced.citations == [EV] and replaced.reason == "pandas 2.0 removed DataFrame.append"


def test_diff_lines_classes():
    changes = build_changes(record())
    assert all(cls == "add" for cls, _ in diff_lines(changes[0]))
    classes = {cls for cls, _ in diff_lines(changes[1])}
    assert {"add", "del"} <= classes


def test_no_changes_when_still_works():
    rec = record(Verdict.STILL_WORKS).model_copy(update={"final_plan": record().plan})
    assert build_changes(rec) == []


def test_badge_svg_text_and_escaping():
    svg = badge_svg(record())
    assert svg.startswith("<svg") and "Fix My Tutorial" in svg and "fixed version available" in svg
    still = badge_svg(record(Verdict.STILL_WORKS))
    assert "verified working" in still and "Python 3.12" in still


def test_og_png_is_1200x630():
    data = og_png(record())
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.size == (1200, 630)
