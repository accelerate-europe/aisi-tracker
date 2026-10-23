# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Write data/reference/roster.csv: every UN member state plus approved extra territories,
with UN M49 region and sub-region and World Bank latest non-null population.

Run rarely and review the diff: uv run scripts/make_roster.py
UN membership comes from Wikidata (member of United Nations, no end date), checked against an
exact expected count. Region and sub-region come from the UN Statistics Division's M49 overview page
(English table); population comes from the World Bank API. Builds never call the network.
"""

import csv
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "reference" / "roster.csv"
UA = {"User-Agent": "aisi-tracker/1.0 (contact@accelerate-europe.org)"}
UN_MEMBERS = 193

# Wikidata gaps, checked by hand against https://www.un.org/en/about-us/member-states (16 Oct 2026).
UN_OVERRIDES = {"DK": "Denmark"}
M49_URL = "https://unstats.un.org/unsd/methodology/m49/overview/"
# Non-UN jurisdictions we track because readers will look for them. M49 has no separate Taiwan entry,
# so its region is set by hand to the sub-region of its neighbours: (name, region id, region, sub-region id, sub-region).
TERRITORIES = {"TW": ("Taiwan", "142", "Asia", "030", "Eastern Asia")}

SPARQL = """SELECT ?iso2 ?name WHERE {
  ?c p:P463 ?st . ?st ps:P463 wd:Q1065 . FILTER NOT EXISTS { ?st pq:P582 [] }
  ?c wdt:P297 ?iso2 . OPTIONAL { ?c rdfs:label ?name . FILTER(LANG(?name) = "en") }
}"""


def get_json(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def un_members() -> dict[str, str]:
    body = urllib.parse.urlencode({"query": SPARQL}).encode()
    res = get_json("https://query.wikidata.org/sparql", body, {"Accept": "application/sparql-results+json"})
    members = {b["iso2"]["value"]: b.get("name", {}).get("value", "") for b in res["results"]["bindings"]}
    members.update(UN_OVERRIDES)
    if len(members) != UN_MEMBERS:
        sys.exit(f"error: expected {UN_MEMBERS} UN members, got {len(members)}; check Wikidata and UN_OVERRIDES")
    return members


def m49() -> dict[str, dict]:
    req = urllib.request.Request(M49_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        page = r.read().decode("utf-8")
    start = page.index('id = "downloadTableEN"')
    table = page[start:page.index("</table>", start)]

    def cell(c):
        return html.unescape(re.sub(r"<[^>]+>", "", c)).strip()

    out = {}
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]:
        c = [cell(x) for x in re.findall(r"<td>(.*?)</td>", re.sub(r"<!--.*?-->", "", row, flags=re.S), re.S)]
        if len(c) >= 11 and c[10]:
            out[c[10]] = {"m49_region_id": c[2], "m49_region": c[3], "m49_subregion_id": c[4], "m49_subregion": c[5]}
    if len(out) < 240:
        sys.exit(f"error: parsed only {len(out)} M49 rows; the page layout may have changed")
    return out


def world_bank() -> dict:
    pop_rows = get_json("https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?format=json&mrnev=1&per_page=400")[1]
    return {r["country"]["id"]: (int(r["value"]), int(r["date"])) for r in pop_rows if r["value"] is not None}


def main() -> None:
    members = un_members()
    regions, pop = m49(), world_bank()
    rows, missing = [], []
    for iso2, wd_name in sorted(members.items()):
        reg = regions.get(iso2)
        if not reg:
            missing.append(iso2)
            continue
        p = pop.get(iso2)
        rows.append({"id": iso2, "name": wd_name, "kind": "state", "un_member": True, **reg,
                     "population": p[0] if p else "", "population_year": p[1] if p else ""})
    for iso2, (name, rid, rname, sid, sname) in TERRITORIES.items():
        rows.append({"id": iso2, "name": name, "kind": "territory", "un_member": False,
                     "m49_region_id": rid, "m49_region": rname, "m49_subregion_id": sid, "m49_subregion": sname,
                     "population": "", "population_year": ""})
    if missing:
        sys.exit(f"error: UN members without an M49 record: {missing}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    no_pop = [r["id"] for r in rows if r["population"] == ""]
    print(f"ok: {len(rows)} jurisdictions ({len(members)} UN members) -> {OUT.relative_to(ROOT)}; no population: {no_pop}")


if __name__ == "__main__":
    main()
