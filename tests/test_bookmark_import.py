from app.services.bookmark_import import parse_bookmark_html


def test_parse_bookmark_html_handles_nested_netscape_structure():
    html = """
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Root Folder</H3>
  <DL><p>
    <DT><A HREF="https://example.com/a">A</A>
    <DT><H3>Inner Folder</H3>
    <DL><p>
      <DT><A HREF="https://example.com/b">B</A>
      <DT><A HREF="https://example.com/c#frag">C</A>
    </DL><p>
  </DL><p>
  <DT><A HREF="https://example.com/root">Root Link</A>
</DL><p>
"""

    rows = parse_bookmark_html(html)
    urls = [row.url for row in rows]
    assert urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c#frag",
        "https://example.com/root",
    ]

    assert rows[0].folder_path == ["Root Folder"]
    assert rows[1].folder_path == ["Root Folder", "Inner Folder"]
    assert rows[2].folder_path == ["Root Folder", "Inner Folder"]
    assert rows[3].folder_path == []


def test_parse_bookmark_html_keeps_empty_title_when_anchor_has_no_text():
    html = """
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><A HREF="https://example.com/no-title"></A>
</DL><p>
"""

    rows = parse_bookmark_html(html)
    assert len(rows) == 1
    assert rows[0].url == "https://example.com/no-title"
    assert rows[0].title == ""
