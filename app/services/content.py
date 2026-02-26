from __future__ import annotations

import time
import warnings
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

try:
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None


DEFAULT_HEADERS = {
    "User-Agent": "LinkLoomBot/1.0 (+https://linkloom.local)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_CERTIFICATE_ERROR_MARKERS = (
    "certificate verify failed",
    "certificateverifyfailed",
    "self signed certificate",
    "unable to get local issuer certificate",
)

LINK_STATUS_ALIVE = "alive"
LINK_STATUS_TIMEOUT = "timeout"
LINK_STATUS_NOT_FOUND = "not_found"
LINK_STATUS_SERVER_ERROR = "server_error"
LINK_STATUS_DNS_ERROR = "dns_error"
LINK_STATUS_UNREACHABLE = "unreachable"

TRANSIENT_LINK_RESULTS = {
    LINK_STATUS_TIMEOUT,
    LINK_STATUS_UNREACHABLE,
    LINK_STATUS_SERVER_ERROR,
}


@dataclass
class ExtractedContent:
    title: str | None
    text: str
    status: str
    error: str | None = None
    status_code: int | None = None
    final_url: str | None = None


@dataclass
class LinkCheckResult:
    status_code: int | None
    final_url: str | None
    result_type: str
    latency_ms: int | None
    error: str | None = None


def _normalize_error(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _is_untrusted_certificate_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _CERTIFICATE_ERROR_MARKERS)


def fetch_html(url: str, timeout: float, max_bytes: int) -> tuple[str, str, int]:
    with httpx.Client(
        follow_redirects=True, timeout=timeout, headers=DEFAULT_HEADERS
    ) as client:
        with client.stream("GET", url) as response:
            status_code = response.status_code
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            return (
                data.decode(encoding, errors="ignore"),
                str(response.url),
                status_code,
            )


def extract_text_from_html(html: str) -> tuple[str | None, str]:
    title = None
    text = ""

    if trafilatura:
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                no_fallback=False,
            )
            if extracted:
                text = extracted
            meta = trafilatura.extract_metadata(html)
            if meta and getattr(meta, "title", None):
                title = meta.title.strip()
        except Exception:
            pass

    if not text:
        soup = _build_soup(html)
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        text = "\n".join(part.strip() for part in soup.stripped_strings)

    return title, text[:200000]


def _build_soup(html: str) -> BeautifulSoup:
    if _looks_like_xml(html):
        try:
            return BeautifulSoup(html, "xml")
        except Exception:
            pass
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        return BeautifulSoup(html, "lxml")


def _looks_like_xml(html: str) -> bool:
    leading = html.lstrip()[:200].lower()
    return (
        leading.startswith("<?xml")
        or leading.startswith("<rss")
        or leading.startswith("<feed")
    )


def fetch_and_extract(url: str, timeout: float, max_bytes: int) -> ExtractedContent:
    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            html, final_url, status_code = fetch_html(
                url,
                timeout=timeout * (1 + (attempt - 1) * 0.5),
                max_bytes=max_bytes,
            )
            result_type = classify_status(status_code, None)
            if result_type == LINK_STATUS_ALIVE:
                title, text = extract_text_from_html(html)
            else:
                title, text = None, ""
            return ExtractedContent(
                title=title,
                text=text,
                status=result_type,
                error=None,
                status_code=status_code,
                final_url=final_url,
            )
        except Exception as exc:
            error = _normalize_error(exc)
            result_type = classify_status(None, error)
            should_retry = attempt < attempts and result_type in TRANSIENT_LINK_RESULTS
            if should_retry:
                continue
            return ExtractedContent(
                title=None,
                text="",
                status=result_type,
                error=error,
                status_code=None,
                final_url=None,
            )

    return ExtractedContent(
        title=None,
        text="",
        status=LINK_STATUS_UNREACHABLE,
        error="Unable to fetch content.",
        status_code=None,
        final_url=None,
    )


def classify_status(status_code: int | None, error: str | None) -> str:
    if error:
        lower = error.lower()
        if _is_untrusted_certificate_error(lower):
            return LINK_STATUS_ALIVE
        if "timed out" in lower or "timeout" in lower:
            return LINK_STATUS_TIMEOUT
        if "name or service not known" in lower or "nodename" in lower:
            return LINK_STATUS_DNS_ERROR
        if "temporary failure in name resolution" in lower:
            return LINK_STATUS_DNS_ERROR
        return LINK_STATUS_UNREACHABLE

    if status_code is None:
        return LINK_STATUS_UNREACHABLE
    if status_code in {404, 410}:
        return LINK_STATUS_NOT_FOUND
    if status_code == 408:
        return LINK_STATUS_TIMEOUT
    if status_code >= 500:
        return LINK_STATUS_SERVER_ERROR
    if 200 <= status_code < 500:
        return LINK_STATUS_ALIVE
    return LINK_STATUS_UNREACHABLE


def _check_link_once(
    client: httpx.Client, url: str
) -> tuple[int | None, str | None, str | None]:
    status_code = None
    final_url = None
    error = None
    try:
        response = client.head(url)
        status_code = response.status_code
        final_url = str(response.url)
        if status_code >= 400 or status_code in {405, 429}:
            response = client.get(url)
            status_code = response.status_code
            final_url = str(response.url)
    except Exception as exc:
        head_error = _normalize_error(exc)
        try:
            response = client.get(url)
            status_code = response.status_code
            final_url = str(response.url)
            error = None
        except Exception as get_exc:
            error = _normalize_error(get_exc) or head_error
    return status_code, final_url, error


def check_link(url: str, timeout: float) -> LinkCheckResult:
    started = time.monotonic()
    attempts = 2
    status_code = None
    final_url = None
    error = None
    result_type = LINK_STATUS_UNREACHABLE

    for attempt in range(1, attempts + 1):
        timeout_value = timeout * (1 + (attempt - 1) * 0.5)
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout_value,
            headers=DEFAULT_HEADERS,
        ) as client:
            status_code, final_url, error = _check_link_once(client, url)
        result_type = classify_status(status_code, error)
        if result_type == LINK_STATUS_ALIVE:
            break
        if attempt < attempts and result_type in TRANSIENT_LINK_RESULTS:
            continue
        break

    latency_ms = int((time.monotonic() - started) * 1000)
    return LinkCheckResult(
        status_code=status_code,
        final_url=final_url,
        result_type=result_type,
        latency_ms=latency_ms,
        error=error,
    )
