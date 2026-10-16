from fixmytutorial.eval import FIXTURES, EvalRow, load_expected, score, table


def row(name, expected, verdict):
    return EvalRow(name=name, expected=expected, verdict=verdict, category="", rounds=1, seconds=1.0,
                   cost_usd=0.01, credits=2, reason="")


def test_score_passes_with_ten_fixed_and_controls_intact():
    rows = [row(f"r{i}", "fixed", "fixed") for i in range(10)] + [row("c", "still_works", "still_works")]
    ok, msg = score(rows)
    assert ok and "10/10" in msg


def test_score_fails_when_control_changes():
    rows = [row(f"r{i}", "fixed", "fixed") for i in range(10)] + [row("c", "still_works", "fixed")]
    assert not score(rows)[0]


def test_score_fails_below_threshold():
    rows = [row(f"r{i}", "fixed", "fixed" if i < 9 else "could_not_fix") for i in range(13)]
    assert not score(rows)[0]


def test_table_has_header_and_rows():
    out = table([row("py_x", "fixed", "fixed")])
    assert out.splitlines()[0].startswith("| fixture |") and "py_x" in out


def test_fixtures_and_expectations_match():
    names = {p.stem for p in FIXTURES.glob("*.md")}
    assert names == set(load_expected()) and len(names) == 15


def test_the_report_is_written_even_when_docs_is_missing(tmp_path):
    # docs/ is not in the public repo, so a fresh clone has no such folder
    from fixmytutorial.eval import write_report
    path = write_report("# Evaluation results\n", root=tmp_path)
    assert path == tmp_path / "docs" / "eval-results.md" and path.read_text() == "# Evaluation results\n"
