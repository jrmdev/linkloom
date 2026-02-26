from __future__ import annotations

from rapidfuzz import fuzz


def _safe(value: str | None) -> str:
    return (value or "").strip()


def score_bookmark(bookmark, query: str) -> tuple[float, list[str]]:
    q = query.strip().lower()
    title = _safe(bookmark.title)
    notes = _safe(getattr(bookmark, "notes", ""))
    tags = " ".join(tag.name for tag in bookmark.tags)
    content = _safe(bookmark.content.extracted_text if bookmark.content else "")

    score = 0.0
    reasons: list[str] = []

    title_l = title.lower()
    tags_l = tags.lower()
    notes_l = notes.lower()
    content_l = content.lower()

    if q == title_l:
        score += 150
        reasons.append("exact_title")
    elif title_l.startswith(q):
        score += 120
        reasons.append("title_prefix")
    elif q in title_l:
        score += 100
        reasons.append("title_contains")

    if q in tags_l:
        score += 90
        reasons.append("tag_match")

    if content_l and q in content_l:
        score += 45
        reasons.append("content_contains")

    if notes_l and q in notes_l:
        score += 35
        reasons.append("notes_contains")

    fuzzy_title = fuzz.partial_ratio(q, title_l) if title_l else 0
    if fuzzy_title >= 72:
        score += fuzzy_title * 0.30
        reasons.append("title_fuzzy")

    fuzzy_meta = fuzz.partial_ratio(q, tags_l)
    if fuzzy_meta >= 80:
        score += fuzzy_meta * 0.20
        reasons.append("meta_fuzzy")

    if content_l and len(q) >= 4:
        fuzzy_content = fuzz.partial_ratio(q, content_l[:6000])
        if fuzzy_content >= 88:
            score += fuzzy_content * 0.20
            reasons.append("content_fuzzy")

    if notes_l and len(q) >= 4:
        fuzzy_notes = fuzz.partial_ratio(q, notes_l[:6000])
        if fuzzy_notes >= 88:
            score += fuzzy_notes * 0.16
            reasons.append("notes_fuzzy")

    return score, reasons


def search_bookmarks(bookmarks, query: str, limit: int = 50):
    if not query or not query.strip():
        return []

    ranked = []
    for bookmark in bookmarks:
        score, reasons = score_bookmark(bookmark, query)
        if reasons and score > 0:
            ranked.append(
                {"bookmark": bookmark, "score": round(score, 2), "reasons": reasons}
            )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]
