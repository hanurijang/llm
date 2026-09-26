"""web_fetch 도구: URL 을 받아 본문 텍스트로 돌려준다(표준 라이브러리만)."""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_DOWNLOAD_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 40_000
USER_AGENT = "llm-cli/web_fetch (+https://github.com)"

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "br", "hr", "li", "ul", "ol", "table", "tr", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3"}:
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if tag == "a" and self._href and self._href.startswith("http"):
            self.parts.append(f" ({self._href})")
            self._href = None
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str]:
    """HTML → (제목, 읽기 좋은 텍스트)."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # 깨진 HTML 이어도 받은 만큼은 쓴다
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return html.unescape(parser.title.strip()), text


def validate_url(url: str) -> str:
    """http(s) URL 만 허용하고 정규화한다. 문제가 있으면 ValueError."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"http(s) URL 만 가져올 수 있습니다: {url!r}")
    return url


def url_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def fetch_text(url: str, max_chars: int = DEFAULT_MAX_CHARS, timeout: float = 30) -> str:
    url = validate_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/json,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            final_url = resp.geturl()
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code} {exc.reason} ({url})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"ERROR: 가져오기 실패: {getattr(exc, 'reason', exc)} ({url})"
    truncated_bytes = len(raw) > MAX_DOWNLOAD_BYTES
    raw = raw[:MAX_DOWNLOAD_BYTES]
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    if match:
        charset = match.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    if "html" in ctype.lower() or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(body)
    elif ctype and not re.search(r"text|json|xml|javascript|yaml|csv", ctype, re.I):
        return f"ERROR: 텍스트가 아닌 콘텐츠입니다({ctype}): {final_url}"
    else:
        title, text = "", body
    header = f"URL: {final_url} (HTTP {status}, {ctype or 'unknown'})"
    if title:
        header += f"\nTitle: {title}"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated {len(text) - max_chars} chars]"
    elif truncated_bytes:
        text += "\n\n... [download truncated]"
    return f"{header}\n\n{text}"
