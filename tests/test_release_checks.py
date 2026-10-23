"""Release invariants, including deliberate stale and malformed mutations."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FLAG_SEARCH, write_json
from scripts import agent_files, build, release_checks
from scripts.reader_text import check_reader_text


QUOTATIONS_NOTICE = (
    "Quotations from sources remain with their rights holders and are reproduced "
    "as quotations; the licence covers our own compilation, text and data."
)


@pytest.mark.parametrize("note", [None, "Quoted source material retains its rights."])
def test_missing_licence_notice_reaches_build(dataset, note):
    assert release_checks.check_metadata(dataset) == []
    if note is None:
        del dataset["dataset"]["license_note"]
    else:
        dataset["dataset"]["license_note"] = note
    assert (
        "dataset.license_note: missing quotations licence notice"
        in build.check_consistency(dataset)
    )


@pytest.mark.parametrize("section", ["agents block", "Licences section"])
def test_readme_licence_notice_mutations(dataset, build_env, monkeypatch, capsys, section):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    original = readme.read_text()
    assert release_checks.check_readme_licence(original) == []
    position = (
        original.index(QUOTATIONS_NOTICE)
        if section == "agents block"
        else original.rindex(QUOTATIONS_NOTICE)
    )
    broken = original[:position] + original[position:].replace(QUOTATIONS_NOTICE, "", 1)
    expected = f"README.md {section}: missing quotations licence notice"
    assert release_checks.check_readme_licence(broken) == [expected]
    readme.write_text(broken)
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    errors = capsys.readouterr().err
    # Generated text is checked for staleness before the proposed regeneration.
    assert ("agents block is stale" if section == "agents block" else expected) in errors
    readme.write_text(original)
    build.main()


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (QUOTATIONS_NOTICE, "", "footer: missing quotations licence notice"),
        (
            "Code: MIT; data, page text and documentation: CC BY 4.0",
            "CC BY 4.0",
            "footer: missing code/content licence split",
        ),
    ],
)
def test_footer_licence_mutations(old, new, expected):
    from bs4 import BeautifulSoup
    from scripts import check_page

    html = (
        "<footer><div id='foot'>"
        "Code: MIT; data, page text and documentation: CC BY 4.0"
        f"<p>{QUOTATIONS_NOTICE}</p></div></footer>"
    )
    assert check_page.check_footer_licence(BeautifulSoup(html, "html.parser")) == []
    broken = BeautifulSoup(html.replace(old, new), "html.parser")
    assert check_page.check_footer_licence(broken) == [expected]
    # Having the notice elsewhere on the page does not satisfy the footer check.
    if old == QUOTATIONS_NOTICE:
        broken.append(BeautifulSoup(f"<main>{QUOTATIONS_NOTICE}</main>", "html.parser"))
        assert check_page.check_footer_licence(broken) == [expected]


@pytest.mark.parametrize("change", ["as_of", "changelog", "citation"])
def test_metadata_mutations_reach_build(dataset, change):
    assert release_checks.check_metadata(dataset) == []
    if change == "as_of":
        dataset["dataset"]["as_of"] = "2026-10-18"
    elif change == "changelog":
        dataset["changelog"][-1]["date"] = "2026-10-16"
    else:
        dataset["dataset"]["citation"] = "Synthetic tracker, version 1.0.0"
    assert any(
        "changelog date" in e or "contain the as_of date" in e
        for e in build.check_consistency(dataset)
    )


@pytest.mark.parametrize("date", ["17 October 2026", "17 Oct 2026", "2026-10-17"])
def test_citation_accepts_explicit_date_formats(dataset, date):
    dataset["dataset"]["citation"] = "version 1.0.0, " + date
    assert release_checks.check_metadata(dataset) == []


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("trailing", [b"", b"\n"])
def test_generation_changes_only_marker_interior_and_is_idempotent(newline, trailing):
    prefix = (
        b"# Readme  \t"
        + newline
        + b"\xe2\x98\x83"
        + newline
        + release_checks.CITATION_START
    )
    suffix = release_checks.CITATION_END + newline + b"Keep  exact \t bytes" + trailing
    before = prefix + newline + b"Stale citation" + newline + suffix
    expected = prefix + newline * 2 + b"New citation." + newline * 2 + suffix
    assert release_checks.generated_citation(before, "New citation.") == expected
    assert release_checks.generated_citation(expected, "New citation.") == expected


@pytest.mark.parametrize(
    "readme",
    [
        b"No markers",
        b"<!-- citation:start -->\n",
        b"<!-- citation:end -->\n<!-- citation:start -->",
        b"<!-- citation:start -->\n<!-- citation:start -->\n<!-- citation:end -->",
        b"Prefix <!-- citation:start -->\n<!-- citation:end -->",
    ],
)
def test_bad_markers_fail_without_guessing(readme):
    with pytest.raises(ValueError):
        release_checks.generated_citation(readme, "Citation")


def test_only_update_readme_writes_the_stale_citation(
    dataset, build_env, monkeypatch, capsys
):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    stale = readme.read_bytes().replace(b"Synthetic tracker", b"Stale tracker", 1)  # the citation block only
    readme.write_bytes(stale)
    for argv in (["build.py", "--check"], ["build.py"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            build.main()
        assert "citation block is stale; run scripts/build.py --update-readme" in capsys.readouterr().err
        assert readme.read_bytes() == stale
    assert not build.DIST.exists()
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    expected = release_checks.generated_citation(stale, dataset["dataset"]["citation"])
    assert readme.read_bytes() == expected
    assert not build.DIST.exists()
    build.main()
    assert readme.read_bytes() == expected
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    build.main()


def test_as_of_cannot_be_after_today(dataset, monkeypatch):
    assert release_checks.check_metadata(dataset) == []
    monkeypatch.setattr(release_checks, "today", lambda: build.dt.date(2026, 10, 16))
    assert "dataset.as_of 2026-10-17 is after today (2026-10-16)" in release_checks.check_metadata(dataset)
    monkeypatch.setattr(release_checks, "today", lambda: build.dt.date(2026, 10, 17))
    assert release_checks.check_metadata(dataset) == []


def test_citation_cff_is_generated_and_stale_copies_fail(dataset, build_env, monkeypatch, capsys):
    write_json(build.DATA, dataset)
    cff = build_env / "CITATION.cff"
    expected = cff.read_text()
    assert "type: dataset" in expected and "- name: Test publisher" in expected
    cff.write_text(expected.replace("version: 1.0.0", "version: 0.0.0"))
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert "CITATION.cff is missing or stale; run scripts/build.py --update-readme" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert cff.read_text() == expected
    cff.unlink()
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert cff.read_text() == expected and oct(cff.stat().st_mode & 0o777) == "0o644"


def test_authored_em_dash_rejected_and_recorded_evidence_exempt(dataset):
    dataset["jurisdictions"][0]["summary"] += "\u2014extra"
    assert any("U+2014" in e for e in build.check_consistency(dataset))
    assert (
        check_reader_text(
            {
                "sources": [
                    {
                        "title": "A\u2014B",
                        "url": "https://example.org/A\u2014B",
                        "excerpts": [{"text": "A\u2014B"}],
                    }
                ],
                "institutes": [{"name": "A\u2014B", "people": [{"role": "A\u2014B"}]}],
                "notes": "See https://example.org/A\u2014B for the source.",
            }
        )
        == []
    )


def test_tracked_markdown_mutation_fails_cli(dataset, build_env, monkeypatch, capsys):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    readme.write_text(readme.read_text() + "\nAuthored\u2014prose\n")
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()

    assert "README.md: tracked Markdown contains U+2014" in capsys.readouterr().err


def test_markdown_uses_git_inventory_not_untracked_files(tmp_path, monkeypatch):
    (tmp_path / "tracked.md").write_text("Clean")
    (tmp_path / "untracked.md").write_text("A\u2014B")

    def inventory(command, **kwargs):
        assert command == ["git", "-C", str(tmp_path), "ls-files", "-z", "--", "*.md"]
        return subprocess.CompletedProcess(command, 0, stdout=b"tracked.md\0")

    monkeypatch.setattr(release_checks.subprocess, "run", inventory)
    assert release_checks.check_markdown(tmp_path) == []
    (tmp_path / "tracked.md").write_text("A\u2014B")
    assert release_checks.check_markdown(tmp_path) == [
        "tracked.md: tracked Markdown contains U+2014"
    ]


def test_stale_generated_citation_is_replaced_before_markdown_prose_check(
    dataset, build_env, monkeypatch
):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    before = readme.read_bytes().replace(
        b"Synthetic tracker", "Stale\u2014tracker".encode(), 1
    )
    readme.write_bytes(before)
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert readme.read_bytes() == release_checks.generated_citation(
        before, dataset["dataset"]["citation"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "See dataset.",
        "dataset.17",
        "DATASET.method",
        "subdataset.",
        "https://example.org/dataset.csv",
    ],
)
def test_literal_dataset_root_is_always_rejected_in_page_prose(text):
    assert any("field-path jargon" in e for e in check_reader_text({"notes": text}))


@pytest.mark.parametrize(
    "text", ["117 October 2026", "2026-10-170", "17 Octobers 2026"]
)
def test_citation_date_rejects_false_word_boundaries(dataset, text):
    dataset["dataset"]["citation"] = "version 1.0.0, " + text
    assert any(
        "contain the as_of date" in e for e in release_checks.check_metadata(dataset)
    )


def test_source_title_exclusion_and_authored_gloss_punctuation():
    assert check_reader_text({"sources": [{"title_en": "A\u2014B"}]}) == []
    source = {"excerpts": [{"gloss": "A\u2014B"}]}
    assert any("U+2014" in e for e in check_reader_text({"sources": [source]}))


def test_citation_cannot_generate_extra_markers():
    readme = b"<!-- citation:start -->\nOld\n<!-- citation:end -->\n"
    with pytest.raises(ValueError, match="canonical citation"):
        release_checks.generated_citation(readme, "<!-- citation:start -->")


@pytest.mark.parametrize("field", ["basis", "attribution_note"])
def test_authored_basis_and_attribution_notes_reject_em_dash(field):
    assert any("U+2014" in e for e in check_reader_text({field: "Authored\u2014prose"}))


def test_candidate_build_never_writes_project_citation(
    dataset, build_env, monkeypatch, capsys
):
    other = write_json(build_env / "candidate.json", dataset)
    readme = build_env / "README.md"
    stale = readme.read_bytes().replace(b"Synthetic tracker", b"Stale tracker", 1)  # the citation block only
    readme.write_bytes(stale)
    for extra in ([], ["--update-readme"], ["--check", "--update-readme"]):
        monkeypatch.setattr(sys, "argv", ["build.py", "--data", str(other), *extra])
        with pytest.raises(SystemExit):
            build.main()
        assert "only generated from data/aisi.json" in capsys.readouterr().err
        assert readme.read_bytes() == stale
    assert not build.DIST.exists()


def test_update_readme_writes_nothing_when_another_check_fails(
    dataset, build_env, monkeypatch, capsys
):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    stale = readme.read_bytes().replace(b"Synthetic tracker", b"Stale tracker", 1)  # the citation block only
    readme.write_bytes(stale)
    (build_env / "other.md").write_text("Tracked\u2014prose\n")
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    with pytest.raises(SystemExit):
        build.main()
    assert "other.md: tracked Markdown contains U+2014" in capsys.readouterr().err
    assert readme.read_bytes() == stale


def test_audit_prose_still_gets_dash_check_without_page_jargon_rule():
    fragment = {
        "dataset": {"exchange_rates": [{"basis": "Uses units_per_eur."}]},
        "institutes": [{"outputs": [{"attribution_note": "Uses credited_from."}]}],
    }
    assert check_reader_text(fragment) == []
    fragment["dataset"]["exchange_rates"][0]["basis"] += "\u2014bad"
    assert any("U+2014" in e for e in check_reader_text(fragment))


@pytest.mark.parametrize("where", ["README.md", "CLAUDE.md", "AGENTS.md", "citation", "schema", "method"])
@pytest.mark.parametrize("wording", ["unreleased", "This draft", "THIS\nDRAFT", "[Owner copy edit pending.]"])
def test_launch_wording_mutations(dataset, build_env, where, wording):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    schema = {
        "properties": {
            "status": {
                "enum": ["draft_policy", "unreleased"],
                "description": "A status.",
            }
        }
    }
    assert release_checks.check_launch_wording(dataset, schema, build_env) == []
    if where in ("README.md", "CLAUDE.md", "AGENTS.md"):
        path = build_env / where
        path.write_text(path.read_text() + "\n" + wording)
    elif where == "citation":
        dataset["dataset"]["citation"] += ", " + wording
    elif where == "method":
        dataset["dataset"]["method"].append({"id": "placeholder", "text": "Reporting line: a placeholder rule. " + wording})
    else:
        schema["properties"]["status"]["description"] += " " + wording
    assert any(
        "draft wording" in e
        for e in release_checks.check_launch_wording(dataset, schema, build_env)
    )


@pytest.mark.parametrize(
    "wording",
    [
        "Version 4.0.1",
        "schema 4.0",
        "docs/design/schema-4.0.md",
        "Download contract (4.0.0)",
        "4.0 activity contract",
        "api/v4/institutes/example.json",
    ],
)
@pytest.mark.parametrize("where", ["README.md", "docs/design/schema-1.0.md", "schema", "changelog", "notes"])
def test_release_rejects_internal_versions(dataset, build_env, where, wording):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    schema = {}
    assert release_checks.check_launch_wording(dataset, schema, build_env) == []
    if where.endswith(".md"):
        path = build_env / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wording)
    elif where == "schema":
        schema["description"] = wording
    elif where == "changelog":
        dataset["changelog"][0]["summary"] = wording
    else:
        dataset["jurisdictions"][0]["notes"] = wording
    assert any(
        "remove internal pre-release version before release" in error
        for error in release_checks.check_launch_wording(dataset, schema, build_env)
    )


def test_release_preserves_licences_fonts_evidence_and_private_history(dataset, build_env):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    history = build_env / "docs/history"
    history.mkdir(parents=True)
    (history / "changelog-prerelease.md").write_text("Version 4.0.1, unreleased draft.")
    dataset["sources"][0]["title"] = "Version 4.0 report"
    schema = {"description": "CC BY 4.0; Creative Commons Attribution 4.0; font version 4.001."}
    assert release_checks.check_launch_wording(dataset, schema, build_env) == []


def test_release_cli_rejects_draft_and_accepts_clean_fixture(
    dataset, build_env, monkeypatch, capsys
):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    # Use the actual schema structure with only its release prose cleaned.
    schema = json.loads(build.SCHEMA.read_text())
    for path, text in list(release_checks.schema_descriptions(schema)):
        node = schema
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = release_checks.DRAFT_WORDING.sub("release", text)
    build_schema = write_json(build_env / "schema/aisi.schema.json", schema)
    monkeypatch.setattr(build, "SCHEMA", build_schema)
    monkeypatch.setattr(agent_files, "PUBLIC_URL", "https://public-url-not-decided.invalid/")
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py", "--release", "--check"])
    with pytest.raises(SystemExit):
        build.main()  # A placeholder address must still block release.
    assert "PUBLIC_URL in scripts/agent_files.py is still the placeholder" in capsys.readouterr().err
    monkeypatch.setattr(agent_files, "PUBLIC_URL", "https://example.org/aisi/")
    write_json(build.DATA, dataset)  # CITATION.cff carries the address
    build.main()
    (build_env / "CLAUDE.md").write_text("This draft is being reviewed.")
    with pytest.raises(SystemExit):
        build.main()
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    build.main()  # Draft builds remain supported.
    # Work dated after as_of: an ordinary check warns, --release fails.
    (build_env / "CLAUDE.md").write_text("Project rules.")
    dataset["institutes"][0]["model_access"]["searches"] = [dict(FLAG_SEARCH, date="2026-10-18")]
    write_json(build.DATA, dataset)
    late = "gb-test: model_access search 2026-10-18 is after dataset.as_of 2026-10-17"
    capsys.readouterr()
    build.main()
    assert f"warning: {late}" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["build.py", "--release", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert late in capsys.readouterr().err


def test_dates_after_as_of_cover_flag_searches_access_and_instrument_status(dataset):
    # as_of is 2026-10-17; the same day is allowed, the next day is flagged.
    assert release_checks.dates_after_as_of(dataset) == []
    institute, source = dataset["institutes"][0], dataset["sources"][0]
    for field in release_checks.FLAG_FIELDS:
        institute[field]["searches"] = [dict(FLAG_SEARCH, date="2026-10-17"), dict(FLAG_SEARCH, date="2026-10-18")]
    source["accessed"] = "2026-10-18"
    # Snapshot timestamps compare by their UTC date.
    for retrieved in ("2026-10-17T23:59:59Z", "2026-10-18T00:30:00+02:00"):
        source["snapshot"] = {"retrieved": retrieved, "sha256": "0" * 64}
        assert release_checks.dates_after_as_of(dataset)[3:] == ["source gb-source: accessed 2026-10-18 is after dataset.as_of 2026-10-17"]
    for retrieved in ("2026-10-17T23:30:00-02:00", "2026-10-18t00:00:00z", "2026-10-18 garbled"):  # 18 October in UTC; lowercase; malformed
        source["snapshot"]["retrieved"] = retrieved
        assert release_checks.dates_after_as_of(dataset)[4] == f"source gb-source: snapshot retrieved {retrieved} is after dataset.as_of 2026-10-17"
    source["snapshot"]["retrieved"] = "2026-10-18T00:00:00Z"
    source["excerpts"].append({"text": "Later quotation", "supports": [], "accessed": "2026-10-19"})
    legal_source(dataset, instrument_status_checked="2026-10-18")
    assert release_checks.dates_after_as_of(dataset) == [
        "gb-test: model_access search 2026-10-18 is after dataset.as_of 2026-10-17",
        "gb-test: testing_capacity search 2026-10-18 is after dataset.as_of 2026-10-17",
        "gb-test: staffing_model search 2026-10-18 is after dataset.as_of 2026-10-17",
        "source gb-source: accessed 2026-10-18 is after dataset.as_of 2026-10-17",
        "source gb-source: snapshot retrieved 2026-10-18T00:00:00Z is after dataset.as_of 2026-10-17",
        "source gb-source: excerpt 1 accessed 2026-10-19 is after dataset.as_of 2026-10-17",
        "source gb-source: instrument_status_checked 2026-10-18 is after dataset.as_of 2026-10-17",
    ]
    dataset["dataset"]["as_of"] = "2026-10-19"  # release day moves as_of forward
    assert release_checks.dates_after_as_of(dataset) == []


def test_instrument_status_age_uses_the_release_date_under_release(dataset, monkeypatch):
    # as_of 2026-10-17 is 183 days after 2026-04-17; release day 2026-10-18 is 184.
    legal_source(dataset, instrument_status_checked="2026-04-17")
    monkeypatch.setattr(release_checks, "today", lambda: build.dt.date(2026, 10, 18))
    assert release_checks.instrument_status_warnings(dataset) == []
    assert release_checks.instrument_status_warnings(dataset, release=True) == [
        "source gb-source: status last verified 2026-04-17, 184 days before the release date 2026-10-18 (limit 183)"
    ]


@pytest.mark.parametrize("text", ["version 1.0.01", "version 1.0.0.1", "version 1.0.0-rc1", "version 1.0.0+build1", "version 11.0.0", "1.0.0"])
def test_citation_version_must_be_the_whole_token(dataset, text):
    dataset["dataset"]["citation"] = "Synthetic tracker, " + text + ", 17 October 2026"
    assert any("current version" in e for e in release_checks.check_metadata(dataset))


@pytest.mark.parametrize("text", ["A\n\n# Injected", "A\r\nB", "A <!-- citation:end", "A <b>bold</b>"])
def test_citation_is_one_line_of_plain_text(dataset, text):
    dataset["dataset"]["citation"] = text + ", version 1.0.0, 17 October 2026"
    schema = json.loads(build.SCHEMA.read_text(encoding="utf-8"))
    errors = list(build.Draft202012Validator(schema).iter_errors(dataset))
    assert any(list(e.absolute_path) == ["dataset", "citation"] for e in errors)


def test_citation_rejects_a_trailing_newline(dataset):
    dataset["dataset"]["citation"] += "\n"
    schema = json.loads(build.SCHEMA.read_text(encoding="utf-8"))
    errors = list(build.Draft202012Validator(schema).iter_errors(dataset))
    assert any(list(e.absolute_path) == ["dataset", "citation"] for e in errors)


def test_generated_counts_have_handworked_values_and_keep_history():
    data = {'dataset': {'version': '1.0.0', 'as_of': '2026-10-18', 'network_milestones': [{}, {}]}, 'jurisdictions': [{'europe_scope': True, 'status': 'none_identified', 'status_confidence': 'medium'}, {'europe_scope': False, 'status': 'established', 'status_confidence': 'high'}], 'institutes': [{'outputs': [{}, {}, {}], 'agreements': [{}]}], 'sources': [{}, {}, {}, {}]}
    before = b'History: 7 outputs.\n<!-- counts:start -->\nold\n<!-- counts:end -->\nTail\n'
    result = release_checks.generated_counts(before, data)
    assert b'2 jurisdictions, 1 institutes and 4 sources.' in result
    assert b'3 output records, 1 agreements and 2 network milestones.' in result
    assert b'0 high, 1 medium, 0 low.' in result
    assert result.startswith(b'History: 7 outputs.\n') and result.endswith(b'Tail\n')
    assert release_checks.generated_counts(result, data) == result
    with pytest.raises(ValueError, match='standalone counts markers'):
        release_checks.generated_counts(b'No block', data)


def test_only_update_readme_writes_stale_counts(dataset, build_env, monkeypatch, capsys):
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    current = readme.read_bytes()
    stale = current.replace(b"1 jurisdictions", b"99 jurisdictions")
    assert stale != current
    readme.write_bytes(stale)
    for argv in (["build.py", "--check"], ["build.py"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            build.main()
        assert "counts block is stale; run scripts/build.py --update-readme" in capsys.readouterr().err
        assert readme.read_bytes() == stale
    assert not build.DIST.exists()
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert readme.read_bytes() == current
    assert not build.DIST.exists()
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    build.main()


def test_generated_counts_preserves_unrecorded_optional_confidence():
    data = {
        "dataset": {"version": "1.0.0", "as_of": "2026-10-18"},
        "jurisdictions": [{"europe_scope": False, "status": "none_identified"}],
        "institutes": [],
        "sources": [],
    }
    result = release_checks.generated_counts(
        b"<!-- counts:start -->\n<!-- counts:end -->\n", data
    )
    assert b"0 high, 0 medium, 0 low, 1 not recorded." in result


def test_candidate_check_does_not_compare_or_write_canonical_readme(
    dataset, build_env, monkeypatch, capsys
):
    # An alternative dataset may change coverage and its citation independently.
    write_json(build.DATA, dataset)
    readme = build_env / "README.md"
    original = readme.read_bytes()
    source = dict(dataset["sources"][0], id="candidate-source", url="https://example.org/candidate")
    source.pop("excerpts", None)
    dataset["sources"].append(source)
    dataset["institutes"][0]["source_ids"].append("candidate-source")
    dataset["dataset"]["citation"] = "Candidate tracker, version 1.0.0, 17 October 2026"
    other = write_json(build_env / "candidate.json", dataset)
    monkeypatch.setattr(sys, "argv", ["build.py", "--data", str(other), "--check"])
    build.main()
    assert "2 sources" in capsys.readouterr().out
    assert readme.read_bytes() == original
    assert not build.DIST.exists()


@pytest.mark.parametrize("field", ["summary", "changes"])
@pytest.mark.parametrize(
    "wording",
    [
        "unreleased draft",
        "This draft",
        "Round B",
        "round B",
        "Round 12: checks",
        "Night 2 corrections",
        "Page round",
        "Plain-bug round",
        "owner decision",
        "independent schema review (Codex) and factual red-team (Fable)",
        "reviewed by a different model family",
        "Fable review",
    ],
)
def test_release_changelog_wording_mutations(dataset, build_env, field, wording):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    clean = "Government drafted a policy; consultation process recorded."
    dataset["changelog"] = [{"version": "1.0.0", "summary": clean, "changes": [clean]}]
    assert release_checks.check_launch_wording(dataset, {}, build_env) == []
    entry = dataset["changelog"][0]  # This fixture has exactly one release.
    entry[field] = [wording] if field == "changes" else wording
    errors = release_checks.check_launch_wording(dataset, {}, build_env)
    category = "draft" if wording in ("unreleased draft", "This draft") else "process"
    assert any(
        f"changelog/1.0.0/{field}" in error
        and f"remove {category} wording before release" in error
        for error in errors
    )


@pytest.mark.parametrize("wording", [
    "A funding round a year later increased the allocation.",
    "Round Robin evaluation results added.",
    "Evaluations cover a new model family and two existing model families.",
])
def test_changelog_allows_substantive_round_and_model_family_text(dataset, build_env, wording):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Agent map.")
    dataset["changelog"] = [{"version": "1.0.0", "summary": wording, "changes": [wording]}]
    assert release_checks.check_launch_wording(dataset, {}, build_env) == []


@pytest.mark.parametrize(
    "wording",
    [
        "Saved review-search-17.",
        "See audit-europe-12.",
        "Saved job_42.",
        "Saved night7/example-task.",
        "See exampleSearch retrievals.",
        "See sampleBlogs retrievals.",
        "See sampleBlog retrievals.",
        "A record was red-teamed by another model.",
        "Checked by an independent subagent.",
        "Reviewed by Claude.",
        "Checked by another model.",
        "Verified by an LLM.",
        "Reviewed by GPT.",
        "Reviewed by Gemini.",
        "Reviewed by AI.",
        "Reviewed by a different model.",
        "A record was checked by a different model per region.",
        "Reviewed by Claude Opus 4.",
        "Checked by GPT-5.",
        "Verified by Gemini 2.5 Pro.",
        "Red-teamed by a different model in each region.",
        "Red-teamed by a different model for each region.",
        "Checked by Codex (GPT-5).",
    ],
)
@pytest.mark.parametrize(
    "where", ["access_note", "check_note", "method", "changelog", "schema"]
)
def test_internal_process_mutation_in_public_prose(dataset, build_env, wording, where):
    (build_env / "CLAUDE.md").write_text("Project rules.")
    (build_env / "AGENTS.md").write_text("Review code with an independent agent.")
    assert release_checks.check_launch_wording(dataset, {}, build_env) == []
    schema = {}
    if where == "access_note":
        dataset["sources"][0]["access_note"] = wording
    elif where == "check_note":
        dataset["jurisdictions"][0]["check"]["language_note"] = wording
    elif where == "method":
        dataset["dataset"]["method"][0]["text"] = wording
    elif where == "changelog":
        dataset["changelog"][0]["changes"].append(wording)
    else:
        schema = {"description": wording}
    assert any(
        "remove internal process wording before release" in e
        for e in release_checks.check_launch_wording(dataset, schema, build_env)
    )


def test_internal_process_preserves_evidence_and_ai_subject_matter(dataset, build_env):
    (build_env / "CLAUDE.md").write_text("Code reviewed by Claude.")
    (build_env / "AGENTS.md").write_text("Review with an independent agent.")
    # Real mandate and output descriptions, not merely synthetic near misses.
    dataset["institutes"][0]["notes"] = (
        "Evaluates, tests and red-teams AI systems; supports standards and regulation. "
        "Institute-published sandboxing tools specifically for running agent evaluations. "
        "Reviewed by AI Office staff. Checked by AI Safety Institute researchers. "
        "Verified by AI Act market-surveillance authorities. "
        "Red-teamed by AI Security Institute evaluators. "
        "Results verified by model providers. "
        "Reviewed by Claude Martin, head of unit."
    )
    source = dataset["sources"][0]
    source["title"] = (
        "Pre-Deployment Evaluation of Anthropic's Upgraded Claude 3.5 Sonnet"
    )
    source["title_en"] = "Reviewed by Claude"
    source["excerpts"][0]["text"] = "Saved review-search-17"
    source["url"] = "https://example.org/review-search-17"
    source["access_note"] = "A copy is held at https://example.org/review-search-17"
    dataset["jurisdictions"][0]["check"]["searches"] = [{"query": "review-search-17"}]
    # Published data adds per-field citations. These are stable source IDs even
    # when the cited field is prose and the ID resembles a research job label.
    dataset["jurisdictions"][0]["citations"] = {
        "notes": ["review-2026", "audit-bill-2025", "agent-2026"]
    }
    assert release_checks.check_launch_wording(dataset, {}, build_env) == []


def legal_source(dataset, **fields):
    """The fixture's one cited source, marked as a legal instrument with hand-set status fields."""
    source = dataset["sources"][0]
    source.update(legal_text=True, **fields)
    return source


def test_instrument_status_warnings_at_a_fixed_as_of(dataset):
    # as_of is 2026-10-17; 183 days before it is 2026-04-17.
    assert dataset["dataset"]["as_of"] == "2026-10-17"
    assert release_checks.instrument_status_warnings(dataset) == []  # no legal_text source
    legal_source(dataset)
    assert release_checks.instrument_status_warnings(dataset) == [
        "source gb-source: status never verified (no instrument_status_checked)"
    ]
    act = dataset["sources"][-1]
    act["instrument_status_checked"] = "2026-04-17"  # exactly 183 days: still current
    assert release_checks.instrument_status_warnings(dataset) == []
    act["instrument_status_checked"] = "2026-04-16"  # 184 days: stale
    assert release_checks.instrument_status_warnings(dataset) == [
        "source gb-source: status last verified 2026-04-16, 184 days before dataset.as_of 2026-10-17 (limit 183)"
    ]
    del act["legal_text"]  # only legal instruments are tracked
    assert release_checks.instrument_status_warnings(dataset) == []


def test_instrument_status_date_needs_legal_text_and_cannot_be_in_the_future(dataset, monkeypatch):
    schema = build.authored_schema(json.loads(build.SCHEMA.read_text()), strict=False)
    validator = build.Draft202012Validator(schema, format_checker=build.Draft202012Validator.FORMAT_CHECKER)
    act = legal_source(dataset, instrument_status_checked="2026-10-17")
    assert list(validator.iter_errors(dataset)) == []
    monkeypatch.setattr(release_checks, "today", lambda: build.dt.date(2026, 10, 16))
    assert "source gb-source: instrument_status_checked 2026-10-17 is after today (2026-10-16)" in build.check_consistency(dataset)
    monkeypatch.setattr(release_checks, "today", lambda: build.dt.date(2026, 10, 17))
    assert build.check_consistency(dataset) == []
    act["instrument_status_checked"] = "2026-02-30"
    assert "/sources/0/instrument_status_checked: '2026-02-30' is not a real date" in build.check_consistency(dataset)
    act["instrument_status_checked"] = "2026-10-17"
    del act["legal_text"]
    assert any("'legal_text' is a dependency of 'instrument_status_checked'" in e.message for e in validator.iter_errors(dataset))


def test_instrument_status_is_exported_and_warnings_reach_the_report_without_failing(dataset, monkeypatch, tmp_path_factory, capsys):
    legal_source(dataset, instrument_status_checked="2026-01-02")
    rows = {r["id"]: r for r in build.tables(build.export_dataset(dataset))["sources"]}
    assert rows["gb-source"]["instrument_status_checked"] == "2026-01-02"
    write_json(build.DATA, dataset)
    report = tmp_path_factory.mktemp("report")
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--report-dir", str(report)])
    build.main()  # a warning never fails validation
    captured = capsys.readouterr()
    assert "is valid" in captured.out
    expected = "source gb-source: status last verified 2026-01-02, 288 days before dataset.as_of 2026-10-17 (limit 183)"
    assert f"warning: {expected}" in captured.err
    assert (report / build.RELEASE_WARNINGS).read_text() == f"## Release warnings\n\n- {expected}\n"


def test_citation_ends_with_the_doi_link(dataset):
    doi = "10.5281/zenodo.1234567"
    dataset["dataset"]["doi"] = doi
    assert any("must end with the DOI link" in e for e in release_checks.check_metadata(dataset))
    dataset["dataset"]["citation"] += " https://doi.org/" + doi + "."
    assert any("must end with the DOI link" in e for e in release_checks.check_metadata(dataset))
    dataset["dataset"]["citation"] = dataset["dataset"]["citation"][:-1]
    assert release_checks.check_metadata(dataset) == []
    del dataset["dataset"]["doi"]  # no DOI, no link required
    assert release_checks.check_metadata(dataset) == []


def test_readme_introduction_links_the_doi():
    doi = "10.5281/zenodo.1234567"
    link = f"[{doi}](https://doi.org/{doi})"
    assert release_checks.check_readme_doi(f"# T\n\nIntro. DOI {link}.\n\n## Setup\n", doi) == []
    assert release_checks.check_readme_doi("# T\n\nIntro.\n\n## Cite\n\n" + link + "\n", doi) == [
        f"README.md introduction: missing the DOI link {link}"
    ]
    assert release_checks.check_readme_doi("# T\n", None) == []


def test_repository_readme_and_data_carry_the_doi():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "data/aisi.json").read_text(encoding="utf-8"))
    doi = data["dataset"]["doi"]
    assert release_checks.check_metadata(data) == []
    assert release_checks.check_readme_doi((root / "README.md").read_text(encoding="utf-8"), doi) == []


def test_zenodo_json_is_generated_and_stale_copies_fail(dataset, build_env, monkeypatch, capsys):
    write_json(build.DATA, dataset)
    zenodo = build_env / ".zenodo.json"
    expected = zenodo.read_text()
    assert json.loads(expected)["version"] == dataset["dataset"]["version"]
    zenodo.write_text(expected.replace('"license": "CC-BY-4.0"', '"license": ["CC-BY-4.0"]'))
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert ".zenodo.json is missing or stale; run scripts/build.py --update-readme" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert zenodo.read_text() == expected
    zenodo.unlink()
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert ".zenodo.json is missing or stale" in capsys.readouterr().err
