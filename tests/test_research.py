from fixmytutorial.ledger import Ledger
from fixmytutorial.models import FailureSignature
from fixmytutorial.research import best_passage, domain_rank, research
from fixmytutorial.search import SearchClient
from fixmytutorial.store import Store
from tests.fakes import FakeTavily

SIG = FailureSignature(category="removed_api", error_type="ImportError", package="sklearn", symbol="load_boston",
                       key_line="ImportError: cannot import name 'load_boston'",
                       search_queries=["sklearn load_boston removed", "load_boston replacement"])


def sc(settings, tmp_path, fake):
    return SearchClient(settings, Ledger(tmp_path / "l.db", settings), Store(tmp_path / "s.db"), client=fake)


def test_domain_rank():
    assert domain_rank("https://scikit-learn.org/stable/whats_new/v1.2.html") == 0
    assert domain_rank("https://some-blog.example/x") == 1


def test_best_passage_picks_matching_paragraph():
    text = "Intro paragraph.\n\nThe load_boston function was removed in 1.2 because of ethical issues.\n\nOther."
    assert "removed in 1.2" in best_passage(text, SIG)


async def test_research_ranks_extracts_and_ids(settings, tmp_path):
    fake = FakeTavily(
        search_results={
            "sklearn load_boston removed": [
                {"url": "https://blog.example/a", "title": "Blog", "content": "load_boston gone", "score": 0.99},
                {"url": "https://scikit-learn.org/whats_new/v1.2", "title": "Release 1.2",
                 "content": "removed load_boston", "score": 0.5}],
            "load_boston replacement": [
                {"url": "https://scikit-learn.org/whats_new/v1.2", "title": "Release 1.2", "content": "dup",
                 "score": 0.4}]},
        extract_results={"https://scikit-learn.org/whats_new/v1.2":
                         "Changes.\n\nload_boston has been removed; use fetch_california_housing."})
    ev = await research(SIG, search=sc(settings, tmp_path, fake), run_id="r", public=True, id_prefix="r1e")
    assert [e.id for e in ev] == ["r1e1", "r1e2"]
    assert ev[0].url == "https://scikit-learn.org/whats_new/v1.2"
    assert "fetch_california_housing" in ev[0].quote
    assert fake.extract_calls[0]["query"] == "load_boston sklearn ImportError"


async def test_timeout_signature_skips_search(settings, tmp_path):
    fake = FakeTavily([])
    ev = await research(FailureSignature(category="timeout"), search=sc(settings, tmp_path, fake),
                        run_id="r", public=True)
    assert ev == [] and fake.search_calls == []


async def test_search_errors_are_tolerated(settings, tmp_path):
    class Boom(FakeTavily):
        async def search(self, query, **kw):
            raise RuntimeError("tavily down")
    assert await research(SIG, search=sc(settings, tmp_path, Boom()), run_id="r", public=True) == []


APPEND_SIG = FailureSignature(category="removed_api", error_type="AttributeError", symbol="DataFrame.append",
                              key_line="AttributeError: 'DataFrame' object has no attribute 'append'",
                              search_queries=["pandas DataFrame.append removed"])


def test_best_passage_prefers_the_migration_note_over_repeated_mentions():
    text = ("Bug in DataFrame.append() failing to retain dtypes (GH 43392). Bug in DataFrame.append() with "
            "duplicate DataFrame columns.\n\nDataFrame.append was removed; use pandas.concat instead.")
    assert best_passage(text, APPEND_SIG).startswith("DataFrame.append was removed")


async def test_quotes_that_never_mention_the_symbol_are_dropped(settings, tmp_path):
    fake = FakeTavily(search_results={"pandas DataFrame.append removed": [
        {"url": "https://pandas.pydata.org/whatsnew/v2.0", "title": "2.0", "score": 0.9,
         "content": "DataFrame.append was removed; use pandas.concat instead."},
        {"url": "https://tutorials.example/pandas", "title": "Pandas DataFrame", "score": 0.8,
         "content": "+ Adding New Column to Existing DataFrame in Pandas 6 min read + Python | DataFrame.drop"}]})
    ev = await research(APPEND_SIG, search=sc(settings, tmp_path, fake), run_id="r", public=True)
    assert [e.url for e in ev] == ["https://pandas.pydata.org/whatsnew/v2.0"]


async def test_only_web_links_become_evidence(settings, tmp_path):
    fake = FakeTavily(search_results={"pandas DataFrame.append removed": [
        {"url": "javascript:alert(1)", "title": "x", "score": 0.99, "content": "DataFrame.append was removed."},
        {"url": "https://pandas.pydata.org/whatsnew/v2.0", "title": "2.0", "score": 0.9,
         "content": "DataFrame.append was removed; use pandas.concat instead."}]})
    ev = await research(APPEND_SIG, search=sc(settings, tmp_path, fake), run_id="r", public=True)
    assert [e.url for e in ev] == ["https://pandas.pydata.org/whatsnew/v2.0"]
