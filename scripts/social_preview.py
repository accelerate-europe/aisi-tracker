"""Generate site/social-preview.png: uv run --locked scripts/social_preview.py.

Uses the page title and dataset credits. The bundled, unmodified Inter Regular
and Bold fonts are version 4.001 (git-9221beed3), Copyright 2016 The Inter Project
Authors, under SIL OFL 1.1; see fonts/OFL.txt. Their embedded licence and version
metadata identify the installed originals; SHA-256 pins below prevent silent
substitution. Upstream: https://github.com/rsms/inter/tree/9221beed3

Pillow is already pinned in the repository's uv.lock. No network or system fonts
are needed. The PNG has no timestamp; repeat runs in the locked environment on
the same platform produce identical bytes. Regenerate when publisher or
maintainer credits change as well as when the page title changes.
"""

import argparse
import hashlib
import html
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
FONTS = {
    "Regular": "d4f2b9e148059a15f014cb0f0b8fea8cd11bfa447dd483bedf1b0adc0e2ba799",
    "Bold": "9fc6261e817d0b1b2bf1953d8799bb1c837d11aeebbf83ae352e7b4d6de6425d",
}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    path = Path(__file__).parent / "fonts" / f"Inter-{weight}.otf"
    if hashlib.sha256(path.read_bytes()).hexdigest() != FONTS[weight]:
        raise ValueError(f"Font checksum mismatch: {path.name}")
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)


def generate(output: Path) -> None:
    page = (ROOT / "site/index.html").read_text(encoding="utf-8")
    title = html.unescape(re.search(r"<title>([^<]+)</title>", page)[1])
    data = json.loads((ROOT / "data/aisi.json").read_text(encoding="utf-8"))["dataset"]
    image = Image.new("RGB", (1200, 630), "#faf7f2")
    draw = ImageDraw.Draw(image)
    draw.rectangle((72, 72, 1128, 79), fill="#bd783c")
    heading = font("Bold", 96)
    lines = []
    line = ""
    for word in title.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=heading) > 1056 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    if len(lines) > 2 or any(
        draw.textlength(line, font=heading) > 1056 for line in lines
    ):
        raise ValueError("Page title does not fit the social image")
    draw.multiline_text(
        (72, 140), "\n".join(lines), font=heading, fill="#1b1b1b", spacing=16
    )
    credit = "Compiled by " + ", ".join(
        person["name"] for person in data["maintainers"]
    )
    for text, size in ((data["publisher"], 34), (credit, 27)):
        if draw.textlength(text, font=font("Regular", size)) > 1056:
            raise ValueError(f"Credit line does not fit the social image: {text!r}")
    draw.text((76, 452), data["publisher"], font=font("Regular", 34), fill="#1b1b1b")
    draw.text((76, 506), credit, font=font("Regular", 27), fill="#706a63")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Title", title)
    image.save(output, pnginfo=metadata)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "site/social-preview.png")
    generate(parser.parse_args().out)
