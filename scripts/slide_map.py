# /// script
# requires-python = ">=3.11"
# dependencies = ["geopandas", "matplotlib", "pyogrio"]
# ///
"""Render a static map of European AISI status for slides, from data/aisi.json.

  uv run scripts/slide_map.py dist/maps/aisi-europe            # writes .png and .svg
  AISI_MAP_FONTS=/path/to/fonts uv run scripts/slide_map.py ...   # font directory

Boundaries: Natural Earth 1:50m admin-0 countries, downloaded once to work/cache/ (public domain) and checked
against the pinned NE_SHA256; a changed upstream file fails until the pin is reviewed and updated.
The SVG is deterministic: no date metadata and a fixed ID salt.
Categories are deliberately coarse for a slide: established; planned or proposed; none (exploring counts as none).
Countries outside the 32 tracked European states are drawn faint as context.
"""

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.patches import Patch

if __package__:
    from .published import publish
else:
    from published import publish


ROOT = Path(__file__).resolve().parent.parent
NE_URL = "https://naciscdn.org/naturalearth/50m/cultural/ne_50m_admin_0_countries.zip"
NE_ZIP = ROOT / "work" / "cache" / "ne_50m_admin_0_countries.zip"
NE_SHA256 = "5fed433373581fa648920435f937d95f2d3c0200e067409c6478dcdf1b853139"  # retrieved 2026-10-21
NE_TIMEOUT = 60  # seconds per network operation
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

ORANGE, SECOND, GREY, CONTEXT, PAPER, INK = "#B37334", "#F2CFAF", "#E8E3DD", "#F5F4F2", "#FFFFFF", "#1A1A1A"
CONTEXT_A3 = {"RUS", "BLR", "UKR", "MDA", "TUR", "MAR", "DZA", "TUN", "LBY", "EGY", "SYR", "GEO"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boundaries() -> Path:
    """The pinned Natural Earth zip: the cached copy if its hash matches, else a fresh download written atomically."""
    if NE_ZIP.exists() and sha256(NE_ZIP) == NE_SHA256:
        return NE_ZIP
    NE_ZIP.parent.mkdir(parents=True, exist_ok=True)
    partial = NE_ZIP.with_suffix(".part")
    with urllib.request.urlopen(NE_URL, timeout=NE_TIMEOUT) as response:
        partial.write_bytes(response.read())
    if (got := sha256(partial)) != NE_SHA256:
        partial.unlink()
        sys.exit(f"error: {NE_URL} has sha256 {got}, expected {NE_SHA256}; review the new file before updating NE_SHA256")
    partial.replace(NE_ZIP)
    return NE_ZIP


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "maps" / "aisi-europe"
    if os.environ.get("AISI_MAP_FONTS"):
        for f in fm.findSystemFonts([os.path.expanduser(os.environ["AISI_MAP_FONTS"])]):
            fm.fontManager.addfont(f)
    plt.rcParams["font.family"] = ["Source Sans 3", "Inter", "Helvetica", "sans-serif"]
    plt.rcParams["svg.fonttype"] = "none"
    plt.rcParams["svg.hashsalt"] = "aisi-europe"  # fixed element IDs instead of random ones

    d = publish(json.loads((ROOT / "data" / "aisi.json").read_text(encoding="utf-8")))
    europe = {j["id"]: j["status"] for j in d["jurisdictions"] if j.get("europe_scope")}
    est = {k for k, s in europe.items() if s == "established"}
    soon = {k for k, s in europe.items() if s in ("planned", "proposed")}

    w = gpd.read_file(f"zip://{boundaries()}")
    iso2 = w["ISO_A2_EH"]  # ISO_A2_EH fills the -99 gaps (France, Norway) of ISO_A2
    missing = set(europe) - set(iso2)
    if missing:
        sys.exit(f"error: no boundary for {sorted(missing)}")
    w = w.assign(id2=iso2)
    ours = w[w["id2"].isin(europe)].to_crs(3035)
    others = w[(w["CONTINENT"] == "Europe") & ~w["id2"].isin(europe) & ~w["ADM0_A3"].isin({"RUS"})]

    def colour(c: str) -> str:
        return ORANGE if c in est else SECOND if c in soon else GREY

    fig, ax = plt.subplots(figsize=(10, 7.5), dpi=200)
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    w[w["ADM0_A3"].isin(CONTEXT_A3)].to_crs(3035).plot(ax=ax, color=CONTEXT, edgecolor=PAPER, linewidth=0.6)
    others.to_crs(3035).plot(ax=ax, color=CONTEXT, edgecolor=PAPER, linewidth=0.6)
    ours.plot(ax=ax, color=[colour(c) for c in ours["id2"]], edgecolor=PAPER, linewidth=0.6)
    ax.set_xlim(2.6e6, 6.6e6)  # east edge includes Cyprus
    ax.set_ylim(1.35e6, 5.45e6)
    ax.set_aspect("equal")
    ax.set_axis_off()
    y, m, _ = d["dataset"]["as_of"].split("-")
    ax.legend(handles=[Patch(color=ORANGE, label="AISI established"),
                       Patch(color=SECOND, label="Planned or proposed"),
                       Patch(color=GREY, label="None")],
              loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=3, frameon=False, fontsize=17, labelcolor=INK,
              handlelength=1.0, handletextpad=0.5, columnspacing=1.4, borderpad=0.2,
              title=f"European AI Security Institutes, as of {MON[int(m) - 1]} {y}", title_fontsize=14)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out.with_suffix(".png"), bbox_inches="tight", facecolor=PAPER)
    fig.savefig(out.with_suffix(".svg"), bbox_inches="tight", facecolor=PAPER, metadata={"Date": None})
    print(f"ok: {len(est)} established, {len(soon)} planned or proposed -> {out}.png/.svg")


if __name__ == "__main__":
    main()
