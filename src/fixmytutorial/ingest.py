"""Turn a tutorial URL or pasted Markdown into a TutorialDoc (title, text, code blocks, content hash)."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from markdown_it import MarkdownIt
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .ledger import BudgetExceeded
from .models import CodeBlock, TutorialDoc

USER_AGENT = "FixMyTutorial/0.1 (+https://github.com/iamarunbrahma/fix-my-tutorial)"
MAX_BYTES = 3_000_000
GH_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
GH_REPO = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+)/?$")
_md = MarkdownIt()


class IngestError(Exception):
    pass


@dataclass
class Source:
    url: str | None = None
    markdown: str | None = None

    def label(self) -> str:
        return self.url or "pasted-markdown"


def github_raw_url(url: str) -> str | None:
    if m := GH_BLOB.match(url):
        return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}/{m[4]}"
    if m := GH_REPO.match(url):
        return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/HEAD/README.md"
    if url.startswith("https://raw.githubusercontent.com/"):
        return url
    return None


def markdown_code_blocks(text: str) -> list[CodeBlock]:
    return [CodeBlock(lang=(t.info or "").strip().split(" ")[0], code=t.content)
            for t in _md.parse(text) if t.type in ("fence", "code_block")]


def first_heading(text: str) -> str | None:
    m = re.search(r"^#{1,3}\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def notebook_to_markdown(nb_json: str) -> str:
    nb = json.loads(nb_json)
    lang = nb.get("metadata", {}).get("language_info", {}).get("name", "python")
    parts = []
    for cell in nb.get("cells", []):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        if cell.get("cell_type") == "markdown":
            parts.append(src)
        elif cell.get("cell_type") == "code" and src.strip():
            parts.append(f"```{lang}\n{src}\n```")
    return "\n\n".join(parts)


def html_to_markdown(html: str) -> tuple[str, str]:
    tree = HTMLParser(html)
    heading = tree.css_first("h1") or tree.css_first("title")
    title = heading.text(strip=True) if heading else "Untitled tutorial"
    for sel in ("script", "style", "nav", "footer", "header", "aside", "form", "noscript"):
        for node in tree.css(sel):
            node.decompose()
    root = tree.css_first("article") or tree.css_first("main") or tree.body
    if root is None:
        return title, ""
    parts = []
    for n in root.css("h1, h2, h3, h4, p, li, pre"):
        if n.tag == "pre":
            code = n.css_first("code")
            cls = ((code.attributes.get("class") if code else None) or n.attributes.get("class") or "")
            m = re.search(r"(?:language|lang)-([\w+-]+)", cls)
            parts.append(f"```{m.group(1) if m else ''}\n{n.text()}\n```")
        elif n.tag[0] == "h":
            parts.append("#" * int(n.tag[1]) + " " + n.text(strip=True))
        else:
            text = n.text(strip=True)
            if text:
                parts.append(("- " if n.tag == "li" else "") + text)
    return title, "\n\n".join(parts)


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


async def _check_public(url: str, resolver: Callable[[str], list[str]]) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise IngestError("Only http and https links are supported.")
    try:
        ips = await asyncio.to_thread(resolver, p.hostname)
    except OSError:
        raise IngestError("Couldn't find that website. Check the link.") from None
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise IngestError("That link points to a private network address.")


async def _safe_get(url: str, client: httpx.AsyncClient, resolver) -> httpx.Response:
    for _ in range(4):
        await _check_public(url, resolver)
        try:
            r = await client.get(url, follow_redirects=False, timeout=20, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError:
            raise IngestError("Couldn't read that page (the site did not respond).") from None
        if r.is_redirect and "location" in r.headers:
            url = urljoin(url, r.headers["location"])
            continue
        if r.status_code >= 400:
            raise IngestError(f"Couldn't read that page (HTTP {r.status_code}).")
        if len(r.content) > MAX_BYTES:
            raise IngestError("That page is too large to read.")
        return r
    raise IngestError("Couldn't read that page (too many redirects).")


@asynccontextmanager
async def _client(http: httpx.AsyncClient | None):
    if http is not None:
        yield http
    else:
        async with httpx.AsyncClient() as c:
            yield c


async def ingest(source: Source, search, *, run_id: str, public: bool, http: httpx.AsyncClient | None = None,
                 resolver: Callable[[str], list[str]] | None = None, max_chars: int = 60_000) -> TutorialDoc:
    resolver = resolver or _default_resolver
    if source.markdown and source.markdown.strip():
        text = source.markdown
        title = first_heading(text) or "Pasted tutorial"
        label = "pasted-markdown"
    elif source.url and source.url.strip():
        url = source.url.strip()
        label = url
        await _check_public(url, resolver)
        raw = github_raw_url(url)
        text, title = "", ""
        async with _client(http) as client:
            if raw:
                r = await _safe_get(raw, client, resolver)
                text = notebook_to_markdown(r.text) if raw.endswith(".ipynb") else r.text
                title = first_heading(text) or raw.rsplit("/", 1)[-1]
            else:
                if search is not None:
                    try:
                        res = await search.extract([url], run_id=run_id, public=public, depth="advanced")
                    except BudgetExceeded:
                        raise
                    except Exception:
                        res = []
                    if res and res[0]["raw_content"].strip():
                        text = res[0]["raw_content"]
                        title = first_heading(text) or url
                if not text:
                    r = await _safe_get(url, client, resolver)
                    if url.endswith(".ipynb"):
                        text = notebook_to_markdown(r.text)
                        title = first_heading(text) or url
                    else:
                        title, text = html_to_markdown(r.text)
    else:
        raise IngestError("Paste a tutorial link or its Markdown.")
    if not text.strip():
        raise IngestError("Couldn't find any text on that page.")
    text = text[:max_chars]
    return TutorialDoc(source=label, title=title[:200], text=text, code_blocks=markdown_code_blocks(text),
                       content_hash=hashlib.sha256(text.encode()).hexdigest()[:16])
