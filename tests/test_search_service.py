from types import SimpleNamespace

from app.services.search import search_bookmarks


def _bookmark(
    title: str,
    tags=None,
    content: str = "",
    notes: str = "",
):
    tag_rows = [SimpleNamespace(name=name) for name in (tags or [])]
    content_row = SimpleNamespace(extracted_text=content)
    return SimpleNamespace(
        title=title,
        notes=notes,
        tags=tag_rows,
        content=content_row,
    )


def test_search_filters_irrelevant_items():
    bookmarks = [
        _bookmark("Python docs"),
        _bookmark("Gardening tips"),
        _bookmark("Travel planning"),
    ]

    results = search_bookmarks(bookmarks, "python")

    assert [row["bookmark"].title for row in results] == ["Python docs"]


def test_search_keeps_high_confidence_fuzzy_matches():
    bookmarks = [
        _bookmark("Python documentation"),
        _bookmark("Rust cookbook"),
    ]

    results = search_bookmarks(bookmarks, "pythn")

    assert results
    assert results[0]["bookmark"].title == "Python documentation"


def test_search_matches_notes_text():
    bookmarks = [
        _bookmark("Weekly roundup", notes="This includes release notes for flask 3.1"),
        _bookmark("Other"),
    ]

    results = search_bookmarks(bookmarks, "flask 3.1")

    assert len(results) == 1
    assert results[0]["bookmark"].title == "Weekly roundup"
