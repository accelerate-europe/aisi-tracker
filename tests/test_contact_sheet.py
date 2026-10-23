"""Before/after selection for the visual pass: what counts as a changed region."""

import json

from PIL import Image, ImageDraw

from scripts import contact_sheet as sheet


def page():
    image = Image.new("RGB", (400, 600), (250, 247, 242))
    draw = ImageDraw.Draw(image)
    for y in range(20, 580, 30):
        draw.text((20, y), "Status since 21 Sep 2026", fill=(20, 20, 20))
    return image


def regions(before, after):
    return sheet.changed_regions(sheet.change_mask(before, after))


def test_identical_crops_have_no_region():
    assert regions(page(), page()) == []


def test_one_pixel_move_of_a_line_is_found():
    after = page()
    line = after.crop((20, 200, 220, 215))
    after.paste((250, 247, 242), (20, 200, 220, 215))
    after.paste(line, (21, 200))
    [(box, count)] = regions(page(), after)
    assert 18 <= box[0] <= 22 and 198 <= box[1] <= 202 and count > 0


def test_colour_changes_above_noise_are_found_and_below_are_not():
    # Measured noise between launches of one build: 7. A change of 16 must show; one of 8 must not.
    for delta, found in ((16, True), (8, False)):
        after = page()
        ImageDraw.Draw(after).rectangle((300, 300, 380, 340), fill=(250 - delta, 247, 242))
        assert bool(regions(page(), after)) is found


def test_nearby_changes_merge_and_distant_ones_stay_apart():
    near, far = page(), page()
    for image, second_y in ((near, 130), (far, 500)):
        ImageDraw.Draw(image).rectangle((300, 100, 310, 110), fill=(0, 0, 0))
        ImageDraw.Draw(image).rectangle((300, second_y, 310, second_y + 10), fill=(0, 0, 0))
    assert len(regions(page(), near)) == 1
    assert len(regions(page(), far)) == 2


def test_changes_near_the_edge_of_an_odd_width_are_found():
    before = Image.new("RGB", (390, 600), (250, 247, 242))
    for x in range(376, 389):
        after = before.copy()
        ImageDraw.Draw(after).rectangle((x, 300, x + 1, 340), fill=(0, 0, 0))
        [(box, _)] = regions(before, after)
        assert box[0] <= x and box[2] >= x + 2


def test_content_added_below_is_shown(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    page().save(before / "390-light-cards-1-planned.png")
    taller = Image.new("RGB", (400, 800), (250, 247, 242))
    taller.paste(page(), (0, 0))
    ImageDraw.Draw(taller).text((20, 700), "New card", fill=(0, 0, 0))
    taller.save(after / "390-light-cards-1-planned.png")
    regions, compared = sheet.find_regions(before, after)
    assert compared == 1
    assert [r.label for r in regions] == ["390-light-cards-1-planned at x 0, y 600: changed, height +200 px"]


def red(image):
    return image is not None and (255, 0, 0) in {colour for _, colour in image.getcolors(1 << 24)}


def test_removed_content_is_outlined_on_the_before_side(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    taller = Image.new("RGB", (400, 800), (250, 247, 242))
    taller.paste(page(), (0, 0))
    taller.save(before / "390-light-cards-1-planned.png")
    page().save(after / "390-light-cards-1-planned.png")
    page().save(before / "390-light-cards-2-gone.png")
    regions, _ = sheet.find_regions(before, after)
    cells = {r.label: sheet.pair_cells(r.old, r.new, r.crop, r.box) for r in regions}
    shrunk = cells["390-light-cards-1-planned at x 0, y 600: changed, height -200 px"]
    assert red(shrunk[0]) and not red(shrunk[1])
    gone = cells["390-light-cards-2-gone at x 0, y 0: removed"]
    assert red(gone[0]) and gone[1] is None


def marks(tmp_path, name, parts):
    (tmp_path / name.replace(".png", ".json")).write_text(json.dumps({"crop": {"label": "Planned European cards", "anchor": "c-it"}, "landmarks": parts}))


def test_one_card_changed_at_several_widths_and_themes_is_one_change(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    card = {"box": [0, 0, 400, 600], "label": "Italy card", "anchor": "c-it", "family": "state cards", "name": "Italy", "part": None}
    field = {**card, "box": [280, 280, 400, 360], "label": "Italy card, Budget field", "part": "Budget field"}
    for name in ("1440-light-cards-2-planned.png", "1440-dark-cards-2-planned.png", "390-dark-cards-2-planned.png"):
        page().save(before / name)
        changed = page()
        ImageDraw.Draw(changed).rectangle((300, 300, 380, 340), fill=(0, 0, 0))
        ImageDraw.Draw(changed).rectangle((20, 520, 60, 540), fill=(0, 0, 0))  # outside the field, inside the card
        changed.save(after / name)
        marks(after, name, [card, field])
    changes = sheet.group_changes(before, after, sheet.find_regions(before, after)[0])
    # The field and the rest of the card are one element: one change.
    assert [(c.label, c.anchor, c.states(), c.what()) for c in changes] == [
        ("Italy card, Budget field", "c-it", "1440 px light and dark, 390 px dark", "changed"),
    ]
    image = tmp_path / "change.jpg"
    assert sheet.write_change_image(changes[0], image) == [] and image.is_file()


def test_a_region_outside_every_named_part_falls_back_to_the_capture(tmp_path):
    marks(tmp_path, "390-light-cards-2-planned.png", [{"box": [0, 0, 10, 10], "label": "x", "anchor": "x"}])
    found = sheet.landmarks(tmp_path / "none", tmp_path, "390-light-cards-2-planned")
    assert sheet.locate(found, (100, 100, 120, 120)) == {"label": "Planned European cards", "anchor": "c-it"}


def test_the_same_field_changed_in_several_cards_is_one_change(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    parts = [{"box": [0, y, 400, y + 30], "label": f"{name} card, Staff field", "anchor": f"c-{name[:2].lower()}",
              "family": "state cards", "name": name, "part": "Staff field"} for y, name in ((90, "France"), (290, "Germany"), (490, "Spain"))]
    name = "1000-light-cards-1-established.png"
    page().save(before / name)
    changed = page()
    for y in (100, 300, 500):
        ImageDraw.Draw(changed).rectangle((300, y, 320, y + 10), fill=(0, 0, 0))
    changed.save(after / name)
    marks(after, name, parts)
    regions = sheet.find_regions(before, after)[0]
    [change] = sheet.group_changes(before, after, regions)
    assert change.label == "state cards, Staff field (3: France, Germany, Spain)" and change.anchor == "c-fr"
    assert sheet.write_change_image(change, tmp_path / "family.jpg") == []  # every card has its row
    # Two cards are two changes: each may be a separate edit.
    marks(after, name, parts[:2] + [{**parts[2], "family": "other"}])
    assert len(sheet.group_changes(before, after, regions)) == 3


def test_an_edit_above_does_not_hide_content_added_at_the_end(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    page().save(before / "390-light-cards-1-planned.png")
    taller = Image.new("RGB", (400, 800), (250, 247, 242))
    taller.paste(page(), (0, 0))
    ImageDraw.Draw(taller).rectangle((300, 30, 380, 40), fill=(0, 0, 0))
    ImageDraw.Draw(taller).text((20, 700), "New card", fill=(0, 0, 0))
    taller.save(after / "390-light-cards-1-planned.png")
    regions, _ = sheet.find_regions(before, after)
    assert sorted(r.what for r in regions) == ["changed", "changed, height +200 px"]


def test_a_removed_capture_links_to_its_section(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    page().save(before / "390-light-cards-7-planned.png")
    page().save(after / "390-light-hero.png")
    page().save(before / "390-light-hero.png")
    (before / "390-light-cards-7-planned.json").write_text(json.dumps({"crop": {"label": "Planned worldwide cards", "anchor": "c-ke", "section": "world-h"}, "landmarks": []}))
    [change] = sheet.group_changes(before, after, sheet.find_regions(before, after)[0])
    assert (change.label, change.anchor, change.what()) == ("Planned worldwide cards", "world-h", "removed")


def test_sections_sort_in_page_order():
    names = ["funding", "table", "toolbar", "cards-10-x", "cards-2-x", "hero"]
    assert sorted(names, key=sheet.section_order) == ["hero", "cards-2-x", "cards-10-x", "toolbar", "table", "funding"]


def card_parts(*names_and_tops):
    return [{"box": [0, top, 400, top + 150], "label": f"{name} card", "anchor": f"c-{name[:2].lower()}", "family": "state cards", "name": name, "part": None}
            for name, top in names_and_tops]


def test_a_shift_says_what_follows_moves_and_keeps_its_end(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    page().save(before / "390-light-cards-1-planned.png")
    shifted = Image.new("RGB", (400, 620), (250, 247, 242))
    shifted.paste(page().crop((0, 0, 400, 300)), (0, 0))
    shifted.paste(page().crop((0, 300, 400, 600)), (0, 320))
    shifted.save(after / "390-light-cards-1-planned.png")
    marks(after, "390-light-cards-1-planned.png", card_parts(("France", 0), ("Germany", 200), ("Spain", 400)))
    [change] = sheet.group_changes(before, after, sheet.find_regions(before, after)[0])
    assert change.label == "Germany card" and change.what() == "changed, height +20 px, what follows moves"
    assert len(change.regions) == 2


def test_a_region_across_two_cards_names_and_shows_both(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    name = "390-light-cards-1-established.png"
    page().save(before / name)
    changed = page()
    ImageDraw.Draw(changed).rectangle((300, 120, 320, 140), fill=(0, 0, 0))
    ImageDraw.Draw(changed).rectangle((300, 170, 320, 190), fill=(0, 0, 0))  # merges with the first across the gap
    changed.save(after / name)
    marks(after, name, card_parts(("France", 0), ("Germany", 160)))
    regions = sheet.find_regions(before, after)[0]
    assert len(regions) == 1
    changes = sheet.group_changes(before, after, regions)
    assert [c.label for c in changes] == ["France card", "Germany card"]


def test_a_change_image_keeps_every_width_and_theme_beside_every_element(tmp_path):
    regions = []
    for index in range(7):
        for width in sheet.WIDTHS:
            for theme in sheet.THEMES:
                region = sheet.Region(f"{width}-{theme}-table", (0, 0, 10, 10), 100 if width == "1440" and theme == "light" else 1,
                                      "changed", page(), page(), (0, 0, 60, 60))
                region.part = f"row {index}"
                regions.append(region)
    change = sheet.Change("comparison table rows", "world-scroll", regions=regions)
    left_out = sheet.write_change_image(change, tmp_path / "rows.jpg")
    assert len(regions) - len(left_out) == 7 + 5  # one per row, plus the five other widths and themes


def test_cards_appended_below_the_old_end_render_as_absent_before(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    name = "390-light-cards-1-planned.png"
    page().crop((0, 0, 400, 400)).save(before / name)
    taller = Image.new("RGB", (400, 850), (250, 247, 242))
    taller.paste(page().crop((0, 0, 400, 400)), (0, 0))
    ImageDraw.Draw(taller).rectangle((20, 420, 200, 600), fill=(0, 0, 0))
    ImageDraw.Draw(taller).rectangle((20, 670, 200, 840), fill=(0, 0, 0))
    taller.save(after / name)
    marks(after, name, card_parts(("France", 400), ("Germany", 650)))
    changes = sheet.group_changes(before, after, sheet.find_regions(before, after)[0])
    for number, change in enumerate(changes):
        sheet.write_change_image(change, tmp_path / f"{number}.jpg")
    assert [c.label for c in changes] == ["France card", "Germany card"]


def test_a_short_moving_region_is_located_within_itself():
    found = {"crop": {"label": "European state cards", "anchor": "entries"},
             "landmarks": card_parts(("Germany", 600)) + [{"box": [0, 0, 400, 2000], "label": "European state cards", "anchor": "entries"}]}
    # Germany's card ends at 750; probing 48 px from a region starting at 740 would reach past it.
    assert sheet.locate(found, (10, 740, 80, 749), moves=True)["label"] == "Germany card"
