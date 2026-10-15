import pytest

from fixmytutorial.ledger import BudgetExceeded, Ledger
from fixmytutorial.search import SearchClient, extract_credits
from fixmytutorial.store import Store
from tests.fakes import FakeTavily


def make(settings, tmp_path, fake, **caps):
    s = settings.model_copy(update=caps)
    led = Ledger(tmp_path / "l.db", s)
    return SearchClient(s, led, Store(tmp_path / "s.db"), client=fake), led


def test_extract_credit_math():
    assert extract_credits(1, "basic") == 1
    assert extract_credits(5, "basic") == 1
    assert extract_credits(6, "basic") == 2
    assert extract_credits(3, "advanced") == 2


async def test_search_normalizes_and_charges_one_credit(settings, tmp_path):
    fake = FakeTavily([{"url": "https://u", "title": "T", "content": "C", "score": 0.9, "extra": 1}])
    sc, led = make(settings, tmp_path, fake)
    res = await sc.search("pandas append removed", run_id="r", public=True)
    assert res == [{"url": "https://u", "title": "T", "content": "C", "score": 0.9}]
    assert led.totals("r").run_credits == 1
    assert fake.search_calls[0]["search_depth"] == "basic"


async def test_search_is_cached(settings, tmp_path):
    fake = FakeTavily([{"url": "https://u", "title": "T", "content": "C", "score": 0.9}])
    sc, led = make(settings, tmp_path, fake)
    await sc.search("q", run_id="r", public=True)
    await sc.search("q", run_id="r2", public=True)
    assert len(fake.search_calls) == 1
    assert led.totals().lifetime_credits == 1


async def test_extract_charges_by_url_batches(settings, tmp_path):
    fake = FakeTavily(extract_results={"https://a": "A text", "https://b": "B text"})
    sc, led = make(settings, tmp_path, fake)
    res = await sc.extract(["https://a", "https://b", "https://missing"], run_id="r", public=True,
                           query="DataFrame.append")
    assert {r["url"] for r in res} == {"https://a", "https://b"}
    assert fake.extract_calls[0]["query"] == "DataFrame.append"
    assert led.totals("r").run_credits == 1


async def test_run_credit_cap(settings, tmp_path):
    fake = FakeTavily([])
    sc, _ = make(settings, tmp_path, fake, cap_tavily_run_credits=1)
    await sc.search("a", run_id="r", public=True)
    with pytest.raises(BudgetExceeded):
        await sc.search("b", run_id="r", public=True)


async def test_search_error_charges_reserved_credit(settings, tmp_path):
    class Boom(FakeTavily):
        async def search(self, query, **kw):
            raise TimeoutError("tavily timed out")
    sc, led = make(settings, tmp_path, Boom())
    with pytest.raises(TimeoutError):
        await sc.search("q", run_id="r", public=True)
    assert led.totals("r").run_credits == 1


async def test_search_can_restrict_domains_and_caches_per_domain_set(settings, tmp_path):
    fake = FakeTavily([{"url": "https://realpython.com/x", "title": "T", "content": "C", "score": 0.5}])
    sc, _ = make(settings, tmp_path, fake)
    await sc.search("pandas tutorial", run_id="r", public=False, include_domains=["realpython.com"])
    await sc.search("pandas tutorial", run_id="r", public=False)
    assert fake.search_calls[0]["include_domains"] == ["realpython.com"]
    assert "include_domains" not in fake.search_calls[1]


async def test_cancelled_search_is_charged(settings, tmp_path):
    import asyncio

    class Cancelled(FakeTavily):
        async def search(self, query, **kw):
            raise asyncio.CancelledError()
    sc, led = make(settings, tmp_path, Cancelled())
    with pytest.raises(asyncio.CancelledError):
        await sc.search("q", run_id="r", public=True)
    assert [row["credits"] for row in led.export_rows()] == [1]


async def test_malformed_extract_reply_is_charged(settings, tmp_path):
    class Weird(FakeTavily):
        async def extract(self, urls, **kw):
            return {"results": [{"no_url": True}]}
    sc, led = make(settings, tmp_path, Weird())
    with pytest.raises(KeyError):
        await sc.extract(["https://a.example"], run_id="r", public=True)
    assert [row["credits"] for row in led.export_rows()] == [1]
