from urllib.parse import parse_qs, urlparse

import pytest

from fixmytutorial.author import author_note, github_repo_for, issue_url
from fixmytutorial.models import (
    Candidate,
    Edit,
    Evidence,
    FailureSignature,
    RepairRecord,
    RunPlan,
    RunRecord,
    RunStatus,
    Step,
    Verdict,
    utcnow,
)

REPORT = "https://fmt.example/r/abc"
EVID = Evidence(
    id="r1e1",
    url="https://pandas.pydata.org/whatsnew/v2.0",
    title="pandas 2.0.0 release notes",
    quote="DataFrame.append was removed in 2.0.0; use pandas.concat",
)
SIG = FailureSignature(
    category="removed_api",
    error_type="AttributeError",
    package="pandas",
    symbol="DataFrame.append",
    key_line="AttributeError: 'DataFrame' object has no attribute 'append'",
)


def record(verdict: Verdict) -> RunRecord:
    plan = RunPlan(
        language="python",
        runtime="3.12",
        steps=[
            Step(id="s1", title="Install pandas", kind="shell", content="pip install pandas"),
            Step(
                id="s2",
                title="Add a row",
                kind="run_code",
                content="df = df.append({'a': 1}, ignore_index=True)",
            ),
        ],
    )
    fixed = plan.model_copy(
        update={
            "steps": [
                plan.steps[0],
                plan.steps[1].model_copy(
                    update={
                        "content": "df = pd.concat([df, pd.DataFrame([{'a': 1}])], ignore_index=True)",
                        "fix_reason": "pandas 2.0 removed DataFrame.append; concat is the replacement.",
                        "fix_evidence_ids": ["r1e1"],
                    }
                ),
            ]
        }
    )
    winner = Candidate(
        strategy="migrate",
        rationale="use concat",
        edits=[Edit(step_id="s2", new_content=fixed.steps[1].content, reason="r", evidence_ids=["r1e1"])],
    )
    return RunRecord(
        id="abc",
        source="https://github.com/owner/repo/blob/main/guide.md",
        title="Pandas rows",
        status=RunStatus.DONE,
        verdict=verdict,
        plan=plan,
        final_plan=fixed if verdict == Verdict.FIXED else plan,
        repairs=[
            RepairRecord(
                step_id="s2",
                signature=SIG,
                evidence=[EVID],
                winner=winner if verdict == Verdict.FIXED else None,
            )
        ],
        created_at=utcnow(),
        finished_at=utcnow(),
    )


@pytest.mark.parametrize(
    "url,repo",
    [
        (
            "https://github.com/jakevdp/PythonDataScienceHandbook/blob/master/notebooks/03.01.ipynb",
            ("jakevdp", "PythonDataScienceHandbook"),
        ),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://www.github.com/owner/repo/tree/main/docs", ("owner", "repo")),
        ("https://raw.githubusercontent.com/owner/repo/main/README.md", ("owner", "repo")),
        ("https://owner.github.io/repo/guide/start.html", None),  # project site or blog post: can't tell
        ("https://karpathy.github.io/2015/05/21/rnn-effectiveness/", None),
        ("https://owner.github.io/", None),
        ("https://github.com/../../x", None),
        ("https://github.com/advisories/GHSA-x", None),
        ("https://github.com/users/owner", None),
        ("https://colab.research.google.com/github/owner/repo/blob/main/nb.ipynb", ("owner", "repo")),
        ("https://nbviewer.org/github/owner/repo/blob/main/nb.ipynb", ("owner", "repo")),
        ("https://nbviewer.jupyter.org/github/owner/repo/blob/main/nb.ipynb", ("owner", "repo")),
        ("https://github.com/orgs/owner/repositories", None),
        ("https://github.com/topics/python", None),
        ("https://github.com/owner", None),
        ("https://medium.com/@a/b", None),
        ("https://evil.example/github.com/owner/repo", None),
        ("https://github.com.evil.example/owner/repo", None),
        ("https://github.com/own<er/repo", None),
        ("not a url", None),
        ("", None),
    ],
)
def test_github_repo_for(url, repo):
    assert github_repo_for(url) == repo


def test_note_for_a_fixed_tutorial():
    title, body = author_note(record(Verdict.FIXED), REPORT)
    assert title == "This tutorial fails today: AttributeError: 'DataFrame' object has no attribute 'append'"
    assert "fresh sandbox" in body and "Python 3.12" in body
    assert "```diff" in body and "-df = df.append" in body and "+df = pd.concat" in body
    assert "pandas 2.0 removed DataFrame.append" in body
    assert "[pandas 2.0.0 release notes](https://pandas.pydata.org/whatsnew/v2.0)" in body
    assert body.rstrip().endswith(REPORT) and body.count("```") % 2 == 0


def test_note_for_a_tutorial_we_could_not_fix():
    title, body = author_note(record(Verdict.COULD_NOT_FIX), REPORT)
    assert title.startswith("This tutorial fails today: AttributeError")
    assert "```diff" not in body and "We couldn't find a fix" in body
    assert "[pandas 2.0.0 release notes](https://pandas.pydata.org/whatsnew/v2.0)" in body and REPORT in body


def test_note_without_a_diagnosis_still_reads_well():
    rec = record(Verdict.COULD_NOT_FIX).model_copy(update={"repairs": []})
    title, body = author_note(rec, REPORT)
    assert title == "This tutorial no longer runs as written" and REPORT in body


def test_issue_url_stays_short_and_keeps_the_report_link():
    body = "```diff\n" + "+line\n" * 5000 + "```\n\nFull report: " + REPORT
    url = issue_url("owner", "repo", "Title", body, REPORT, limit=7000)
    assert len(url) <= 7000 and url.startswith("https://github.com/owner/repo/issues/new?")
    q = parse_qs(urlparse(url).query)
    assert q["title"] == ["Title"] and REPORT in q["body"][0]
    assert q["body"][0].count("```") % 2 == 0  # a cut never leaves a code block open


def test_issue_url_keeps_a_short_body_whole():
    _, body = author_note(record(Verdict.FIXED), REPORT)
    q = parse_qs(urlparse(issue_url("owner", "repo", "T", body, REPORT)).query)
    assert q["body"] == [body]


def test_note_says_when_it_broke():
    from datetime import date

    from fixmytutorial.models import BreakPoint

    bp = BreakPoint(
        package="pandas",
        step_id="s2",
        first_bad="2.0.0",
        first_bad_date=date(2023, 4, 3),
        last_good="1.5.3",
        last_good_date=date(2023, 1, 19),
        how="ran",
    )
    _, body = author_note(record(Verdict.FIXED).model_copy(update={"broke_in": bp}), REPORT)
    assert "**When it broke:** Broke in pandas 2.0.0, released 3 Apr 2023." in body
    _, plain = author_note(record(Verdict.FIXED), REPORT)
    assert "When it broke" not in plain
