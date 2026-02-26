from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query_items = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_query = urlencode(query_items)
    return urlunparse((scheme, netloc, path, "", normalized_query, ""))


def parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    tokens = [t.strip().lower() for t in raw.replace(";", ",").split(",")]
    return sorted({t for t in tokens if t})
