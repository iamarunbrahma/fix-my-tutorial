from pathlib import Path

import httpx
import pytest
import respx

from fixmytutorial.ingest import IngestError, Source, github_raw_url, html_to_markdown, ingest
from fixmytutorial.ledger import Ledger
from fixmytutorial.search import SearchClient
from fixmytutorial.store import Store
from tests.fakes import FakeTavily

PAGES = Path(__file__).parent / "fixtures" / "pages"


def public_resolver(host):
    return ["93.184.216.34"]


def search_client(settings, tmp_path, fake):
    return SearchClient(settings, Ledger(tmp_path / "l.db", settings), Store(tmp_path / "s.db"), client=fake)


def test_github_raw_url():
    assert github_raw_url("https://github.com/o/r/blob/main/docs/t.md") == \
        "https://raw.githubusercontent.com/o/r/main/docs/t.md"
    assert github_raw_url("https://github.com/o/r") == "https://raw.githubusercontent.com/o/r/HEAD/README.md"
    assert github_raw_url("https://example.com/x") is None


def test_html_to_markdown_keeps_code_and_language():
    title, md = html_to_markdown((PAGES / "tutorial.html").read_text())
    assert title == "Linear regression with scikit-learn"
    assert "```python\nfrom sklearn.datasets import load_boston" in md
    assert "menu" not in md and "footer" not in md


async def test_markdown_source(settings, tmp_path):
    md = "# My tut\n\n```python\nprint('hi')\n```\n"
    doc = await ingest(Source(markdown=md), None, run_id="r", public=True)
    assert doc.title == "My tut" and doc.source == "pasted-markdown"
    assert doc.code_blocks[0].lang == "python" and doc.code_blocks[0].code == "print('hi')\n"
    assert len(doc.content_hash) == 16


async def test_tavily_extract_is_used_first(settings, tmp_path):
    fake = FakeTavily(extract_results={"https://ex.com/t": "# T\n\n```python\nprint(1)\n```"})
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title == "T" and len(doc.code_blocks) == 1
    assert fake.extract_calls[0]["extract_depth"] == "advanced"


@respx.mock
async def test_html_fallback_when_tavily_returns_nothing(settings, tmp_path):
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(200, text=(PAGES / "tutorial.html").read_text()))
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, FakeTavily()),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title.startswith("Linear regression")
    assert any("load_boston" in b.code for b in doc.code_blocks)


@respx.mock
async def test_github_notebook(settings, tmp_path):
    raw = "https://raw.githubusercontent.com/o/r/main/nb.ipynb"
    respx.get(raw).mock(return_value=httpx.Response(200, text=(PAGES / "notebook.ipynb").read_text()))
    doc = await ingest(Source(url="https://github.com/o/r/blob/main/nb.ipynb"), None, run_id="r", public=True,
                       resolver=public_resolver)
    assert doc.title == "Pandas basics"
    assert [b.code.strip() for b in doc.code_blocks][1] == "print(df.shape)"


@respx.mock
async def test_ingest_unreachable_url_raises_ingest_error(settings, tmp_path):
    respx.get("https://ex.com/gone").mock(return_value=httpx.Response(404))
    with pytest.raises(IngestError, match="HTTP 404"):
        await ingest(Source(url="https://ex.com/gone"), search_client(settings, tmp_path, FakeTavily()),
                     run_id="r", public=True, resolver=public_resolver)


async def test_private_address_rejected(settings, tmp_path):
    with pytest.raises(IngestError, match="private"):
        await ingest(Source(url="http://internal.local/x"), None, run_id="r", public=True,
                     resolver=lambda host: ["10.0.0.5"])


async def test_non_http_scheme_rejected():
    with pytest.raises(IngestError, match="http"):
        await ingest(Source(url="file:///etc/passwd"), None, run_id="r", public=True, resolver=public_resolver)


async def test_empty_source_rejected():
    with pytest.raises(IngestError):
        await ingest(Source(), None, run_id="r", public=True)


async def test_connect_time_check_blocks_rebinding_to_private_ip():
    from fixmytutorial.ingest import SafeNetworkBackend
    backend = SafeNetworkBackend(resolver=lambda host: ["127.0.0.1"])
    with pytest.raises(IngestError, match="private"):
        await backend.connect_tcp("rebind.example", 80)


async def test_connect_goes_to_the_validated_ip():
    from fixmytutorial.ingest import SafeNetworkBackend

    class Inner:
        hosts = []

        async def connect_tcp(self, host, port, **kw):
            self.hosts.append(host)
            return "stream"

    inner = Inner()
    backend = SafeNetworkBackend(resolver=public_resolver, inner=inner)
    assert await backend.connect_tcp("ex.com", 443) == "stream"
    assert inner.hosts == ["93.184.216.34"]


@respx.mock
async def test_oversized_page_rejected_by_content_length_before_reading(monkeypatch):
    monkeypatch.setattr("fixmytutorial.ingest.MAX_BYTES", 100)
    consumed = []

    async def body():
        for _ in range(50):
            consumed.append(1)
            yield b"x" * 10

    respx.get("https://ex.com/big").mock(
        return_value=httpx.Response(200, headers={"content-length": "500"}, content=body()))
    with pytest.raises(IngestError, match="too large"):
        await ingest(Source(url="https://ex.com/big"), None, run_id="r", public=True, resolver=public_resolver)
    assert consumed == []


@respx.mock
async def test_oversized_stream_without_length_is_cut_off(monkeypatch):
    monkeypatch.setattr("fixmytutorial.ingest.MAX_BYTES", 100)

    consumed = []

    async def chunks():
        for _ in range(50):
            consumed.append(1)
            yield b"y" * 10

    respx.get("https://ex.com/stream").mock(return_value=httpx.Response(200, content=chunks()))
    with pytest.raises(IngestError, match="too large"):
        await ingest(Source(url="https://ex.com/stream"), None, run_id="r", public=True, resolver=public_resolver)
    assert len(consumed) <= 12  # stopped right after crossing the cap, not after reading everything
