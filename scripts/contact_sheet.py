# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Lay page captures out as small, scannable contact sheets.

  uv run scripts/contact_sheet.py --after DIR --out DIR
  uv run scripts/contact_sheet.py --before DIR --after DIR --out DIR

Captures are named <width>-<theme>-<section>.png (check_page.py --capture), each with a .json of the
named page parts inside it. Writes, into --out:

- change-NN-<slug>.jpg (only with --before): one image per logical change. Regions whose pixels
  differ beyond rendering noise are attributed to the smallest named page part holding them (a
  card field, a table row, a section); regions in the same part at any width or theme are one
  change. Each image shows its regions at full resolution with a margin, before beside after, the
  changed area outlined in red. The pixel difference only chooses what to show; it is never a pass
  or fail.
- sheet-<width>-N.jpg: every section at one width, light beside dark.
- index.json: the changes (plain label, element id, where they show, image) and the sheet pages,
  so a report can list them and link the files.

Pages are at most PAGE_HEIGHT pixels tall and saved as JPEG, so a full set stays a few MB.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

WIDTHS = ("1440", "1000", "390")
THEMES = ("light", "dark")
# Shown width per capture width: desktop text stays readable, phones at full size.
SCALE = {"1440": 0.45, "1000": 0.6, "390": 1.0}
PAGE_HEIGHT = 4000
LABEL = 28
PAD = 14
FONT = ImageFont.load_default(size=17)


def split(name: str) -> tuple[str, str, str]:
    width, theme, section = name.removesuffix(".png").split("-", 2)
    return width, theme, section


def section_order(section: str) -> tuple:
    """Page order: hero, status groups (cards-2 before cards-10), toolbar, table, funding, the rest."""
    parts = section.split("-")
    if section == "hero":
        return (0, 0, section)
    if parts[0] == "cards" and len(parts) > 1 and parts[1].isdigit():
        return (1, int(parts[1]), section)
    later = ("toolbar", "table", "funding")
    return (2, later.index(section) if section in later else len(later), section)


def shrink(image: Image.Image, width: str) -> Image.Image:
    scale = SCALE.get(width, 0.5)
    image = image.convert("RGB")
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))


# check_page.py --capture renders text deterministically; across 8 launches of one build the largest
# remaining channel difference was 7 (hairlines in one long list). A 1 px move of text gives about 220.
NOISE = 12
MARGIN = 48  # context around each changed region, in capture pixels
MERGE = 48  # changed regions closer than this are shown as one
MAX_CROP_HEIGHT = 400  # a change that shifts everything below shows where it starts
MAX_ROWS_PER_CHANGE = 6  # rows per change image, unless it covers more elements
SAME_KIND = 3  # a family of cards or rows changed in this many members is one (style) change


def change_mask(before: Image.Image, after: Image.Image) -> Image.Image:
    """Pixels whose strongest channel differs by more than NOISE (255) or not (0)."""
    red, green, blue = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).split()
    strongest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return strongest.point(lambda v: 255 if v > NOISE else 0)


def changed_regions(mask: Image.Image) -> list[tuple[tuple[int, int, int, int], int]]:
    """Boxes around groups of changed pixels, nearby groups merged, with their changed-pixel counts."""
    cell = 8
    # Pad to whole cells so every grid cell maps back to exactly cell x cell pixels.
    padded = Image.new("L", (-(-mask.width // cell) * cell, -(-mask.height // cell) * cell), 0)
    padded.paste(mask, (0, 0))
    grid = padded.resize((padded.width // cell, padded.height // cell), Image.Resampling.BOX).point(lambda v: 255 if v else 0)
    reach = MERGE // cell
    width, height = grid.size
    marked = grid.load()
    seen, regions = set(), []
    for y in range(height):
        for x in range(width):
            if not marked[x, y] or (x, y) in seen:
                continue
            stack, cells = [(x, y)], []
            seen.add((x, y))
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for nx in range(max(0, cx - reach), min(width, cx + reach + 1)):
                    for ny in range(max(0, cy - reach), min(height, cy + reach + 1)):
                        if marked[nx, ny] and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
            xs, ys = [c[0] for c in cells], [c[1] for c in cells]
            area = (min(xs) * cell, min(ys) * cell, min(mask.width, (max(xs) + 1) * cell), min(mask.height, (max(ys) + 1) * cell))
            box = mask.crop(area).getbbox()
            if box:
                tight = (area[0] + box[0], area[1] + box[1], area[0] + box[2], area[1] + box[3])
                regions.append((tight, mask.crop(tight).histogram()[255]))
    return regions


Row = tuple[str, list[Image.Image | None]]


def draw_page(rows: list[Row], columns: list[str], path: Path, cell: int | None = None) -> None:
    """Draw labelled rows of images in columns onto one JPEG."""
    cell = cell or max(c.width for _, cells in rows for c in cells if c)
    # Row labels span the columns; a narrow crop must not cut them off.
    cell = max(cell, max(round(FONT.getlength(label)) for label, _ in rows) // len(columns) + PAD)
    heights = [LABEL + max((c.height for c in cells if c), default=40) + PAD for _, cells in rows]
    sheet = Image.new("RGB", (PAD + len(columns) * (cell + PAD), LABEL + PAD + sum(heights)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, column in enumerate(columns):
        draw.text((PAD + i * (cell + PAD), 6), column, fill="black", font=FONT)
    y = LABEL + PAD
    for (label, cells), row_height in zip(rows, heights):
        draw.text((PAD, y), label, fill="black", font=FONT)
        for i, image in enumerate(cells):
            x = PAD + i * (cell + PAD)
            if image is None:
                draw.rectangle((x, y + LABEL, x + cell, y + LABEL + 40), outline="grey")
                draw.text((x + 6, y + LABEL + 10), "absent", fill="grey", font=FONT)
            else:
                sheet.paste(image, (x, y + LABEL))
                draw.rectangle((x - 1, y + LABEL - 1, x + image.width, y + LABEL + image.height), outline="#bbbbbb")
        y += row_height
    sheet.save(path, quality=75, optimize=True)


def paginate(rows: list[Row], columns: list[str], stem: Path) -> list[tuple[str, list[str]]]:
    """Write rows onto pages of at most PAGE_HEIGHT pixels; return (file name, row labels) per page."""
    if not rows:
        return []
    cell = max(c.width for _, cells in rows for c in cells if c)
    pages, current, height = [], [], LABEL + PAD
    for row in rows:
        row_height = LABEL + max((c.height for c in row[1] if c), default=40) + PAD
        if current and height + row_height > PAGE_HEIGHT:
            pages.append(current)
            current, height = [], LABEL + PAD
        current.append(row)
        height += row_height
    pages.append(current)
    written = []
    for number, page_rows in enumerate(pages, 1):
        name = f"{stem.name}-{number}.jpg"
        draw_page(page_rows, columns, stem.parent / name, cell)
        written.append((name, [label for label, _ in page_rows]))
    return written


def width_rows(after: Path, width: str) -> list[Row]:
    shots = {split(p.name): p for p in after.glob("*.png")}
    rows = []
    for section in sorted({s for w, _, s in shots if w == width}, key=section_order):
        cells = [shots.get((width, theme, section)) for theme in THEMES]
        rows.append((section, [shrink(Image.open(c), width) if c else None for c in cells]))
    return rows


def change_order(name: str) -> tuple:
    width, _, section = split(name)
    return (WIDTHS.index(width) if width in WIDTHS else len(WIDTHS), section_order(section), name)


def pair_cells(old: Image.Image | None, new: Image.Image | None, crop: tuple, box: tuple) -> list[Image.Image | None]:
    """Before and after pieces of one region; the side that still has the changed area is outlined
    (after, unless the content was removed and exists only before)."""
    marked = new if new is not None and box[1] < new.height else old
    cells = []
    for image in (old, new):
        if image is None or crop[1] >= image.height:  # absent on this side, or below its end
            cells.append(None)
            continue
        piece = image.crop((*crop[:3], min(crop[3], image.height)))
        if image is marked:
            bottom = min(box[3], crop[3], image.height) - crop[1] - 1
            ImageDraw.Draw(piece).rectangle((box[0] - crop[0], box[1] - crop[1], min(box[2], crop[2]) - crop[0] - 1, bottom), outline="red", width=2)
        cells.append(piece)
    return cells


@dataclass(eq=False)  # regions are distinct objects; comparing their images would be slow
class Region:
    """One changed region in one capture."""

    name: str  # capture name without .png, e.g. 1440-light-cards-1-established
    box: tuple[int, int, int, int]
    count: int  # changed pixels; orders regions by size
    kind: str  # changed, added, removed (a whole capture) or resized (its width)
    old: Image.Image | None
    new: Image.Image | None
    crop: tuple[int, int, int, int]
    grown: int = 0  # height change of the capture, on the region at its end
    moves: bool = False  # the region runs to the end of the common area: what follows moves
    tail_of: "Region | None" = None  # the end of a capture whose change starts at that region
    part: str = ""  # label of the page part it fell in

    @property
    def what(self) -> str:
        words = [self.kind] + ([f"height {self.grown:+d} px"] if self.grown else []) + (["what follows moves"] if self.moves else [])
        return ", ".join(words)

    @property
    def state(self) -> tuple[str, str]:
        width, theme, _ = split(self.name)
        return width, theme

    @property
    def label(self) -> str:
        return f"{self.name} at x {self.box[0]}, y {self.box[1]}: {self.what}"


def landmarks(before: Path, after: Path, name: str) -> dict:
    """Named page parts in the capture (check_page.py writes them beside each PNG); the after
    capture when it exists, since that is the page under review."""
    for side in (after, before):
        path = side / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"crop": {"label": split(name)[2], "anchor": "top"}, "landmarks": []}


def area(box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def overlap(a, b) -> int:
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def locate(marks: dict, box: tuple, moves: bool = False) -> dict:
    """The smallest named page part holding the region (for a region that runs on, its first rows),
    else the part it overlaps most, else the capture itself. A part has a plain label and an element
    id (anchor); a repeated part (card field, table row) also a kind and the name of its card or row."""
    probe = box if box[3] - box[1] <= MAX_CROP_HEIGHT and not moves else (box[0], box[1], box[2], min(box[3], box[1] + MERGE))
    parts = marks["landmarks"]
    holding = [m for m in parts if overlap(m["box"], probe) == area(probe) > 0]
    if holding:
        best = min(holding, key=lambda m: area(m["box"]))
    else:
        touching = [m for m in parts if overlap(m["box"], probe) > 0]
        if not touching:
            return marks["crop"]
        best = max(touching, key=lambda m: (overlap(m["box"], probe), -area(m["box"])))
    return best


def find_regions(before: Path, after: Path) -> tuple[list[Region], int]:
    """Every changed region in every capture pair."""
    names = sorted({p.name for p in before.glob("*.png")} | {p.name for p in after.glob("*.png")}, key=change_order)
    regions = []
    for name in names:
        old = Image.open(before / name).convert("RGB") if (before / name).exists() else None
        new = Image.open(after / name).convert("RGB") if (after / name).exists() else None
        if old is None or new is None or old.width != new.width:
            whole = (0, 0, *(new or old).size)
            whole = (*whole[:3], min(whole[3], MAX_CROP_HEIGHT))
            kind = "added" if old is None else "removed" if new is None else "resized"
            regions.append(Region(name.removesuffix(".png"), whole, area(whole), kind, old, new, whole))
            continue
        # A height change shifts everything below it: compare the common area to find where it
        # starts, and always show the end, where content was added or removed. When a region runs
        # on to the end, the end belongs to its change.
        common = min(old.height, new.height)
        found = changed_regions(change_mask(old.crop((0, 0, old.width, common)), new.crop((0, 0, new.width, common))))
        grown = new.height - old.height
        moving = None
        for box, count in found:
            moves = bool(grown) and box[3] >= common - MERGE
            region = Region(name.removesuffix(".png"), box, count, "changed", old, new, crop_for(box, new.width), moves=moves)
            moving = moving or (region if moves else None)  # the earliest start of the shift
            regions.append(region)
        if grown:
            box = (0, common, new.width, max(old.height, new.height))
            regions.append(Region(name.removesuffix(".png"), box, abs(grown) * new.width, "changed", old, new, crop_for(box, new.width), grown, tail_of=moving))
    return regions, len(names)


def crop_for(box: tuple, width: int) -> tuple:
    """The region with a margin, at most MAX_CROP_HEIGHT tall."""
    crop = (max(0, box[0] - MARGIN), max(0, box[1] - MARGIN), min(width, box[2] + MARGIN), box[3] + MARGIN)
    return (*crop[:3], min(crop[3], crop[1] + MAX_CROP_HEIGHT))


def split_by_element(region: Region, marks: dict) -> list[Region]:
    """A region spanning several cards or rows (nearby changes merge) as one region per element,
    so that each is named and shown; a region that runs on to the end stays whole."""
    if region.kind != "changed" or region.moves or region.tail_of:
        return [region]
    members = [m for m in marks["landmarks"] if m.get("family") and not m.get("part") and overlap(m["box"], region.box) > 0]
    if len(members) < 2:
        return [region]
    pieces = []
    for member in members:
        box = (max(region.box[0], member["box"][0]), max(region.box[1], member["box"][1]),
               min(region.box[2], member["box"][2]), min(region.box[3], member["box"][3]))
        count = max(1, region.count * area(box) // max(1, area(region.box)))
        pieces.append(replace(region, box=box, count=count, crop=crop_for(box, region.new.width)))
    return pieces


@dataclass
class Change:
    """One logical change: the regions that fall in the same named page part, at any width or theme."""

    label: str
    anchor: str
    family: str | None = None  # repeated parts, e.g. "state cards" or "comparison table rows"
    names: list[str] = field(default_factory=list)  # the cards or rows of that family it covers
    parts: list[str] = field(default_factory=list)  # the fields within them, e.g. "Budget field"
    regions: list[Region] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)  # labels of the parts its regions fell in

    def states(self) -> str:
        """Where it shows, e.g. "1440 px light and dark, 390 px dark"."""
        seen = {r.state for r in self.regions}
        if seen >= {(w, t) for w in WIDTHS for t in THEMES}:
            return "every width, light and dark"
        parts = []
        for width in sorted({w for w, _ in seen}, key=lambda w: WIDTHS.index(w) if w in WIDTHS else len(WIDTHS)):
            themes = [t for t in THEMES if (width, t) in seen]
            parts.append(f"{width} px {' and '.join(themes)}")
        return ", ".join(parts)

    def what(self) -> str:
        """Plain words across its regions, e.g. "changed, height +24 / +48 px, what follows moves"."""
        words = list(dict.fromkeys(r.kind for r in self.regions))
        heights = sorted({r.grown for r in self.regions if r.grown})
        if heights:
            words.append("height " + " / ".join(f"{h:+d}" for h in heights) + " px")
        if any(r.moves for r in self.regions):
            words.append("what follows moves")
        return ", ".join(words)


def within(box: tuple, image: Image.Image) -> tuple:
    return (max(0, box[0]), max(0, box[1]), min(image.width, box[2]), min(image.height, box[3]))


def group_changes(before: Path, after: Path, regions: list[Region]) -> list[Change]:
    """Regions in the same named page part are one change. When one family of repeated parts
    (cards, table rows) changed in SAME_KIND or more of its members, typically a style change, the
    whole family is one change. In page order."""
    changes: dict[tuple, Change] = {}
    keys: dict[int, tuple] = {}
    pieces = [piece for region in regions for piece in split_by_element(region, landmarks(before, after, region.name))]
    for region in sorted(pieces, key=lambda r: r.tail_of is not None):  # a change's start before its end
        marks = landmarks(before, after, region.name)
        if region.tail_of is not None and id(region.tail_of) in keys:
            region.part = f"{region.tail_of.part}, end of the section"
            changes[keys[id(region.tail_of)]].regions.append(region)
            continue
        part = marks["crop"] if region.kind != "changed" else locate(marks, region.box, region.moves)
        if region.kind == "removed":  # its own element is gone from the page under review
            part = {**part, "anchor": part.get("section", "top")}
        region.part = part["label"]
        if "box" in part and region.new is not None and area(part["box"]) < area((0, 0, *region.new.size)):
            # Show the whole part (the card field, the row) around the region, for context.
            image = region.new
            wide = within((part["box"][0] - PAD, part["box"][1] - PAD, part["box"][2] + PAD, part["box"][3] + PAD), image)
            top, bottom = region.crop[1], region.crop[3]
            if wide[3] - wide[1] <= MAX_CROP_HEIGHT:  # a taller part only widens the crop
                top, bottom = min(top, wide[1]), min(max(bottom, wide[3]), min(top, wide[1]) + MAX_CROP_HEIGHT)
            region.crop = (min(region.crop[0], wide[0]), top, max(region.crop[2], wide[2]), bottom)
        # One card or row is one element, whichever of its fields changed.
        key = (part["family"], part["name"]) if part.get("family") else (part["label"], part["anchor"])
        if key not in changes:
            changes[key] = Change(part["label"], part["anchor"], part.get("family"), [part["name"]] if part.get("name") else [])
        change = changes[key]
        change.regions.append(region)
        keys[id(region)] = key
        change.labels = list(dict.fromkeys([*change.labels, part["label"]]))
        if part.get("part") and part["part"] not in change.parts:
            change.parts.append(part["part"])
    for change in changes.values():
        if len(change.labels) > 1:  # the card itself and some of its fields: "Sweden card, Finding field"
            element = min(change.labels, key=len).split(", ")[0]
            change.label = element + (f", {and_list(change.parts)}" if change.parts else "")
    families: dict[str, list[Change]] = {}
    for change in changes.values():
        if change.family:
            families.setdefault(change.family, []).append(change)
    for family, members in families.items():
        names = list(dict.fromkeys(n for c in sorted(members, key=page_order) for n in c.names))
        if len(names) < SAME_KIND:
            continue
        parts = list(dict.fromkeys(p for c in sorted(members, key=page_order) for p in c.parts))
        shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
        label = family + (f", {and_list(parts)}" if parts else "") + f" ({len(names)}: {shown})"
        first = min(members, key=page_order)
        merged = Change(label, first.anchor, family, names, parts, [r for c in members for r in c.regions])
        for key in [k for k, c in changes.items() if c in members]:
            del changes[key]
        changes[(family, None)] = merged
    return sorted(changes.values(), key=page_order)


def and_list(items: list[str]) -> str:
    """"Budget field", "Staff field" -> "Budget and Staff fields"."""
    if len(items) == 1:
        return items[0]
    if all(i.endswith(" field") for i in items):
        stems = [i.removesuffix(" field") for i in items]
        return ", ".join(stems[:-1]) + f" and {stems[-1]} fields"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def page_order(change: "Change") -> tuple:
    first = min(change.regions, key=lambda r: (section_order(split(r.name)[2]), change_order(r.name)))
    return (section_order(split(first.name)[2]), first.box[1])


def slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))[:48].strip("-") or "change"


def write_change_image(change: Change, path: Path) -> list[str]:
    """One full-resolution before/after image for one change: the largest region of every element
    it covers (each card or row of a merged family) and of every width and theme, then more up to
    MAX_ROWS_PER_CHANGE rows; returns the regions left out."""
    ordered = sorted(change.regions, key=lambda r: -r.count)
    elements = list({r.part: r for r in reversed(ordered)}.values())
    states = [r for r in {r.state: r for r in reversed(ordered)}.values() if r not in elements]
    rest = [r for r in ordered if r not in elements and r not in states]
    picked = sorted(elements, key=lambda r: -r.count) + sorted(states, key=lambda r: -r.count)
    picked += rest[: max(0, MAX_ROWS_PER_CHANGE - len(picked))]
    shown = sorted(picked, key=lambda r: change_order(r.name))
    rows = [(r.label, pair_cells(r.old, r.new, r.crop, r.box)) for r in shown]
    draw_page(rows, ["before", "after (changed area outlined)"], path)
    return [r.label for r in ordered if r not in picked]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--after", type=Path, required=True)
    ap.add_argument("--before", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="directory for the sheets and index.json")
    args = ap.parse_args()
    if not any(args.after.glob("*.png")):
        print(f"error: no captures in {args.after}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    index = {"changes": [], "compared": 0, "sheets": {}}
    if args.before:
        regions, index["compared"] = find_regions(args.before, args.after)
        changes = group_changes(args.before, args.after, regions)
        for number, change in enumerate(changes, 1):
            image = f"change-{number:02d}-{slug(change.label)}.jpg"
            left_out = write_change_image(change, args.out / image)
            index["changes"].append({"number": number, "label": change.label, "anchor": change.anchor, "what": change.what(),
                                     "states": change.states(), "image": image, "regions": [r.label for r in change.regions], "left_out": left_out})
        print(f"{len(changes)} changes ({len(regions)} changed regions in {index['compared']} crops)")
    for width in WIDTHS:
        index["sheets"][width] = paginate(width_rows(args.after, width), [f"{width} px {t}" for t in THEMES], args.out / f"sheet-{width}")
    (args.out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"sheets: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
