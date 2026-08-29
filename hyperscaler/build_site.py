"""Render the commitments page from its template and the dataset.

Emits two files from one source so the repo site and the published artifact can
never drift: ``web/index.html`` (a complete document) and ``output/artifact.html``
(the same page as a body fragment, which is what the Artifact host wraps).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "web" / "page.template.html"
DATA = ROOT / "data" / "commitments.json"
SITE = ROOT / "web" / "index.html"
FRAGMENT = ROOT / "output" / "artifact.html"

DOC_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%231f3a5f'/%3E%3Cpath d='M7 24h4v3H7zm6-5h4v8h-4zm6-6h4v14h-4zm6-5h4v19h-4z' fill='%23fff'/%3E%3C/svg%3E">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
"""


def main() -> None:
    payload = json.dumps(json.loads(DATA.read_text()), separators=(",", ":"))
    # The JSON lands inside a <script> block, so the one sequence that could end
    # that block early has to be broken up.
    payload = payload.replace("</", "<\\/")
    body = TEMPLATE.read_text().replace("__DATA__", payload)

    FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
    FRAGMENT.write_text(body)

    head_end = body.index("<div class=\"wrap\">")
    head, rest = body[:head_end], body[head_end:]
    SITE.write_text(f"{DOC_HEAD}{head}</head>\n<body>\n{rest}\n</body>\n</html>\n")

    print(f"wrote {SITE} ({SITE.stat().st_size:,} bytes)")
    print(f"wrote {FRAGMENT} ({FRAGMENT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
