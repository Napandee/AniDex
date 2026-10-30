#!/usr/bin/env python3
"""Write a placeholder cover for every title in demo/seed.sql.

demo/covers/<anime_id>.svg: the title's initials on a colour picked from a hash of
the id. Deterministic (same seed, same bytes), no fonts or images from anywhere
else. The demo serves them at /static/demo-covers/<id>.svg (demo/compose.yml
mounts this folder there).

    python3 demo/make_covers.py [seed.sql] [out_dir]
"""

import hashlib
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
TITLE_ROW = re.compile(r"\(\s*(\d+)\s*,\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'")
SMALL_WORDS = {"the", "of", "on", "at", "a", "an", "and", "to", "no", "in"}
W, H = 230, 325


def titles(seed_sql: str) -> list[tuple[int, str]]:
    """(id, english title) for every row of the seed's anime VALUES list."""
    start = seed_sql.index("INSERT INTO anime ")
    section = seed_sql[start:seed_sql.index("ON CONFLICT (id)", start)]
    out = []
    for raw_id, _romaji, english in TITLE_ROW.findall(section):
        out.append((int(raw_id), english.replace("''", "'")))
    return out


def initials(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", title)]
    big = [w for w in words if w.lower() not in SMALL_WORDS] or words
    return "".join(w[0].upper() for w in big[:2])


def hue(anime_id: int) -> int:
    return int(hashlib.sha256(str(anime_id).encode()).hexdigest()[:8], 16) % 360


def cover_svg(anime_id: int, title: str) -> str:
    h = hue(anime_id)
    label = escape(title)
    lines, line = [], ""
    for word in title.split():
        if line and len(line) + 1 + len(word) > 18:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    caption = "".join(
        f'<text x="{W // 2}" y="{250 + i * 20}" font-size="15" text-anchor="middle" '
        f'fill="#ffffff" fill-opacity="0.9">{escape(t)}</text>'
        for i, t in enumerate(lines[:3])
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{label}">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="hsl({h},55%,48%)"/>'
        f'<stop offset="1" stop-color="hsl({(h + 40) % 360},60%,22%)"/>'
        f"</linearGradient></defs>"
        f'<rect width="{W}" height="{H}" fill="url(#g)"/>'
        f'<g font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif" font-weight="700">'
        f'<text x="{W // 2}" y="160" font-size="84" text-anchor="middle" fill="#ffffff">'
        f"{escape(initials(title))}</text>"
        f"{caption}</g></svg>\n"
    )


def main(argv: list[str]) -> int:
    seed = Path(argv[1]) if len(argv) > 1 else HERE / "seed.sql"
    out_dir = Path(argv[2]) if len(argv) > 2 else HERE / "covers"
    rows = titles(seed.read_text())
    if not rows:
        print(f"no titles found in {seed}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    for anime_id, title in rows:
        (out_dir / f"{anime_id}.svg").write_text(cover_svg(anime_id, title))
    print(f"wrote {len(rows)} covers to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
