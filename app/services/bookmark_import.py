from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from bs4 import BeautifulSoup, Tag


@dataclass
class ImportedBookmark:
    title: str
    url: str
    folder_path: list[str]


def _iter_dt_entries(dl: Tag) -> list[Tag]:
    entries: list[Tag] = []
    for dt in dl.find_all("dt"):
        if not isinstance(dt, Tag):
            continue
        parent_dl = dt.find_parent("dl")
        if parent_dl is dl:
            entries.append(cast(Tag, dt))
    return entries


def _find_nested_dl(dt: Tag) -> Tag | None:
    nested = dt.find("dl")
    if isinstance(nested, Tag):
        return nested

    sibling = dt.next_sibling
    while sibling is not None:
        if isinstance(sibling, Tag):
            name = (sibling.name or "").lower()
            if name == "dl":
                return sibling
            if name == "dt":
                return None
        sibling = sibling.next_sibling
    return None


def _find_anchor_in_dt(dt: Tag) -> Tag | None:
    for anchor in dt.find_all("a"):
        if isinstance(anchor, Tag) and anchor.find_parent("dt") is dt:
            return anchor
    return None


def _find_folder_in_dt(dt: Tag) -> Tag | None:
    for folder in dt.find_all(["h3", "h2", "h1"]):
        if isinstance(folder, Tag) and folder.find_parent("dt") is dt:
            return folder
    return None


def _parse_dl(dl: Tag, folder_path: list[str], out: list[ImportedBookmark]) -> None:
    for dt in _iter_dt_entries(dl):
        anchor = _find_anchor_in_dt(dt)
        if isinstance(anchor, Tag):
            href_value = anchor.get("href")
            href = href_value.strip() if isinstance(href_value, str) else ""
        else:
            href = ""

        if href:
            text = anchor.get_text(strip=True) if isinstance(anchor, Tag) else ""
            out.append(
                ImportedBookmark(
                    title=text.strip(),
                    url=href,
                    folder_path=folder_path.copy(),
                )
            )

        nested_dl = _find_nested_dl(dt)
        folder = _find_folder_in_dt(dt)
        if folder is None and nested_dl is not None:
            for heading in dt.find_all(["h3", "h2", "h1"]):
                if isinstance(heading, Tag):
                    folder = heading
                    break

        if folder and nested_dl:
            name = folder.get_text(strip=True)
            _parse_dl(nested_dl, folder_path + [name], out)


def parse_bookmark_html(html: str) -> list[ImportedBookmark]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("dl")
    if not isinstance(root, Tag):
        return []

    bookmarks: list[ImportedBookmark] = []
    _parse_dl(root, [], bookmarks)
    return [bm for bm in bookmarks if bm.url]
