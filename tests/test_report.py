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



def test_partly_checked_runs_say_so_on_the_badge():
    from fixmytutorial.report import badge_label
    rec = record(Verdict.STILL_WORKS)
    partial = rec.model_copy(update={"plan": rec.plan.model_copy(update={"coverage": 0.8}), "final_plan": None})
    assert "partly checked" in badge_label(partial) and "verified" not in badge_label(partial)
    cut = rec.model_copy(update={"plan": rec.plan.model_copy(update={"truncated": True}), "final_plan": None})
    assert "partly checked" in badge_label(cut)
    assert "partly checked" not in badge_label(rec)
    from fixmytutorial.report import partial_note
    assert partial_note(partial) == "Partly checked: we ran 80% of the code on this page."
    assert partial_note(cut) == "Partly checked: only the first part of this page was run."
    assert partial_note(rec) == ""


def _broken(how="ran", last_good="1.5.3", published="2022-03-14"):
    from datetime import date

    from fixmytutorial.models import BreakPoint
    bp = BreakPoint(package="pandas", step_id="s2", first_bad="2.0.0", first_bad_date=date(2023, 4, 3),
                    last_good=last_good, last_good_date=date(2023, 1, 19) if last_good else None, how=how,
                    evidence_id="r1e1")
    return record().model_copy(update={"broke_in": bp, "published": published})


def test_broke_in_text_when_confirmed_by_running():
    from fixmytutorial.report import broke_in_text
    assert broke_in_text(_broken()) == (
        "Broke in pandas 2.0.0, released 3 Apr 2023. pandas 1.5.3 still runs this step, and 2.0.0 fails the "
        "same way. The tutorial was published in Mar 2022, so it worked for about 13 months.")


def test_broke_in_text_from_the_release_notes():
    from fixmytutorial.report import broke_in_text
    assert broke_in_text(_broken(how="release_notes", last_good=None, published=None)) == (
        "Broke in pandas 2.0.0, released 3 Apr 2023, according to the cited release notes. Older releases "
        "can't be installed here to check.")
    assert broke_in_text(_broken(how="release_notes", published=None)) == (
        "Broke in pandas 2.0.0, released 3 Apr 2023, according to the cited release notes. pandas 1.5.3 still "
        "runs this step.")


def test_broke_in_text_when_the_page_was_already_out_of_date():
    from fixmytutorial.report import broke_in_text
    assert broke_in_text(_broken(published="2023-06-01")).endswith(
        "That release came out before this page was published (Jun 2023), so the page was already out of date then.")
    assert broke_in_text(_broken(published="2023-04-01")).endswith("so it broke within a month of publishing.")


def test_no_break_text_without_a_break():
    from fixmytutorial.report import broke_in_text, broke_in_source
    assert broke_in_text(record()) == "" and broke_in_source(record()) is None
    assert broke_in_source(_broken()).url == "https://pandas.pydata.org/x"
