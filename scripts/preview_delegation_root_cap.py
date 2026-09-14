"""Offline synthetic preview of production delegation rendering (no account data)."""
import html
import sys
from pathlib import Path

from gateway.delegation_cards import render_card


def fixture():
    rows = {}
    for i, letter in enumerate("ABCDEFG", 1):
        identity = f"task-{letter}"
        rows[identity] = dict(task_label=f"{letter} · Check root group", thread_ref=letter,
                              state="running", presentation_order=i * 10, subagent_type="owner")
        rows[f"child-{letter}"] = dict(task_label=f"Verify child of {letter}", card_parent_identity=identity,
                                       state="completed", presentation_order=i * 10 + 1)
    rows["deep-G"] = dict(task_label="Preserve the complete deeply nested label without shortening",
                          card_parent_identity="child-G", state="failed", presentation_order=72)
    return {"rows": rows}


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    cards = []
    for cap in (5, 2):
        text = render_card(fixture(), max_visible_roots=cap)
        assert text.count("Check root group") == cap
        assert text.count("Verify child") == cap
        assert "without shortening" in text
        (directory / f"cap-{cap}.txt").write_text(text, encoding="utf-8")
        assert text.startswith("○ ")  # First task, not a heading or blank line.
        body = html.escape(text)
        cards.append(f"<section><h2>Root cap {cap}</h2><article>{body}</article></section>")
    page = '''<!doctype html><meta charset="utf-8"><title>Synthetic delegation root window</title>
<style>body{font:16px/1.55 system-ui;margin:24px;background:#f2f2f2;color:#161616}
main{display:flex;gap:24px;align-items:flex-start}section{width:360px}h2{font-size:14px;color:#666}
article{white-space:pre-wrap;overflow-wrap:anywhere;background:white;border-radius:12px;padding:16px}
p{font-size:13px;color:#666}</style><p>Offline synthetic preview · production renderer output · no business data</p><main>'''
    (directory / "index.html").write_text(page + "".join(cards) + "</main>", encoding="utf-8")
    print(directory / "index.html")


if __name__ == "__main__":
    main()
