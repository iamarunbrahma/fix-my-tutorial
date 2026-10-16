from pathlib import Path

import httpx
import pytest
import respx

from fixmytutorial.ingest import IngestError, Source, github_raw_url, html_to_markdown, ingest, published_date
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


@respx.mock
async def test_tavily_extract_is_used_when_the_page_refuses_us(settings, tmp_path):
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(403))
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


@respx.mock
async def test_requests_uncompressed_pages():
    route = respx.get("https://raw.githubusercontent.com/o/r/main/t.md").mock(
        return_value=httpx.Response(200, text="# T\n\n```python\nprint(1)\n```\n"))
    await ingest(Source(url="https://github.com/o/r/blob/main/t.md"), None, run_id="r", public=True,
                 resolver=public_resolver)
    assert route.calls.last.request.headers["accept-encoding"] == "identity"


@respx.mock
async def test_gzip_page_is_decoded():
    import gzip
    body = gzip.compress(b"# Gz\n\n```python\nprint(1)\n```\n")
    respx.get("https://raw.githubusercontent.com/o/r/main/g.md").mock(
        return_value=httpx.Response(200, headers={"content-encoding": "gzip"}, content=body))
    doc = await ingest(Source(url="https://github.com/o/r/blob/main/g.md"), None, run_id="r", public=True,
                       resolver=public_resolver)
    assert doc.title == "Gz"


@respx.mock
async def test_gzip_bomb_is_rejected(monkeypatch):
    import gzip
    monkeypatch.setattr("fixmytutorial.ingest.MAX_BYTES", 1000)
    bomb = gzip.compress(b"0" * 500_000)
    assert len(bomb) < 1000
    respx.get("https://raw.githubusercontent.com/o/r/main/b.md").mock(
        return_value=httpx.Response(200, headers={"content-encoding": "gzip"}, content=bomb))
    with pytest.raises(IngestError, match="too large"):
        await ingest(Source(url="https://github.com/o/r/blob/main/b.md"), None, run_id="r", public=True,
                     resolver=public_resolver)



async def test_cut_off_pages_are_flagged(settings, tmp_path):
    doc = await ingest(Source(markdown="# T\n\n```python\nprint(1)\n```\n" + "x" * 500), None, run_id="r",
                       public=True, max_chars=100)
    assert doc.truncated


async def test_shared_address_space_is_blocked():
    with pytest.raises(IngestError, match="private"):
        await ingest(Source(url="http://meta.example/x"), None, run_id="r", public=True,
                     resolver=lambda host: ["100.100.100.200"])


async def test_a_slow_site_gives_a_clear_error_quickly():
    import time

    def slow(host):
        time.sleep(2)
        return ["93.184.216.34"]
    t0 = time.monotonic()
    with pytest.raises(IngestError, match="too long"):
        await ingest(Source(url="https://slow.example/t"), None, run_id="r", public=True, resolver=slow,
                     deadline_s=0.2)
    assert time.monotonic() - t0 < 1.5


@respx.mock
async def test_unreadable_notebook_is_a_clear_error():
    raw = "https://raw.githubusercontent.com/o/r/main/nb.ipynb"
    respx.get(raw).mock(return_value=httpx.Response(200, text="version https://git-lfs.github.com/spec/v1\n"))
    with pytest.raises(IngestError, match="notebook"):
        await ingest(Source(url="https://github.com/o/r/blob/main/nb.ipynb"), None, run_id="r", public=True,
                     resolver=public_resolver)


@pytest.mark.parametrize("head,expected", [
    ('<meta property="article:published_time" content="2022-03-14T09:00:00Z">', "2022-03-14"),
    ('<meta itemprop="datePublished" content="2021-07-02">', "2021-07-02"),
    ('<time itemprop="datePublished" datetime="2021-08-09T10:00">Aug 9</time>', "2021-08-09"),
    ('<script type="application/ld+json">{"@type": "Article", "datePublished": "2020-01-05T10:00:00+05:30"}</script>',
     "2020-01-05"),
    ('<meta name="date" content="2019-11-30">', "2019-11-30"),
    ('<meta name="publish-date" content="2019-10-01">', "2019-10-01"),
    ('<time datetime="2018-06-01T12:00">June 1</time>', "2018-06-01"),
    ('<meta property="article:published_time" content="soon">', None),
    ('<meta property="article:published_time" content="2999-01-01">', None),
    ('<meta property="article:published_time" content="1990-01-01">', None),
    ('<meta property="article:published_time" content="2021-02-30">', None),
    ("<title>No date here</title>", None),
])
def test_published_date(head, expected):
    assert published_date(f"<html><head>{head}</head><body><p>x</p></body></html>") == expected


@respx.mock
async def test_publish_date_is_read_from_the_page_when_tavily_supplies_the_text(settings, tmp_path):
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(
        200, text='<html><head><meta property="article:published_time" content="2022-03-14T09:00:00Z"></head></html>'))
    fake = FakeTavily(extract_results={"https://ex.com/t": "# T\n\n```python\nprint(1)\n```"})
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title == "T" and doc.published == "2022-03-14"


@respx.mock
async def test_a_page_that_refuses_the_date_fetch_still_ingests(settings, tmp_path):
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(403))
    fake = FakeTavily(extract_results={"https://ex.com/t": "# T\n\n```python\nprint(1)\n```"})
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title == "T" and doc.published is None


@respx.mock
async def test_publish_date_from_the_fetched_page(settings, tmp_path):
    page = (PAGES / "tutorial.html").read_text().replace(
        "<head>", '<head><meta property="article:published_time" content="2021-05-04">', 1)
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(200, text=page))
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, FakeTavily()),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.published == "2021-05-04"


def test_line_breaks_inside_code_blocks_are_kept():
    _, md = html_to_markdown("<article><h1>T</h1><pre><code>a = 1<br>b = 2<br/>print(a + b)</code></pre></article>")
    assert "a = 1\nb = 2\nprint(a + b)" in md


@pytest.mark.parametrize("url,feed,post", [
    ("https://medium.com/@egafebridh/basic-visualization-guide-178ea3787f59",
     "https://medium.com/feed/@egafebridh", "178ea3787f59"),
    ("https://medium.com/data-science/predict-housing-price-bfc0fcfff640?source=x",
     "https://medium.com/feed/data-science", "bfc0fcfff640"),
    ("https://someone.medium.com/a-post-title-0123456789ab", "https://someone.medium.com/feed", "0123456789ab"),
    ("https://medium.com/@egafebridh", None, None),
    ("https://example.com/post-178ea3787f59", None, None),
])
def test_medium_feed_for(url, feed, post):
    from fixmytutorial.ingest import medium_feed_for
    assert medium_feed_for(url) == ((feed, post) if feed else None)


FEED = """<?xml version="1.0"?><rss><channel>
<item><title><![CDATA[Another post]]></title><link>https://medium.com/@a/other-111111111111?source=rss</link>
<content:encoded><![CDATA[<p>nothing</p>]]></content:encoded></item>
<item><title><![CDATA[Plot &amp; learn]]></title><link>https://medium.com/@a/plot-178ea3787f59?source=rss</link>
<pubDate>Tue, 14 Mar 2023 10:00:00 GMT</pubDate>
<content:encoded><![CDATA[<h3>Setup</h3><p>Install it.</p><pre>import seaborn as sns<br>print(sns.__version__)</pre>]]></content:encoded></item>
</channel></rss>"""


@respx.mock
async def test_medium_posts_are_read_from_the_author_feed(settings, tmp_path):
    respx.get("https://medium.com/feed/@a").mock(return_value=httpx.Response(200, text=FEED))
    fake = FakeTavily(extract_results={})
    doc = await ingest(Source(url="https://medium.com/@a/plot-178ea3787f59"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title == "Plot & learn" and doc.published == "2023-03-14"
    assert doc.code_blocks[0].code.strip() == "import seaborn as sns\nprint(sns.__version__)"
    assert fake.extract_calls == []  # no credits spent


FLAT = ("# T\n\n```\nimport matplotlib.pyplot as pltimport seaborn as snsimport pandas as pd# Sample Datasetfrom "
        "sklearn.datasets import load_irisiris = load_iris() import as import as import as\n```\n")


@respx.mock
async def test_code_without_line_breaks_is_never_run(settings, tmp_path):
    respx.get("https://medium.com/feed/@a").mock(return_value=httpx.Response(200, text=FEED))  # post not in feed
    respx.get("https://medium.com/@a/old-post-0123456789ab").mock(return_value=httpx.Response(403))
    fake = FakeTavily(extract_results={"https://medium.com/@a/old-post-0123456789ab": FLAT})
    with pytest.raises(IngestError, match="without its line breaks"):
        await ingest(Source(url="https://medium.com/@a/old-post-0123456789ab"), search_client(settings, tmp_path, fake),
                     run_id="r", public=True, resolver=public_resolver)


@respx.mock
async def test_flattened_extract_falls_back_to_the_page_itself(settings, tmp_path):
    page = "<html><head><title>T</title></head><body><article><h1>T</h1><pre>import os<br>print(os.sep)</pre></article></body></html>"
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(200, text=page))
    fake = FakeTavily(extract_results={"https://ex.com/t": FLAT})
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.code_blocks[0].code.strip() == "import os\nprint(os.sep)"


def test_flattened_code_detection_catches_the_real_medium_pandas_block():
    from fixmytutorial.ingest import flattened
    from fixmytutorial.models import CodeBlock
    real = ("import pandas as pddata = {    'Name': ['Alice', 'Bob'],    'Age': [25, 30],}df = pd.DataFrame(data)"
            "print(df) import as data 'Name' 'Alice' 'Bob' 'Age' 25 30 data")
    assert flattened([CodeBlock(code=real)])
    assert not flattened([CodeBlock(code="pip install pandas numpy matplotlib seaborn scikit-learn jupyter notebook ipykernel")])
    assert not flattened([CodeBlock(code="import os\nimport sys\nprint(os.sep, sys.version)")])
    short_medium = "df = pd.read_csv('data.csv', error_bad_lines=False)print(df.head()) df pd 'data.csv'"
    assert flattened([CodeBlock(code=short_medium)], medium=True) and not flattened([CodeBlock(code="x = 1")], medium=True)


@respx.mock
async def test_the_page_itself_is_read_first_when_its_code_is_clean(settings, tmp_path):
    page = ('<html><head><meta property="article:published_time" content="2019-01-03"><title>T</title></head><body>'
            "<article><h1>Iterate rows</h1><pre>import pandas as pd<br>print(pd.__version__)</pre></article></body></html>")
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(200, text=page))
    fake = FakeTavily(extract_results={"https://ex.com/t": FLAT})
    doc = await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                       run_id="r", public=True, resolver=public_resolver)
    assert doc.title == "Iterate rows" and doc.published == "2019-01-03"
    assert doc.code_blocks[0].code.strip() == "import pandas as pd\nprint(pd.__version__)"
    assert fake.extract_calls == []  # no extractor credits spent


GARBLED = ("# T\n\n```\n\n****Output****\n\n![ex1](https://media.example.com/ex1.png)\n\nOutput\n\n****Example 2:**** text\n"
           "\n Python  ````\n import pandas as pd data = pd. read_csv(\"nba.csv\")\n```\n")


@respx.mock
async def test_extracted_code_full_of_prose_and_images_is_refused(settings, tmp_path):
    respx.get("https://ex.com/t").mock(return_value=httpx.Response(403))
    fake = FakeTavily(extract_results={"https://ex.com/t": GARBLED})
    with pytest.raises(IngestError, match="line breaks"):
        await ingest(Source(url="https://ex.com/t"), search_client(settings, tmp_path, fake),
                     run_id="r", public=True, resolver=public_resolver)
