"""Check, and with --apply set, the Zenodo metadata of a released version that .zenodo.json cannot carry.

Zenodo's GitHub integration archives each release from .zenodo.json, but its loader treats every
creator as a person, so the record lacks Accelerate Europe as organisational creator and its
citation reads "Hoelscher-Obermaier, J. (2026)" instead of the canonical "Accelerate Europe (2026)".
It also takes one licence only. This script compares the live record of one version with
agent_files.zenodo_rdm_metadata (the same dataset fields as .zenodo.json) and, with --apply, edits
the record to match. It manages creators, publisher, resource type, version, rights, subjects
(keywords) and related identifiers; title, description, notes, dates and files stay as they are.
For an earlier version it uses the current maintainers and publisher with that version's number.

    uv run scripts/zenodo_metadata.py                  # diff for dataset.version
    uv run scripts/zenodo_metadata.py --version 1.0.0  # diff for another release
    uv run scripts/zenodo_metadata.py --apply          # needs ZENODO_TOKEN; edit, publish, verify

Diff mode exits 0 when the record matches, 1 when it differs, 2 on errors or refusals. --apply
refuses when the record's version is not the requested one, when the record already has an edit
draft with other changes, or when Zenodo reports validation errors on the draft; it then re-reads
the published record and exits 0 only when nothing differs. Zenodo's public API can serve the old record
for about a minute after publishing: while the re-read record's `updated` is older than the publish,
--apply reads it again for up to 2 minutes; a later diff showing old values right after "published"
is that lag, so wait a minute before diffing again.

API, Zenodo's InvenioRDM REST API (checked on 24 October 2026 against the public JSON of record
23614941 with Accept: application/vnd.inveniordm.v1+json):
- https://inveniordm.docs.cern.ch/reference/rest_api_drafts_records/ : GET /api/records/{id}/versions/latest,
  GET /api/records/{id}/versions (anonymous page size at most 25), POST /api/records/{id}/draft
  (edit a published record), PUT /api/records/{id}/draft (the whole draft: access, files,
  metadata, custom_fields, pids), POST /api/records/{id}/draft/actions/publish.
- https://inveniordm.docs.cern.ch/reference/metadata/ : creators (person_or_org type "personal" or
  "organizational", identifiers scheme "orcid", affiliations by name), publisher, rights,
  subjects, related_identifiers.
- https://developers.zenodo.org/#authentication : personal access token as "Authorization: Bearer";
  scopes deposit:write (edit) and deposit:actions (publish, edit, discard).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    from .agent_files import zenodo_rdm_metadata
except ImportError:
    from agent_files import zenodo_rdm_metadata

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "aisi.json"
API = "https://zenodo.org/api"
UA = "aisi-tracker/1.0 zenodo_metadata (contact@accelerate-europe.org)"
MANAGED = ("resource_type", "creators", "publisher", "version", "rights", "subjects", "related_identifiers")
DRAFT_KEYS = ("access", "files", "metadata", "custom_fields", "pids")

# request(method, path, body) -> parsed JSON; path is relative to API or an absolute API URL.
Request = Callable[..., dict]


class Refusal(Exception):
    """A condition under which the script stops without changing anything further."""


def http_client(token: str | None) -> Request:
    def request(method: str, path: str, body: dict | None = None) -> dict:
        url = path if path.startswith("https://") else API + path
        headers = {"Accept": "application/vnd.inveniordm.v1+json", "User-Agent": UA}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if token:  # never print the token, even if an error response echoes it; redact before cutting
                detail = detail.replace(token, "[token]")
            detail = detail[:2000]
            raise Refusal(f"{method} {url}: HTTP {exc.code}: {detail}") from None
        return json.loads(text) if text else {}
    return request


def concept_id(doi: str) -> str:
    prefix = "10.5281/zenodo."
    if not doi.startswith(prefix):
        raise Refusal(f"dataset.doi {doi!r} is not a Zenodo DOI")
    return doi[len(prefix):]


def find_version(request: Request, concept: str, version: str) -> dict:
    """The published record of `version` under the concept record, from the version listing."""
    latest = request("GET", f"/records/{concept}/versions/latest")
    path, matches = f"/records/{latest['id']}/versions?size=25&page=1", []
    while path:
        page = request("GET", path)
        matches += [hit for hit in page["hits"]["hits"] if hit["metadata"].get("version") == version]
        path = page.get("links", {}).get("next")
    if len(matches) != 1:
        raise Refusal(f"{len(matches)} Zenodo versions of concept {concept} carry version {version!r}; expected one")
    record = matches[0]
    if record.get("parent", {}).get("id") != concept:
        raise Refusal(f"record {record['id']} belongs to concept {record.get('parent', {}).get('id')}, not {concept}")
    return record


def _vocab(value: dict) -> dict:
    """A vocabulary entry by id; an entry without an id (free text) is kept whole."""
    return {"id": value["id"]} if "id" in value else value


def _person(value: dict) -> dict:
    # Zenodo generates "name" for a person from given and family name; compare the parts.
    keys = ("type", "given_name", "family_name", "identifiers") if value.get("type") == "personal" else ("type", "name", "identifiers")
    return {k: value[k] for k in keys if value.get(k)}


def managed(metadata: dict) -> dict:
    """The managed fields of record metadata in the shape zenodo_rdm_metadata produces. Related
    identifiers are compared as a set (sorted); creators, rights and subjects keep their order."""
    out = {
        "resource_type": _vocab(metadata["resource_type"]) if metadata.get("resource_type") else None,
        "creators": [{"person_or_org": _person(c["person_or_org"])}
                     | ({"affiliations": [{k: a[k] for k in ("id", "name") if k in a} for a in c["affiliations"]]} if c.get("affiliations") else {})
                     for c in metadata.get("creators", [])],
        "publisher": metadata.get("publisher"),
        "version": metadata.get("version"),
        "rights": [_vocab(r) for r in metadata.get("rights", [])],
        "subjects": [{"id": s["id"]} if "id" in s else {"subject": s["subject"]} for s in metadata.get("subjects", [])],
        "related_identifiers": sorted(
            ({"identifier": r["identifier"], "scheme": r.get("scheme")}
             | {"relation_type": _vocab(r["relation_type"])}
             | ({"resource_type": _vocab(r["resource_type"])} if r.get("resource_type") else {})
             for r in metadata.get("related_identifiers", [])),
            key=lambda r: json.dumps(r, sort_keys=True)),
    }
    return out


def differences(desired: dict, metadata: dict) -> list[str]:
    """One line per managed field whose live value differs from the desired one."""
    live, want = managed(metadata), managed(desired)
    dump = lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True)  # noqa: E731
    return [f"{field}:\n  live:    {dump(live[field])}\n  desired: {dump(want[field])}" for field in MANAGED if live[field] != want[field]]


def _state(record: dict) -> dict:
    """What publishing an edit draft would carry over: metadata, access, custom fields, the DOI and
    the file options with the files by key and checksum (count and size follow from those)."""
    files = record.get("files") or {}
    options = {k: v for k, v in files.items() if k not in ("entries", "count", "total_bytes")}
    return {
        "metadata": record.get("metadata"),
        "access": record.get("access"),
        "custom_fields": record.get("custom_fields"),
        "doi": (record.get("pids") or {}).get("doi", {}).get("identifier"),
        "files": options | {"entries": {k: e.get("checksum") for k, e in (files.get("entries") or {}).items()}},
    }


def draft_payload(draft: dict, desired: dict) -> dict:
    """The PUT body: the draft as Zenodo returned it, with the managed metadata fields replaced."""
    body = {key: draft[key] for key in DRAFT_KEYS if key in draft}
    body["metadata"] = {**draft["metadata"], **{field: desired[field] for field in MANAGED}}
    return body


# Zenodo's public API can serve the record as it was before the edit for about a minute after publishing.
LAG_SECONDS, LAG_POLL = 120, 10


def _when(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def apply(request: Request, record: dict, desired: dict, log: Callable[[str], None] = print,
          sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Edit, update and publish the record; returns the differences left on the re-read record. While the
    re-read record is older than the publish (its `updated` before the published one's), it is read again
    every LAG_POLL seconds for up to LAG_SECONDS: an old copy then is lag, not a failed edit."""
    rid, version = record["id"], desired["version"]
    if record["metadata"].get("version") != version:
        raise Refusal(f"record {rid} has version {record['metadata'].get('version')!r}, not {version!r}")
    draft = request("POST", f"/records/{rid}/draft")
    if draft.get("id") != rid or draft["metadata"].get("version") != version:
        raise Refusal(f"edit draft of {rid} is {draft.get('id')} with version {draft['metadata'].get('version')!r}; nothing changed")
    have, want = _state(draft), _state(record)
    pending = sorted(f"metadata.{k}" for k in set(have["metadata"]) | set(want["metadata"]) if have["metadata"].get(k) != want["metadata"].get(k))
    pending += [k for k in ("access", "custom_fields", "doi", "files") if have[k] != want[k]]
    if pending:
        raise Refusal(f"record {rid} already has an edit draft changing {', '.join(pending)}; publish or discard it on zenodo.org first")
    saved = request("PUT", f"/records/{rid}/draft", draft_payload(draft, desired))
    if saved.get("errors"):
        raise Refusal(f"Zenodo rejected the draft of {rid}, not published (the edit draft stays open on zenodo.org): {json.dumps(saved['errors'], ensure_ascii=False)}")
    log(f"updated the edit draft of {rid}; publishing")
    published = _when(request("POST", f"/records/{rid}/draft/actions/publish").get("updated"))
    waited = 0
    while True:
        live = request("GET", f"/records/{rid}")
        left = differences(desired, live["metadata"])
        served = _when(live.get("updated"))
        stale = published is not None and served is not None and served < published
        if not left or not stale or waited >= LAG_SECONDS:
            return left
        log(f"Zenodo still serves the record as updated {live.get('updated')}, before the publish at {published.isoformat()}; reading again in {LAG_POLL} s")
        sleep(LAG_POLL)
        waited += LAG_POLL


def main(argv: list[str] | None = None, request: Request | None = None, data_path: Path = DATA) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", help="release version, e.g. 1.0.1 (default: dataset.version)")
    ap.add_argument("--apply", action="store_true", help="edit and publish the record (needs ZENODO_TOKEN); never touches files")
    args = ap.parse_args(argv)
    dataset = json.loads(data_path.read_text(encoding="utf-8"))["dataset"]
    version = (args.version or dataset["version"]).removeprefix("v")
    token = os.environ.get("ZENODO_TOKEN") or None
    if args.apply and not token and request is None:
        print("--apply needs ZENODO_TOKEN in the environment (a Zenodo personal access token)", file=sys.stderr)
        return 2
    request = request or http_client(token)
    desired = zenodo_rdm_metadata(dataset, version)
    try:
        record = find_version(request, concept_id(dataset["doi"]), version)
        print(f"Zenodo version {version}: record {record['id']} (https://zenodo.org/records/{record['id']})")
        diffs = differences(desired, record["metadata"])
        if not diffs:
            print("metadata matches; nothing to do")
            return 0
        print("\n".join(diffs))
        if not args.apply:
            return 1
        left = apply(request, record, desired)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if left:
        print("after publishing, the record still differs:\n" + "\n".join(left))
        return 1
    print(f"published; record {record['id']} now matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
