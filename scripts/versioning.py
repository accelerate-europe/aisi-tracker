"""Version state and the date boundary shared by validation and publication."""

import datetime as dt
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    number: tuple[int, int, int]
    development: bool

    @property
    def next_patch(self) -> str:
        major, minor, patch = self.number
        return f"{major}.{minor}.{patch + 1}-dev"


def parse_version(value: str) -> Version:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(-dev)?", value)
    if match is None:
        raise ValueError(f"dataset.version {value!r} is not MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-dev")
    return Version(tuple(int(match[n]) for n in (1, 2, 3)), match[4] is not None)


def require_release(version: str) -> None:
    if parse_version(version).development:
        raise ValueError(f"development version {version} cannot be released; remove -dev in the release commit")


def check_released_version(version: str, ref: str, tag_commit: str | None, head: str) -> list[str]:
    state = parse_version(version)
    if ref in {"main", "refs/heads/main"} or state.development or tag_commit in {None, head}:
        return []
    return [f"{ref.removeprefix('refs/heads/')} carries released version {version}; set {state.next_patch}"]


def release_metadata(data: dict) -> dict:
    """The citable version and date; all other metadata stays current."""
    metadata = data["dataset"]
    if not parse_version(metadata["version"]).development:
        return metadata
    if not data.get("changelog"):
        raise ValueError("a development version needs a last released changelog entry")
    last = data["changelog"][-1]
    require_release(last["version"])
    if parse_version(last["version"]).number >= parse_version(metadata["version"]).number:
        raise ValueError("the last changelog version must be less than the planned development version")
    return {**metadata, "version": last["version"], "as_of": last["date"]}


def utc_date(timestamp: str) -> str:
    """The UTC date of an RFC 3339 timestamp; malformed inputs remain visible to validation."""
    try:
        moment = dt.datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", timestamp).upper())
    except ValueError:
        return timestamp[:10]
    if moment.tzinfo is None:
        return moment.date().isoformat()
    return moment.astimezone(dt.timezone.utc).date().isoformat()


def work_dates(data: dict, rereads: dict):
    """Recorded work only: no event, fiscal, occurrence or generated expiry dates."""
    for collection in ("jurisdictions", "institutes"):
        for record in data.get(collection, []):
            where = f"{collection}/{record['id']}"
            check = record.get("check", {})
            yield f"{where}/check/last_checked", check.get("last_checked")
            for field in ("partial", "searches"):
                for n, item in enumerate(check.get(field, [])):
                    yield f"{where}/check/{field}/{n}/date", item.get("date")
            for field in ("model_access", "testing_capacity", "staffing_model"):
                for n, item in enumerate((record.get(field) or {}).get("searches", [])):
                    yield f"{where}/{field}/searches/{n}/date", item.get("date")
    for source in data.get("sources", []):
        where = f"sources/{source['id']}"
        for field in ("accessed", "instrument_status_checked"):
            yield f"{where}/{field}", source.get(field)
        retrieved = (source.get("snapshot") or {}).get("retrieved")
        if retrieved:
            yield f"{where}/snapshot/retrieved", utc_date(retrieved)
        for n, excerpt in enumerate(source.get("excerpts", [])):
            yield f"{where}/excerpts/{n}/accessed", excerpt.get("accessed")
    yield "rereads/checked_to", rereads.get("checked_to")
    for source_id, quotes in rereads.get("sources", {}).items():
        for quote_id, entry in quotes.items():
            for field in ("last_found", "since", "last_attempt"):
                yield f"rereads/{source_id}/{quote_id}/{field}", entry.get(field)


def effective_date(data: dict, rereads: dict, *, today: dt.date | None = None) -> str:
    """Compute once at the input boundary, before date-sensitive validation or derivation.

    Stable input keeps its authored date and its existing validation. Development
    input refuses each future work date, including raw reread entries.
    """
    baseline = data["dataset"]["as_of"]
    if not parse_version(data["dataset"]["version"]).development:
        return baseline
    if today is None:
        if __package__:
            from .release_checks import today as run_today
        else:
            from release_checks import today as run_today
        today = run_today()
    dates = [baseline]
    for where, value in [("dataset/as_of", baseline), *work_dates(data, rereads)]:
        if value is None:
            continue
        day = dt.date.fromisoformat(value)
        if day > today:
            raise ValueError(f"{where}: {value} is after today ({today.isoformat()})")
        dates.append(value)
    return max(dates)


def dated_data(data: dict, as_of: str) -> dict:
    """A view with the effective date; the authored baseline is never changed."""
    return {**data, "dataset": {**data["dataset"], "as_of": as_of}}
