"""The map's arc simplification (simplifyTopology in site/index.html), run on its own in Node with the vendored d3:
no drawn line moves by more than the tolerance, shared borders stay one arc, no ring turns inside out, and small
islands keep a shape."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site/index.html"
ASSETS = ROOT / "site/assets"
EPS = 0.35


def run(body: str) -> dict:
    source = PAGE.read_text()
    start = source.index("    function simplifyTopology(")
    end = source.index("\n    }\n", start) + len("\n    }\n")  # the function's closing brace, at its own indent
    helper = source[start:end]
    script = f"""
const d3 = require({json.dumps(str(ASSETS / "d3.v7.8.5.min.js"))});
const topojson = require({json.dumps(str(ASSETS / "topojson-client.v3.1.0.min.js"))});
{helper}
// Every original point's distance to the lines d3 draws for its simplified arc (after resampling and clipping).
function drawnPieces(proj, coords) {{
  const pieces = []; let cur;
  d3.geoStream({{ type: "LineString", coordinates: coords }}, proj.stream({{ point(x, y) {{ cur.push([x, y]); }}, lineStart() {{ cur = []; pieces.push(cur); }}, lineEnd() {{}}, polygonStart() {{}}, polygonEnd() {{}}, sphere() {{}} }}));
  return pieces;
}}
function seg2(q, a, b) {{ const dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy, k = l2 ? Math.max(0, Math.min(1, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / l2)) : 0; return (a[0] + k * dx - q[0]) ** 2 + (a[1] + k * dy - q[1]) ** 2; }}
function decode(world) {{ const t = world.transform; return world.arcs.map(arc => {{ let x = 0, y = 0; return arc.map(q => t ? (x += q[0], y += q[1], [x * t.scale[0] + t.translate[0], y * t.scale[1] + t.translate[1]]) : q); }}); }}
function worst(world, simple, proj) {{
  let most = 0;
  decode(world).forEach((full, k) => {{
    const pieces = drawnPieces(proj, simple.arcs[k]);
    full.forEach(q => {{ const p = proj(q); let d = Infinity; pieces.forEach(line => {{ for (let i = 0; i < line.length; i++) d = Math.min(d, seg2(p, line[Math.max(0, i - 1)], line[i])); }}); most = Math.max(most, Math.sqrt(d)); }});
  }});
  return most;
}}
function insideOut(topology) {{ return topojson.feature(topology, topology.objects.countries).features.filter(f => (f.geometry ? d3.geoArea(f) : 0) > 2 * Math.PI).map(f => f.id); }}
{body}
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_world_topology_within_tolerance_for_the_page_views():
    # The World frame of both layouts and the Europe frame of the wide one, as projectionFor builds them (Equal Earth
    # rotated to the box's middle longitude and fitted to it).
    got = run(f"""
const world = JSON.parse(require("fs").readFileSync({json.dumps(str(ASSETS / "world-50m.v2.0.2.json"))}, "utf8"));
const views = {{ "wide World": [720, 500, [[-125, -42], [156, 66]]], "narrow World": [360, 200, [[-125, -42], [156, 66]]], "wide Europe": [720, 500, [[-24, 34], [44, 71]]] }};
const out = {{}};
for (const [name, [W, H, [[a, b], [c, e]]]] of Object.entries(views)) {{
  const mid = (a + c) / 2, proj = d3.geoEqualEarth().rotate([-mid, 0]).fitExtent([[10, 10], [W - 10, H - 10]], {{ type: "MultiPoint", coordinates: [[a, b], [c, b], [a, e], [c, e], [mid, b], [mid, e]] }});
  const simple = simplifyTopology(world, proj, {EPS});
  out[name] = {{ worst: worst(world, simple, proj), arcs: [world.arcs.length, simple.arcs.length], points: [world.arcs.flat().length, simple.arcs.flat().length],
    insideOut: insideOut(simple), before: insideOut(world) }};
}}
console.log(JSON.stringify(out));
""")
    for name, view in got.items():
        assert view["worst"] <= EPS + 1e-9, (name, view["worst"])
        assert view["arcs"][0] == view["arcs"][1], name  # one arc per shared border, so neighbours stay joined
        assert view["insideOut"] == view["before"], name
    # It does simplify: World is the saved drawing; Europe, at about four times the scale, keeps more.
    assert got["wide World"]["points"][1] < got["wide World"]["points"][0] / 2, got["wide World"]["points"]
    assert got["narrow World"]["points"][1] < got["narrow World"]["points"][0] / 3, got["narrow World"]["points"]


def test_hand_cases():
    # A dense run along 49°N (a straight parallel in Equal Earth, which a single great-circle segment would bow away
    # from), a run across the projection's cut at 180°, and a tiny island of one closed arc shared by nothing.
    got = run(f"""
const parallel = Array.from({{ length: 241 }}, (_, i) => [-120 + i * 0.25, 49]);
const across = Array.from({{ length: 81 }}, (_, i) => [170 + i * 0.25, -17 + Math.sin(i) * 0.01]);
const island = Array.from({{ length: 21 }}, (_, i) => [14 + 0.002 * Math.cos(-i * Math.PI / 10), 36 + 0.002 * Math.sin(-i * Math.PI / 10)]);
island[20] = island[0];
const world = {{ type: "Topology", arcs: [parallel, across, island], objects: {{ countries: {{ type: "GeometryCollection", geometries: [{{ type: "Polygon", arcs: [[2]], id: "1" }}] }} }} }};
const proj = d3.geoEqualEarth().fitExtent([[10, 10], [710, 490]], {{ type: "Sphere" }});
const simple = simplifyTopology(world, proj, {EPS});
console.log(JSON.stringify({{ worst: worst(world, simple, proj), counts: simple.arcs.map(a => a.length), closed: simple.arcs[2][0] === simple.arcs[2].at(-1),
  distinct: new Set(simple.arcs[2].map(String)).size, insideOut: insideOut(simple), drawn: d3.geoPath(proj)(topojson.feature(simple, simple.objects.countries)) !== null }}));
""")
    assert got["worst"] <= EPS + 1e-9, got["worst"]
    assert got["counts"][0] < 241 and got["counts"][0] > 2, got["counts"]  # simplified, yet more than the two ends: one segment would bow
    assert got["counts"][1] < 81, got["counts"]
    assert got["closed"] and got["distinct"] >= 3 and got["drawn"], got
    assert got["insideOut"] == []
