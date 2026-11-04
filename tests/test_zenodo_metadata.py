"""scripts/zenodo_metadata.py against Zenodo responses recorded on 24 October 2026 (no network)."""

import copy
import io
import json
from pathlib import Path

import pytest

from scripts import agent_files, zenodo_metadata as zm

FIXTURES = Path(__file__).parent / "fixtures" / "zenodo"
ROOT = Path(__file__).resolve().parents[1]
DATASET = {
    "version": "1.0.1",
    "publisher": "Accelerate Europe",
    "maintainers": [{"name": "Jason Hoelscher-Obermaier", "affiliation": "Accelerate Europe; Transformative KI Österreich", "orcid": "0000-0003-4773-9059"}],
    "license": "CC BY 4.0",
    "doi": "10.5281/zenodo.23600431",
    "repository": "https://github.com/accelerate-europe/aisi-tracker",
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeZenodo:
    """Serves the recorded listing and records; edit calls follow the InvenioRDM draft flow."""

    def __init__(self, records: dict | None = None, draft_changes: dict | None = None, put_errors: list | None = None):
        self.records = records or {r: fixture(f"record-{r}") for r in ("23614941", "23600432")}
        self.draft_changes, self.put_errors, self.calls, self.put_body = draft_changes or {}, put_errors, [], None

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path))
        rid = path.split("/")[2] if path.startswith("/records/") else None
        if (method, path) == ("GET", "/records/23600431/versions/latest"):
            return copy.deepcopy(self.records["23614941"])
        if method == "GET" and path.startswith("/records/23614941/versions?"):
            listing = fixture("versions")
            listing["hits"]["hits"] = [copy.deepcopy(self.records[h["id"]]) for h in listing["hits"]["hits"]]
            return listing
        if (method, path) == ("POST", f"/records/{rid}/draft"):
            return copy.deepcopy(self.records[rid]) | copy.deepcopy(self.draft_changes)
        if (method, path) == ("PUT", f"/records/{rid}/draft"):
            self.put_body = body
            return {"id": rid, "metadata": body["metadata"]} | ({"errors": self.put_errors} if self.put_errors else {})
        if (method, path) == ("POST", f"/records/{rid}/draft/actions/publish"):
            self.records[rid]["metadata"] = self.put_body["metadata"]
            return self.records[rid]
        if (method, path) == ("GET", f"/records/{rid}"):
            return copy.deepcopy(self.records[rid])
        raise AssertionError(f"unexpected call {method} {path}")


def test_desired_metadata_shape():
    desired = agent_files.zenodo_rdm_metadata(DATASET, "1.0.0")
    assert desired["creators"] == [
        {"person_or_org": {"type": "organizational", "name": "Accelerate Europe"}},
        {"person_or_org": {"type": "personal", "family_name": "Hoelscher-Obermaier", "given_name": "Jason",
                           "identifiers": [{"scheme": "orcid", "identifier": "0000-0003-4773-9059"}]},
         "affiliations": [{"name": "Accelerate Europe; Transformative KI Österreich"}]},
    ]
    assert desired["resource_type"] == {"id": "dataset"} and desired["publisher"] == "Accelerate Europe"
    assert desired["version"] == "1.0.0"
    assert desired["rights"] == [{"id": "cc-by-4.0"}, {"id": "mit"}]
    assert [s["subject"] for s in desired["subjects"]] == agent_files.ZENODO_KEYWORDS
    assert {r["identifier"] for r in desired["related_identifiers"]} == {
        "https://aisi.accelerate-europe.org/", "https://github.com/accelerate-europe/aisi-tracker/tree/v1.0.0"}


def test_related_identifiers_match_zenodo_json():
    dataset = json.loads((ROOT / "data" / "aisi.json").read_text(encoding="utf-8"))["dataset"]
    legacy = json.loads(agent_files.zenodo_json({"dataset": dataset}))["related_identifiers"]
    rdm = agent_files.zenodo_rdm_metadata(dataset, dataset["version"])["related_identifiers"]
    assert [(r["relation_type"]["id"], r["identifier"], r["resource_type"]["id"]) for r in rdm] == [
        (r["relation"].lower(), r["identifier"], r["resource_type"]) for r in legacy]


def test_diff_on_recorded_1_0_1():
    diffs = zm.differences(agent_files.zenodo_rdm_metadata(DATASET, "1.0.1"), fixture("record-23614941")["metadata"])
    assert [d.split(":")[0] for d in diffs] == ["creators", "rights"]


def test_diff_on_recorded_1_0_0():
    diffs = zm.differences(agent_files.zenodo_rdm_metadata(DATASET, "1.0.0"), fixture("record-23600432")["metadata"])
    assert [d.split(":")[0] for d in diffs] == ["creators", "publisher", "subjects"]


def test_matching_record_has_no_diff_despite_generated_fields_and_order():
    desired = agent_files.zenodo_rdm_metadata(DATASET, "1.0.1")
    metadata = copy.deepcopy(fixture("record-23614941")["metadata"]) | copy.deepcopy(desired)
    # As Zenodo returns it: generated person name, vocabulary titles, related identifiers reordered.
    metadata["creators"][1]["person_or_org"]["name"] = "Hoelscher-Obermaier, Jason"
    metadata["rights"][1] |= {"title": {"en": "MIT License"}, "props": {"scheme": "spdx"}}
    metadata["related_identifiers"].reverse()
    assert zm.differences(desired, metadata) == []


def test_find_version_and_refusals():
    fake = FakeZenodo()
    assert zm.find_version(fake, "23600431", "1.0.0")["id"] == "23600432"
    with pytest.raises(zm.Refusal, match="0 Zenodo versions"):
        zm.find_version(fake, "23600431", "9.9.9")
    fake.records["23600432"]["parent"]["id"] = "999"
    with pytest.raises(zm.Refusal, match="belongs to concept 999"):
        zm.find_version(fake, "23600431", "1.0.0")


def test_apply_runs_the_edit_flow_and_keeps_other_fields():
    fake = FakeZenodo()
    desired = agent_files.zenodo_rdm_metadata(DATASET, "1.0.1")
    before = fixture("record-23614941")
    assert zm.apply(fake, before, desired, log=lambda _: None) == []
    assert fake.calls == [("POST", "/records/23614941/draft"), ("PUT", "/records/23614941/draft"),
                          ("POST", "/records/23614941/draft/actions/publish"), ("GET", "/records/23614941")]
    body = fake.put_body
    assert set(body) <= set(zm.DRAFT_KEYS) and "metadata" in body and "access" in body
    for field in zm.MANAGED:
        assert body["metadata"][field] == desired[field]
    for field in ("title", "description", "additional_descriptions", "publication_date"):
        assert body["metadata"][field] == before["metadata"][field]


def test_apply_refuses_a_version_mismatch():
    fake = FakeZenodo()
    with pytest.raises(zm.Refusal, match="not '1.0.1'"):
        zm.apply(fake, fixture("record-23600432"), agent_files.zenodo_rdm_metadata(DATASET, "1.0.1"))
    assert fake.calls == []


@pytest.mark.parametrize("change, named", [
    ({"metadata": fixture("record-23614941")["metadata"] | {"title": "Edited by hand"}}, "metadata.title"),
    ({"access": {"record": "restricted", "files": "restricted"}}, "access"),
    ({"custom_fields": {"code:codeRepository": "https://example.org/other"}}, "custom_fields"),
    ({"files": fixture("record-23614941")["files"] | {"enabled": False}}, "files"),
    ({"files": fixture("record-23614941")["files"] | {"default_preview": "other.zip"}}, "files"),
    ({"pids": {"doi": {"identifier": "10.5281/zenodo.1"}}}, "doi"),
])
def test_apply_refuses_an_open_draft_with_other_changes(change, named):
    fake = FakeZenodo(draft_changes=change)
    with pytest.raises(zm.Refusal, match=f"already has an edit draft changing {named}"):
        zm.apply(fake, fixture("record-23614941"), agent_files.zenodo_rdm_metadata(DATASET, "1.0.1"))
    assert [c[0] for c in fake.calls] == ["POST"]


def test_apply_does_not_publish_a_draft_with_errors():
    fake = FakeZenodo(put_errors=[{"field": "metadata.rights.1.id", "messages": ["unknown"]}])
    with pytest.raises(zm.Refusal, match="not published"):
        zm.apply(fake, fixture("record-23614941"), agent_files.zenodo_rdm_metadata(DATASET, "1.0.1"))
    assert ("POST", "/records/23614941/draft/actions/publish") not in fake.calls


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    data = tmp_path / "aisi.json"
    data.write_text(json.dumps({"dataset": DATASET}), encoding="utf-8")
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    assert zm.main(["--apply"], data_path=data) == 2
    assert zm.main([], request=FakeZenodo(), data_path=data) == 1
    assert "record 23614941" in capsys.readouterr().out
    fake = FakeZenodo()
    fake.records["23600432"]["metadata"] |= agent_files.zenodo_rdm_metadata(DATASET, "1.0.0")
    assert zm.main(["--version", "v1.0.0"], request=fake, data_path=data) == 0
    assert zm.main(["--version", "2.0.0"], request=FakeZenodo(), data_path=data) == 2
    assert zm.main(["--apply"], request=FakeZenodo(), data_path=data) == 0


def test_http_errors_never_show_the_token(monkeypatch):
    token = "FAKE-TOKEN-123"

    def fail(*args, **kwargs):
        # The token straddles the 2,000-character cut.
        raise zm.urllib.error.HTTPError("https://zenodo.org/api/records/1", 401, "unauthorised", {}, io.BytesIO(("x" * 1990 + token).encode()))

    monkeypatch.setattr(zm.urllib.request, "urlopen", fail)
    with pytest.raises(zm.Refusal) as exc:
        zm.http_client(token)("GET", "/records/1")
    assert "FAKE-TOK" not in str(exc.value) and "HTTP 401" in str(exc.value)


def test_apply_rereads_while_zenodo_serves_the_record_from_before_the_publish():
    fake = FakeZenodo()
    desired = agent_files.zenodo_rdm_metadata(DATASET, "1.0.1")
    before = fixture("record-23614941")
    old = copy.deepcopy(before) | {"updated": "2026-10-24T10:00:00+00:00"}
    reads, naps = [], []
    publish = fake.__call__

    def lagging(method, path, body=None):
        if method == "POST" and path.endswith("/actions/publish"):
            return publish(method, path, body) | {"updated": "2026-10-24T10:05:00+00:00"}
        if (method, path) == ("GET", "/records/23614941"):
            reads.append(path)
            fresh = publish(method, path, body) | {"updated": "2026-10-24T10:05:00+00:00"}
            return old if len(reads) < 3 else fresh
        return publish(method, path, body)

    assert zm.apply(lagging, before, desired, log=lambda _: None, sleep=naps.append) == []
    assert len(reads) == 3 and naps == [zm.LAG_POLL, zm.LAG_POLL]
    # A record that stays old past the limit is reported, not waited on for ever.
    reads.clear()
    naps.clear()
    publish = FakeZenodo().__call__
    stuck = lambda m, p, b=None: old if (m, p) == ("GET", "/records/23614941") else lagging(m, p, b)  # noqa: E731
    assert zm.apply(stuck, before, desired, log=lambda _: None, sleep=naps.append)
    assert sum(naps) == zm.LAG_SECONDS
