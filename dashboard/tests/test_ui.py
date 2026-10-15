from dashboard.ui import Tone, escape_markdown, pill


def test_pill_escapes_untrusted_text() -> None:
    html = pill('<img src=x onerror="alert(1)">', Tone.DANGER)
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html


def test_escape_markdown_neutralises_links_code_and_icons() -> None:
    escaped = escape_markdown("[click](http://x) `code` :material/home: *bold*")
    assert escaped == (r"\[click\]\(http\://x\) \`code\` \:material/home\: \*bold\*")
