"""Tests for tasks.py — the docs/TASKS.md v2 read/search/write helper.

All fixtures are synthetic TASKS.md / DONE.md files built in tmp_path. The real
docs/TASKS.md is never read or touched.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "ctask.py"
_spec = importlib.util.spec_from_file_location("tasks_script", _SCRIPT)
tasks = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tasks  # dataclasses needs the module registered before exec
_spec.loader.exec_module(tasks)


FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## TOC
- [features](#features)
- [interfaces](#interfaces)

## How to use this file
Some instructions here. Don't reuse retired IDs.

## features

### FEAT-15 — Improve sentiment scoring

- prio:: high
- module:: features
- created:: 2026-01-01
- refs:: src/features/sentiment.py

**Goal.** Make sentiment scoring better.

**Details.**
- do a thing
- do another thing

**Done when.** scoring is better.

### FEAT-16 — Add ETF exclusion

- prio:: medium
- module:: features
- created:: 2026-01-02
- blocked-by:: FEAT-15
- refs:: src/features/sentiment.py, docs/BUSINESS_LOGIC-features-sentiment.md

**Goal.** Exclude ETF tickers from sentiment counts.

**Done when.** ETF tickers no longer counted.

## interfaces

### CLI-05 — Fix status output

- prio:: low
- module:: interfaces
- created:: 2026-01-03
- blocked-by:: FEAT-16

**Goal.** Status output should be clean.

**Done when.** status output is clean.
"""


# Fixture with a malformed task-id header, used to prove the parser refuses
# to parse a malformed task header rather than silently accepting it.
MALFORMED_ID_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## analysis

### ANL-16-STEP2 — Session model follow-ups: fold non-trading Reddit tail; Kronos session-staleness

- prio:: medium
- module:: analysis
- created:: 2026-01-01

**Goal.** Follow up on session modelling gaps.

**Done when.** non-trading tail and staleness are handled.
"""


# A pre-existing DONE.md, mirroring TASKS.md's grammar, with one already-closed
# task (FEAT-24) under "## features" — used to test id-reuse prevention and
# section-append behavior.
DONE_FIXTURE = """---
type: requirement
state: done
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Done
Ledger of completed tasks, moved out of docs/TASKS.md on close. Never reuse a listed id.

## features

### FEAT-24 — old landed work

- prio:: low
- module:: features
- created:: 2026-01-01
- closed:: 2026-07-01
- commits:: abc1234 wip(feat-24): landed work

**Goal.** Old landed work.

**Done when.** it landed.
"""


@pytest.fixture()
def tasks_file(tmp_path: Path) -> Path:
    p = tmp_path / "TASKS.md"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


@pytest.fixture()
def malformed_id_file(tmp_path: Path) -> Path:
    p = tmp_path / "TASKS-malformed.md"
    p.write_text(MALFORMED_ID_FIXTURE, encoding="utf-8")
    return p


@pytest.fixture()
def done_file(tmp_path: Path) -> Path:
    return tmp_path / "DONE.md"


def run(tasks_file: Path, done_file: Path, *argv: str) -> int:
    return tasks.main(["--file", str(tasks_file), "--done-file", str(done_file), *argv])


def run_captured(capsys, tasks_file: Path, done_file: Path, *argv: str) -> tuple[int, str, str]:
    rc = run(tasks_file, done_file, *argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# --------------------------------------------------------------------------
# parsing / round-trip
# --------------------------------------------------------------------------
def test_parse_multitask_fixture(tasks_file: Path) -> None:
    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    ids = [t.id for t in parsed.all_tasks()]
    assert ids == ["FEAT-15", "FEAT-16", "CLI-05"]
    feat15 = parsed.tasks_by_id()["FEAT-15"]
    assert feat15.prio == "high"
    assert feat15.module == "features"
    assert feat15.goal == "Make sentiment scoring better."
    assert feat15.details == "- do a thing\n- do another thing"
    assert feat15.done_when == "scoring is better."
    assert feat15.blocked_by is None
    feat16 = parsed.tasks_by_id()["FEAT-16"]
    assert feat16.blocked_by == "FEAT-15"
    assert feat16.refs == "src/features/sentiment.py, docs/BUSINESS_LOGIC-features-sentiment.md"


def test_roundtrip_byte_identical(tasks_file: Path) -> None:
    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    assert parsed.render() == text


def test_atomicity_parse_error_leaves_file_unchanged(tmp_path: Path, done_file: Path) -> None:
    bad = tmp_path / "TASKS.md"
    bad_text = (
        "---\ntype: requirement\nstate: wip\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "cross-file-references: [docs/TASKS.md]\n---\n# Tasks\n\n## features\n\n"
        "### NOT-A-VALID-HEADER\n\n- prio:: high\n"
    )
    bad.write_text(bad_text, encoding="utf-8")
    rc = run(bad, done_file, "close", "FEAT-15")
    assert rc != 0
    assert bad.read_text(encoding="utf-8") == bad_text


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def test_list_all(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list", "--ids-only")
    assert rc == 0
    assert out.splitlines() == ["FEAT-15", "FEAT-16", "CLI-05"]


def test_list_filter_prio(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list", "--prio", "high,medium", "--ids-only")
    assert rc == 0
    assert out.splitlines() == ["FEAT-15", "FEAT-16"]


def test_list_filter_module(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list", "--module", "interfaces", "--ids-only")
    assert rc == 0
    assert out.splitlines() == ["CLI-05"]


def test_list_blocked_vs_unblocked(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list", "--blocked", "--ids-only")
    assert out.splitlines() == ["FEAT-16", "CLI-05"]
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list", "--unblocked", "--ids-only")
    assert out.splitlines() == ["FEAT-15"]


def test_list_aligned_columns(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "list")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].startswith("FEAT-15")
    assert "high" in lines[0]
    assert "features" in lines[0]
    assert "Improve sentiment scoring" in lines[0]


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------
def test_show_returns_verbatim_body(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "show", "FEAT-15")
    assert rc == 0
    assert out.strip("\n") == (
        "### FEAT-15 — Improve sentiment scoring\n\n"
        "- prio:: high\n"
        "- module:: features\n"
        "- created:: 2026-01-01\n"
        "- refs:: src/features/sentiment.py\n\n"
        "**Goal.** Make sentiment scoring better.\n\n"
        "**Details.**\n"
        "- do a thing\n"
        "- do another thing\n\n"
        "**Done when.** scoring is better."
    )


def test_show_multiple_ids(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "show", "FEAT-15", "FEAT-16")
    assert rc == 0
    assert "FEAT-15 — Improve sentiment scoring" in out
    assert "FEAT-16 — Add ETF exclusion" in out


def test_show_unknown_id(tasks_file, done_file, capsys):
    rc, out, err = run_captured(capsys, tasks_file, done_file, "show", "NOPE-99")
    assert rc == 1
    assert "NOPE-99" in err
    assert "not in TASKS.md or DONE.md" in err


def test_show_finds_closed_task_in_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "show", "FEAT-24")
    assert rc == 0
    assert "[CLOSED — from DONE.md]" in out
    assert "FEAT-24 — old landed work" in out
    assert "closed:: 2026-07-01" in out
    assert "commits:: abc1234 wip(feat-24): landed work" in out


def test_show_open_task_unaffected_by_done_md_presence(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "show", "FEAT-15")
    assert rc == 0
    assert "[CLOSED" not in out
    assert "FEAT-15 — Improve sentiment scoring" in out


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
def test_search_hits_title(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "ETF", "--field", "title", "--ids-only")
    assert rc == 0
    assert out.splitlines() == ["FEAT-16"]


def test_search_hits_goal(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "sentiment scoring better", "--field", "goal", "--ids-only")
    assert out.splitlines() == ["FEAT-15"]


def test_search_hits_details(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "another thing", "--field", "details", "--ids-only")
    assert out.splitlines() == ["FEAT-15"]


def test_search_hits_done(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "ETF tickers no longer", "--field", "done", "--ids-only")
    assert out.splitlines() == ["FEAT-16"]


def test_search_case_insensitive(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "etf tickers no longer", "--field", "done", "--ids-only")
    assert out.splitlines() == ["FEAT-16"]


def test_search_all_field_default_includes_snippet(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "search", "ETF")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "FEAT-16  Add ETF exclusion"
    assert lines[1].strip().startswith("title:")


def test_search_include_done_finds_archived_hit_and_labels_it(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "search", "old landed work", "--field", "title", "--include-done",
    )
    assert rc == 0
    assert "FEAT-24" in out
    assert "[CLOSED]" in out


def test_search_without_include_done_ignores_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "search", "old landed work", "--field", "title",
    )
    assert rc == 0
    assert out == ""
    assert "FEAT-24" not in out


# --------------------------------------------------------------------------
# blockers / blocking
# --------------------------------------------------------------------------
def test_blockers_resolves_forward_edge(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "blockers", "CLI-05")
    assert rc == 0
    assert out.splitlines() == ["FEAT-16  Add ETF exclusion"]


def test_blockers_none_for_unblocked_task(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "blockers", "FEAT-15")
    assert rc == 0
    assert out == ""


def test_blocking_resolves_reverse_edge(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "blocking", "FEAT-15")
    assert rc == 0
    assert out.splitlines() == ["FEAT-16  Add ETF exclusion"]


def test_blocking_chain(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "blocking", "FEAT-16")
    assert rc == 0
    assert out.splitlines() == ["CLI-05  Fix status output"]


# --------------------------------------------------------------------------
# refs
# --------------------------------------------------------------------------
def test_refs_for_id(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "refs", "FEAT-16")
    assert rc == 0
    assert out.splitlines() == [
        "src/features/sentiment.py",
        "docs/BUSINESS_LOGIC-features-sentiment.md",
    ]


def test_refs_referencing_substring(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "refs", "--referencing", "sentiment.py")
    assert rc == 0
    assert out.splitlines() == [
        "FEAT-15  Improve sentiment scoring",
        "FEAT-16  Add ETF exclusion",
    ]


def test_refs_referencing_no_match(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "refs", "--referencing", "nonexistent/path.py")
    assert rc == 0
    assert out == ""


# --------------------------------------------------------------------------
# add — explicit id / prefix
# --------------------------------------------------------------------------
def test_add_explicit_id_inserts_under_correct_section(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-99", "--title", "New thing", "--prio", "low", "--module", "features",
        "--goal", "Do a new thing.", "--done-when", "the new thing is done.",
    )
    assert rc == 0
    assert out.strip() == "FEAT-99"

    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    features_section = next(s for s in parsed.sections if s.name == "features")
    assert [t.id for t in features_section.tasks] == ["FEAT-15", "FEAT-16", "FEAT-99"]
    new_task = parsed.tasks_by_id()["FEAT-99"]
    assert new_task.title == "New thing"
    assert new_task.prio == "low"
    assert new_task.goal == "Do a new thing."
    assert new_task.done_when == "the new thing is done."
    # neighbours untouched
    assert parsed.tasks_by_id()["FEAT-15"].raw == tasks.parse_tasks_file(FIXTURE).tasks_by_id()["FEAT-15"].raw


def test_add_auto_assign_picks_next_free(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id-prefix", "FEAT", "--title", "Auto id thing", "--prio", "medium", "--module", "features",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    # highest existing FEAT id in the fixture is FEAT-16
    assert out.strip() == "FEAT-17"


def test_add_refuses_duplicate_live_id(tasks_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-15", "--title", "Dup", "--prio", "low", "--module", "features",
    )
    assert rc != 0
    assert "FEAT-15" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_add_rejects_both_id_and_prefix(tasks_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-77", "--id-prefix", "FEAT", "--title", "x", "--prio", "low", "--module", "features",
    )
    assert rc == 2


# --------------------------------------------------------------------------
# add — CHANGE 3: auto-ID from TASKS.md ∪ DONE.md union, never reuse a closed id
# --------------------------------------------------------------------------
def test_add_refuses_id_already_closed_in_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-24", "--title", "Should be refused", "--prio", "low", "--module", "features",
    )
    assert rc != 0
    assert "FEAT-24" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_add_auto_assign_skips_ids_closed_in_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")  # highest closed FEAT id is FEAT-24
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id-prefix", "FEAT", "--title", "Auto id thing", "--prio", "medium", "--module", "features",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    # union of TASKS.md (max FEAT-16) and DONE.md (max FEAT-24) -> next is FEAT-25
    assert out.strip() == "FEAT-25"


# --------------------------------------------------------------------------
# add — CHANGE 3: prefix inference from --module
# --------------------------------------------------------------------------
def test_infer_prefix_from_module_top_level_mappings():
    assert tasks._infer_prefix_from_module("fetcher") == "FET"
    assert tasks._infer_prefix_from_module("features") == "FEAT"
    assert tasks._infer_prefix_from_module("backtest") == "BACK"
    assert tasks._infer_prefix_from_module("analysis") == "ANL"
    assert tasks._infer_prefix_from_module("storage") == "STOR"
    assert tasks._infer_prefix_from_module("meta") == "META"


def test_infer_prefix_from_module_longest_match_wins_for_interfaces_submodules():
    assert tasks._infer_prefix_from_module("interfaces.cli") == "CLI"
    assert tasks._infer_prefix_from_module("interfaces.ui") == "UI"
    assert tasks._infer_prefix_from_module("interfaces.scheduler") == "SCH"
    assert tasks._infer_prefix_from_module("interfaces.notifications") == "NOTI"
    assert tasks._infer_prefix_from_module("interfaces.mcp") == "MCP"


def test_infer_prefix_from_module_falls_back_to_top_level_segment():
    # "analysis.signals" has no submodule-specific mapping; falls back to "analysis" -> ANL
    assert tasks._infer_prefix_from_module("analysis.signals") == "ANL"
    assert tasks._infer_prefix_from_module("features.sentiment") == "FEAT"


def test_infer_prefix_from_module_unknown_raises_with_known_mappings_listed():
    with pytest.raises(ValueError) as exc:
        tasks._infer_prefix_from_module("nonexistent")
    msg = str(exc.value)
    assert "nonexistent" in msg
    assert "FEAT" in msg  # known mappings are listed


def test_add_infers_prefix_from_module_end_to_end(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--title", "New thing", "--prio", "low", "--module", "features",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "FEAT-17"


def test_add_unknown_module_prefix_inference_fails_clearly(tasks_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--title", "x", "--prio", "low", "--module", "unknownmod",
    )
    assert rc != 0
    assert "unknownmod" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


@pytest.mark.parametrize("bad_id", ["ANL-16-STEP2", "DB-TS-MIGRATION", "toolong-1", "A-1"])
def test_add_rejects_malformed_id_shape(tasks_file, done_file, capsys, bad_id):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", bad_id, "--title", "x", "--prio", "low", "--module", "features",
    )
    assert rc != 0
    assert bad_id in err
    # refuses to write
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_add_explicit_id_accepts_conforming_shape(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-42", "--title", "Follow-up cleanup", "--prio", "low", "--module", "features",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "FEAT-42"
    parsed = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8"))
    assert "FEAT-42" in parsed.tasks_by_id()


# --------------------------------------------------------------------------
# add — section placement: a dotted --module must land under its real "## "
# section header, not fail on a literal (nonexistent) "## <module>" match.
# One canonical table backs both id-prefix inference and section placement.
# --------------------------------------------------------------------------
SECTIONS_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## fetcher

### FET-01 — placeholder fetcher task

- prio:: low
- module:: fetcher
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.

## analysis

### ANL-01 — placeholder analysis task

- prio:: low
- module:: analysis
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.

## interfaces — cli

### CLI-01 — placeholder cli task

- prio:: low
- module:: interfaces.cli
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.

## interfaces — ui

### UI-01 — placeholder ui task

- prio:: low
- module:: interfaces.ui
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.
"""


@pytest.fixture()
def sections_file(tmp_path: Path) -> Path:
    p = tmp_path / "TASKS-sections.md"
    p.write_text(SECTIONS_FIXTURE, encoding="utf-8")
    return p


def test_infer_section_from_module_matches_real_headers():
    assert tasks._infer_section_from_module("interfaces.cli") == "interfaces — cli"
    assert tasks._infer_section_from_module("interfaces.ui") == "interfaces — ui"
    assert tasks._infer_section_from_module("interfaces.scheduler") == "interfaces — scheduler"
    assert tasks._infer_section_from_module("interfaces.notifications") == "interfaces — notifications"
    assert tasks._infer_section_from_module("interfaces.mcp") == "interfaces — mcp"
    assert tasks._infer_section_from_module("fetcher") == "fetcher"
    assert tasks._infer_section_from_module("meta") == "meta — cross-cutting"


def test_infer_section_from_module_falls_back_to_top_level_segment():
    assert tasks._infer_section_from_module("analysis.signals") == "analysis"
    assert tasks._infer_section_from_module("fetcher.reddit") == "fetcher"


# --------------------------------------------------------------------------
# per-project module config: docs/.ctask (JSON) extends/overrides the
# builtin _MODULE_PREFIX_MAP for one repo, without polluting it globally.
# --------------------------------------------------------------------------
def _write_project_config(repo_root: Path, modules: dict) -> Path:
    docs_dir = repo_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    config_path = docs_dir / ".ctask"
    config_path.write_text(json.dumps({"modules": modules}), encoding="utf-8")
    return config_path


PROJECT_SECTIONS_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## CR wave — 2026-07-01 code review findings

### CR-01 — placeholder cr task

- prio:: low
- module:: cr
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.

## Follow-ups — deferred decisions

### FU-01 — placeholder fu task

- prio:: low
- module:: fu
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.
"""


def test_infer_prefix_from_module_repo_root_none_matches_default_behavior():
    # explicit repo_root=None must behave identically to omitting it entirely
    assert tasks._infer_prefix_from_module("features", None) == tasks._infer_prefix_from_module("features")
    assert tasks._infer_section_from_module("features", None) == tasks._infer_section_from_module("features")


def test_infer_prefix_and_section_from_project_config(tmp_path: Path):
    _write_project_config(tmp_path, {
        "cr": {"prefix": "CR", "section": "CR wave — 2026-07-01 code review findings"},
        "fu": {"prefix": "FU", "section": "Follow-ups — deferred decisions"},
    })
    assert tasks._infer_prefix_from_module("cr", repo_root=tmp_path) == "CR"
    assert tasks._infer_section_from_module("cr", repo_root=tmp_path) == "CR wave — 2026-07-01 code review findings"


def test_project_config_overrides_builtin_on_key_collision(tmp_path: Path):
    _write_project_config(tmp_path, {
        "features": {"prefix": "ZZZZ", "section": "overridden section"},
    })
    assert tasks._infer_prefix_from_module("features", repo_root=tmp_path) == "ZZZZ"
    assert tasks._infer_section_from_module("features", repo_root=tmp_path) == "overridden section"


def test_missing_project_config_file_falls_back_to_builtin_only(tmp_path: Path):
    (tmp_path / "docs").mkdir()  # docs/ exists, but no .ctask file in it
    assert tasks._infer_prefix_from_module("features", repo_root=tmp_path) == "FEAT"


def test_project_config_missing_docs_dir_falls_back_to_builtin_only(tmp_path: Path):
    assert tasks._infer_prefix_from_module("features", repo_root=tmp_path) == "FEAT"


def test_malformed_project_config_invalid_json_raises_value_error(tmp_path: Path):
    config_path = tmp_path / "docs" / ".ctask"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        tasks._infer_prefix_from_module("cr", repo_root=tmp_path)
    assert str(config_path) in str(exc.value)


def test_malformed_project_config_bad_prefix_shape_raises_value_error(tmp_path: Path):
    config_path = _write_project_config(tmp_path, {"cr": {"prefix": "lowercase", "section": "x"}})
    with pytest.raises(ValueError) as exc:
        tasks._infer_prefix_from_module("cr", repo_root=tmp_path)
    assert str(config_path) in str(exc.value)


def test_add_end_to_end_with_project_config(tmp_path: Path, done_file, capsys):
    tasks_path = tmp_path / "docs" / "TASKS.md"
    tasks_path.parent.mkdir(parents=True)
    tasks_path.write_text(PROJECT_SECTIONS_FIXTURE, encoding="utf-8")
    _write_project_config(tmp_path, {
        "cr": {"prefix": "CR", "section": "CR wave — 2026-07-01 code review findings"},
        "fu": {"prefix": "FU", "section": "Follow-ups — deferred decisions"},
    })
    rc, out, _ = run_captured(
        capsys, tasks_path, done_file, "add",
        "--module", "cr", "--title", "New CR task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    new_id = out.strip()
    assert new_id.startswith("CR-")
    parsed = tasks.parse_tasks_file(tasks_path.read_text(encoding="utf-8"))
    section = next(s for s in parsed.sections if s.name == "CR wave — 2026-07-01 code review findings")
    assert new_id in [t.id for t in section.tasks]


def test_edit_module_moves_task_under_project_config_section(tmp_path: Path, done_file, capsys):
    tasks_path = tmp_path / "docs" / "TASKS.md"
    tasks_path.parent.mkdir(parents=True)
    tasks_path.write_text(PROJECT_SECTIONS_FIXTURE, encoding="utf-8")
    _write_project_config(tmp_path, {
        "cr": {"prefix": "CR", "section": "CR wave — 2026-07-01 code review findings"},
        "fu": {"prefix": "FU", "section": "Follow-ups — deferred decisions"},
    })
    rc, _, _ = run_captured(capsys, tasks_path, done_file, "edit", "FU-01", "--module", "cr")
    assert rc == 0
    parsed = tasks.parse_tasks_file(tasks_path.read_text(encoding="utf-8"))
    cr_section = next(s for s in parsed.sections if s.name == "CR wave — 2026-07-01 code review findings")
    fu_section = next(s for s in parsed.sections if s.name == "Follow-ups — deferred decisions")
    assert "FU-01" in [t.id for t in cr_section.tasks]
    assert "FU-01" not in [t.id for t in fu_section.tasks]


def test_add_module_interfaces_cli_lands_under_correct_section_with_cli_id(sections_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, sections_file, done_file, "add",
        "--module", "interfaces.cli", "--title", "New CLI task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    new_id = out.strip()
    assert new_id.startswith("CLI-")
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    section = next(s for s in parsed.sections if s.name == "interfaces — cli")
    assert new_id in [t.id for t in section.tasks]


def test_add_module_analysis_signals_infers_id_and_falls_back_section(sections_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, sections_file, done_file, "add",
        "--module", "analysis.signals", "--title", "New analysis task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    new_id = out.strip()
    assert new_id.startswith("ANL-")
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    section = next(s for s in parsed.sections if s.name == "analysis")
    assert new_id in [t.id for t in section.tasks]


def test_add_module_fetcher_reddit_infers_id_and_falls_back_section(sections_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, sections_file, done_file, "add",
        "--module", "fetcher.reddit", "--title", "New fetcher task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    new_id = out.strip()
    assert new_id.startswith("FET-")
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    section = next(s for s in parsed.sections if s.name == "fetcher")
    assert new_id in [t.id for t in section.tasks]


def test_add_section_override_places_elsewhere_than_inferred_default(sections_file, done_file, capsys):
    """e.g. FET-36 (fetcher-flavored) legitimately lives under '## interfaces —
    ui' in the real registry — --section must express that, winning over the
    module-inferred default."""
    rc, out, _ = run_captured(
        capsys, sections_file, done_file, "add",
        "--module", "fetcher", "--section", "interfaces — ui",
        "--title", "Fetcher-ish task filed under UI", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    new_id = out.strip()
    assert new_id.startswith("FET-")  # id-prefix still comes from --module
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    ui_section = next(s for s in parsed.sections if s.name == "interfaces — ui")
    assert new_id in [t.id for t in ui_section.tasks]
    fetcher_section = next(s for s in parsed.sections if s.name == "fetcher")
    assert new_id not in [t.id for t in fetcher_section.tasks]


def test_add_unknown_section_override_fails_listing_available_sections(sections_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, sections_file, done_file, "add",
        "--module", "fetcher", "--section", "nonexistent-section",
        "--title", "x", "--prio", "low",
    )
    assert rc != 0
    assert "nonexistent-section" in err
    assert "fetcher" in err  # available sections listed
    assert sections_file.read_text(encoding="utf-8") == SECTIONS_FIXTURE


def test_add_explicit_id_with_unmapped_module_fails_without_section_override(sections_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, sections_file, done_file, "add",
        "--id", "FEAT-77", "--module", "unmappedthing",
        "--title", "x", "--prio", "low",
    )
    assert rc != 0
    assert "unmappedthing" in err
    assert sections_file.read_text(encoding="utf-8") == SECTIONS_FIXTURE


def test_add_explicit_id_with_unmapped_module_succeeds_with_section_override(sections_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, sections_file, done_file, "add",
        "--id", "FEAT-77", "--module", "unmappedthing", "--section", "fetcher",
        "--title", "x", "--prio", "low", "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "FEAT-77"
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    fetcher_section = next(s for s in parsed.sections if s.name == "fetcher")
    assert "FEAT-77" in [t.id for t in fetcher_section.tasks]


# --------------------------------------------------------------------------
# add — retired-id protection, third source: git commit history.
#
# DONE.md has never existed (nothing has ever been closed through the
# script), so the old live-ids ∪ done-ids union silently re-issues any id
# retired before the script existed. git history is the durable record.
# --------------------------------------------------------------------------
def _git_repo_with_commits(tmp_path: Path, tasks_text: str, commit_subjects: list[str]) -> tuple[Path, Path]:
    """Build a throwaway git repo with docs/TASKS.md == tasks_text and one
    commit per subject in commit_subjects (oldest first). Returns
    (docs_dir, proj_root)."""
    proj = tmp_path / "proj"
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "TASKS.md").write_text(tasks_text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=proj, check=True)
    for i, subject in enumerate(commit_subjects):
        (proj / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "add", f"f{i}.txt"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-q", "-m", subject], cwd=proj, check=True)
    return docs_dir, proj


STOR_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## storage

### STOR-14 — placeholder storage task

- prio:: low
- module:: storage
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.
"""

BACK_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## backtest

### BACK-12 — placeholder backtest task

- prio:: low
- module:: backtest
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.
"""

SCH_FIXTURE = """---
type: requirement
state: wip
created: 2026-01-01
updated: 2026-01-01
cross-file-references: [docs/TASKS.md]
---
# Tasks

## interfaces — scheduler

### SCH-12 — placeholder scheduler task

- prio:: low
- module:: interfaces.scheduler
- created:: 2026-01-01

**Goal.** placeholder.

**Done when.** placeholder.
"""


def test_extract_id_from_scope_parses_leading_id():
    assert tasks._extract_id_from_scope("fet-38") == "FET-38"
    assert tasks._extract_id_from_scope("stor-15-ui-replica") == "STOR-15"
    assert tasks._extract_id_from_scope("back-13-sweep-harness") == "BACK-13"
    assert tasks._extract_id_from_scope("int-sch-13-queue-stats") == "SCH-13"
    assert tasks._extract_id_from_scope("int-sch-13") == "SCH-13"


def test_extract_id_from_scope_returns_none_for_slug_only():
    assert tasks._extract_id_from_scope("backtest-rebuild") is None
    assert tasks._extract_id_from_scope("cleanup") is None


def test_add_skips_id_found_only_in_git_history_stor15(tmp_path: Path, capsys) -> None:
    """The STOR-15 incident: live max is STOR-14, but git history holds a
    STOR-15 commit (the retired UI-replica task) — auto-assign must skip it
    and land on STOR-16, not reissue STOR-15."""
    docs_dir, _proj = _git_repo_with_commits(
        tmp_path, STOR_FIXTURE,
        ["wip(stor-15-ui-replica): step 5 — docs + raw_data_api replica swap"],
    )
    rc, out, _err = run_captured(
        capsys, docs_dir / "TASKS.md", docs_dir / "DONE.md", "add",
        "--module", "storage", "--title", "New storage task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "STOR-16"


def test_add_skips_id_found_only_in_git_history_back13(tmp_path: Path, capsys) -> None:
    """live max BACK-12, git history holds back-13-sweep-harness -> next must
    be BACK-14, not the colliding BACK-13."""
    docs_dir, _proj = _git_repo_with_commits(
        tmp_path, BACK_FIXTURE, ["feat(back-13-sweep-harness): x"],
    )
    rc, out, _err = run_captured(
        capsys, docs_dir / "TASKS.md", docs_dir / "DONE.md", "add",
        "--module", "backtest", "--title", "New backtest task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "BACK-14"


def test_add_normalizes_historical_interfaces_prefix_from_git_history(tmp_path: Path, capsys) -> None:
    """git history spells the old int-sch-13 scope; it must still block SCH-13
    today (normalized onto the renamed prefix), landing on SCH-14."""
    docs_dir, _proj = _git_repo_with_commits(
        tmp_path, SCH_FIXTURE, ["wip(int-sch-13): step 4 — delete schedule group"],
    )
    rc, out, _err = run_captured(
        capsys, docs_dir / "TASKS.md", docs_dir / "DONE.md", "add",
        "--module", "interfaces.scheduler", "--title", "New sched task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "SCH-14"


def test_add_explicit_id_refused_when_present_only_in_git_history(tmp_path: Path, capsys) -> None:
    docs_dir, _proj = _git_repo_with_commits(
        tmp_path, STOR_FIXTURE, ["wip(stor-15-ui-replica): x"],
    )
    rc, out, err = run_captured(
        capsys, docs_dir / "TASKS.md", docs_dir / "DONE.md", "add",
        "--id", "STOR-15", "--module", "storage", "--title", "x", "--prio", "low",
    )
    assert rc != 0
    assert "STOR-15" in err
    assert (docs_dir / "TASKS.md").read_text(encoding="utf-8") == STOR_FIXTURE


def test_add_ignores_slug_only_scope_in_git_history(tmp_path: Path, capsys) -> None:
    """A non-id scope (chore(backtest-rebuild)) must not crash the scan, and
    must not mask a genuine id-bearing commit alongside it."""
    docs_dir, _proj = _git_repo_with_commits(
        tmp_path, STOR_FIXTURE, ["chore(backtest-rebuild): x", "wip(stor-15-ui-replica): x"],
    )
    rc, out, _err = run_captured(
        capsys, docs_dir / "TASKS.md", docs_dir / "DONE.md", "add",
        "--module", "storage", "--title", "New storage task", "--prio", "low",
        "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "STOR-16"


def test_add_degrades_with_warning_when_git_unavailable(tasks_file, done_file, capsys) -> None:
    """tmp_path fixtures aren't git repos: the reuse check must degrade to
    sources 1+2 and print a clear warning, never crash or fail silently."""
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--module", "features", "--title", "x", "--prio", "low", "--goal", "g", "--done-when", "d",
    )
    assert rc == 0
    assert "git" in err.lower()
    assert "history" in err.lower()


# --------------------------------------------------------------------------
# close — CHANGE 2: moves to docs/DONE.md
# --------------------------------------------------------------------------
def test_close_removes_only_target_task(tasks_file, done_file, capsys):
    before = tasks.parse_tasks_file(FIXTURE)
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "close", "FEAT-15")
    assert rc == 0
    assert out.splitlines()[0] == "FEAT-15"

    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    ids = [t.id for t in parsed.all_tasks()]
    assert ids == ["FEAT-16", "CLI-05"]

    # neighbours byte-identical
    assert parsed.tasks_by_id()["FEAT-16"].raw == before.tasks_by_id()["FEAT-16"].raw
    assert parsed.tasks_by_id()["CLI-05"].raw == before.tasks_by_id()["CLI-05"].raw


def test_close_reports_stale_blocked_by_referrers(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "close", "FEAT-15")
    assert rc == 0
    assert "STALE  FEAT-16  Add ETF exclusion" in out.splitlines()


def test_close_creates_done_md_with_frontmatter_when_absent(tasks_file, done_file, capsys):
    assert not done_file.exists()
    run(tasks_file, done_file, "close", "FEAT-15")
    assert done_file.exists()
    text = done_file.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "type: requirement" in text
    assert "state: done" in text
    assert "cross-file-references: [docs/TASKS.md]" in text
    assert "# Done" in text


def test_close_moves_task_verbatim_plus_closed_and_commits_bullets(tasks_file, done_file, capsys):
    before = tasks.parse_tasks_file(FIXTURE).tasks_by_id()["FEAT-15"]
    run(tasks_file, done_file, "close", "FEAT-15")
    done_text = done_file.read_text(encoding="utf-8")
    parsed_done = tasks.parse_tasks_file(done_text)  # must parse cleanly
    moved = parsed_done.tasks_by_id()["FEAT-15"]

    assert moved.title == before.title
    assert moved.goal == before.goal
    assert moved.details == before.details
    assert moved.done_when == before.done_when
    assert moved.meta["prio"] == before.meta["prio"]
    assert moved.meta["module"] == before.meta["module"]
    assert moved.meta["refs"] == before.meta["refs"]
    assert moved.meta["closed"] == _dt.date.today().isoformat()
    assert "commits" in moved.meta


def test_close_creates_module_section_in_done_md(tasks_file, done_file, capsys):
    run(tasks_file, done_file, "close", "CLI-05")
    parsed_done = tasks.parse_tasks_file(done_file.read_text(encoding="utf-8"))
    interfaces_section = next(s for s in parsed_done.sections if s.name == "interfaces")
    assert [t.id for t in interfaces_section.tasks] == ["CLI-05"]


def test_close_appends_to_existing_module_section_in_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE, encoding="utf-8")  # already has "## features" w/ FEAT-24
    run(tasks_file, done_file, "close", "FEAT-15")
    parsed_done = tasks.parse_tasks_file(done_file.read_text(encoding="utf-8"))
    features_sections = [s for s in parsed_done.sections if s.name == "features"]
    assert len(features_sections) == 1
    assert [t.id for t in features_sections[0].tasks] == ["FEAT-24", "FEAT-15"]


def test_close_bumps_updated_on_both_files(tasks_file, done_file, capsys):
    run(tasks_file, done_file, "close", "FEAT-15")
    tasks_text = tasks_file.read_text(encoding="utf-8")
    assert "updated: 2026-01-01" not in tasks_text
    assert "created: 2026-01-01" in tasks_text  # created untouched
    done_text = done_file.read_text(encoding="utf-8")
    assert f"updated: {_dt.date.today().isoformat()}" in done_text


def test_close_unknown_id_no_write(tasks_file, done_file, capsys):
    rc, out, err = run_captured(capsys, tasks_file, done_file, "close", "NOPE-99")
    assert rc == 1
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE
    assert not done_file.exists()


def test_close_last_task_in_a_section_does_not_eat_following_section_header(tasks_file, done_file, capsys):
    """FEAT-16 is the last task in '## features', immediately followed by
    '## interfaces'. Closing it must leave the '## interfaces' header (and
    CLI-05 under it) fully intact — not swallowed as part of FEAT-16's span."""
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "close", "FEAT-16")
    assert rc == 0
    text = tasks_file.read_text(encoding="utf-8")
    assert "## interfaces\n" in text
    parsed = tasks.parse_tasks_file(text)
    interfaces_section = next(s for s in parsed.sections if s.name == "interfaces")
    assert [t.id for t in interfaces_section.tasks] == ["CLI-05"]
    assert parsed.tasks_by_id()["CLI-05"].raw == tasks.parse_tasks_file(FIXTURE).tasks_by_id()["CLI-05"].raw


def test_close_last_task_in_last_section_leaves_section_header_and_eof_clean(tasks_file, done_file, capsys):
    """CLI-05 is the only task in '## interfaces', the last section in the
    file (nothing follows it, no trailing '## ' or frontmatter '---' to eat)."""
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "close", "CLI-05")
    assert rc == 0
    text = tasks_file.read_text(encoding="utf-8")
    # section header + its preamble (the blank line before where the task was)
    # survive untouched; only the task's own span is gone.
    assert text.endswith("## interfaces\n\n")
    parsed = tasks.parse_tasks_file(text)  # must still parse cleanly
    interfaces_section = next(s for s in parsed.sections if s.name == "interfaces")
    assert interfaces_section.tasks == []
    assert [t.id for t in parsed.all_tasks()] == ["FEAT-15", "FEAT-16"]


def test_close_task_with_literal_horizontal_rule_in_body(tmp_path: Path, done_file: Path, capsys) -> None:
    """A task body may legitimately contain a literal '---' line (preserved
    verbatim, e.g. a markdown horizontal rule in Details). It must not be
    mistaken for a frontmatter delimiter or otherwise corrupt parsing, and
    closing that task must remove exactly its span."""
    text_with_rule = FIXTURE.replace(
        "**Details.**\n- do a thing\n- do another thing\n",
        "**Details.**\n- do a thing\n\n---\n\n- do another thing\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text_with_rule, encoding="utf-8")

    parsed = tasks.parse_tasks_file(text_with_rule)  # must not raise / mis-split at '---'
    assert parsed.render() == text_with_rule
    assert "---" in parsed.tasks_by_id()["FEAT-15"].details

    rc, out, _ = run_captured(capsys, p, done_file, "close", "FEAT-15")
    assert rc == 0
    result_text = p.read_text(encoding="utf-8")
    result = tasks.parse_tasks_file(result_text)  # frontmatter still parses correctly
    assert [t.id for t in result.all_tasks()] == ["FEAT-16", "CLI-05"]
    assert result.frontmatter_raw.startswith("---\n")
    assert "updated: 2026-01-01" not in result_text


def test_close_commits_none_found_when_no_git_repo(tasks_file, done_file, capsys):
    """tmp_path fixtures aren't git repos: commit lookup must degrade to
    '(none found)' rather than failing the close."""
    run(tasks_file, done_file, "close", "FEAT-15")
    done_text = done_file.read_text(encoding="utf-8")
    parsed_done = tasks.parse_tasks_file(done_text)
    assert parsed_done.tasks_by_id()["FEAT-15"].meta["commits"] == "(none found)"


def test_close_finds_matching_commits_via_git_grep(tmp_path: Path, monkeypatch, capsys) -> None:
    proj = tmp_path / "proj"
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "TASKS.md").write_text(FIXTURE, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=proj, check=True)
    (proj / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "wip(feat-15): landed the sentiment fix"], cwd=proj, check=True)

    monkeypatch.chdir(proj)
    rc = tasks.main(["--file", str(docs_dir / "TASKS.md"), "--done-file", str(docs_dir / "DONE.md"), "close", "FEAT-15"])
    assert rc == 0
    done_text = (docs_dir / "DONE.md").read_text(encoding="utf-8")
    assert "landed the sentiment fix" in done_text


# --------------------------------------------------------------------------
# ledger (docs/TASKS-retired.md) concept fully removed
# --------------------------------------------------------------------------
def test_ledger_flag_no_longer_exists():
    with pytest.raises(SystemExit):
        tasks.main(["--ledger", "/tmp/whatever", "list"])


def test_ledger_helpers_removed_from_module():
    assert not hasattr(tasks, "parse_ledger_ids")
    assert not hasattr(tasks, "_append_ledger_entry")
    assert not hasattr(tasks, "_default_ledger_text")
    assert not hasattr(tasks, "_LEDGER_ENTRY_RE")
    assert not hasattr(tasks, "_DEFAULT_LEDGER_PATH")


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
def test_validate_clean_fixture_passes(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "validate")
    assert rc == 0
    assert out == ""


def test_validate_catches_duplicate_id(tmp_path, done_file, capsys):
    dup_text = FIXTURE.replace("### FEAT-16 — Add ETF exclusion", "### FEAT-15 — Add ETF exclusion")
    p = tmp_path / "TASKS.md"
    p.write_text(dup_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "duplicate id FEAT-15" in out


def test_validate_catches_bad_prio(tmp_path, done_file, capsys):
    bad_text = FIXTURE.replace("- prio:: high\n", "- prio:: urgent\n")
    p = tmp_path / "TASKS.md"
    p.write_text(bad_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "invalid prio" in out


def test_validate_catches_dangling_blocker(tmp_path, done_file, capsys):
    dangling_text = FIXTURE.replace("- blocked-by:: FEAT-15", "- blocked-by:: FEAT-999")
    p = tmp_path / "TASKS.md"
    p.write_text(dangling_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "dangling reference 'FEAT-999'" in out


def test_validate_catches_fence_inside_task_body(tmp_path, done_file, capsys):
    """A ``` fence inside a task's own span (header through the next header/EOF)
    is the actual migration debt (v1 wrapped every task in a code fence)."""
    fenced_text = FIXTURE.replace(
        "**Done when.** ETF tickers no longer counted.\n",
        "**Done when.** ETF tickers no longer counted.\n\n```\nstray fence left over from v1\n```\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(fenced_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "leftover code fence" in out
    assert "FEAT-16" in out


def test_validate_ignores_fence_in_preamble(tmp_path, done_file, capsys):
    """A ```bash usage example under '## How to use this file' is legitimate —
    only fences inside a task's own span are migration debt."""
    documented_text = FIXTURE.replace(
        "## How to use this file\nSome instructions here. Don't reuse retired IDs.\n",
        "## How to use this file\nSome instructions here. Don't reuse retired IDs.\n\n"
        "```bash\ntasks.py list --prio high\n```\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(documented_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0
    assert out == ""


def test_validate_catches_missing_required_field(tmp_path, done_file, capsys):
    missing_text = FIXTURE.replace("- prio:: high\n", "")
    p = tmp_path / "TASKS.md"
    p.write_text(missing_text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "missing required field 'prio'" in out


def test_validate_warns_on_id_also_in_done_md(tasks_file, done_file, capsys):
    done_file.write_text(DONE_FIXTURE.replace("FEAT-24", "FEAT-15"), encoding="utf-8")
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "validate")
    assert rc == 1
    assert "FEAT-15" in out
    assert "DONE.md" in out


def test_validate_does_not_flag_blocked_by_referencing_a_closed_id_as_dangling(tasks_file, done_file, capsys):
    """After closing FEAT-15 (which moves to DONE.md and is removed from
    TASKS.md), FEAT-16's blocked-by:: FEAT-15 must not be flagged dangling —
    the id is still known, just relocated."""
    run(tasks_file, done_file, "close", "FEAT-15")
    capsys.readouterr()  # discard close's own stdout before checking validate's
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "validate")
    assert rc == 0
    assert out == ""


# --------------------------------------------------------------------------
# strict ID shape: [A-Z]{2,4}-[0-9]+, enforced everywhere an ID is parsed or
# accepted. ANL-16-STEP2 / DB-TS-MIGRATION (retired shapes) must now be
# rejected, not tolerated.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("good", ["FET-15", "CLI-05", "SCH-17", "META-07", "FEAT-27", "UI-01", "NOTI-1"])
def test_id_regex_accepts_conforming_shapes(good):
    assert tasks._ID_RE.match(good), good


@pytest.mark.parametrize("bad", ["ANL-16-STEP2", "DB-TS-MIGRATION", "toolong-1", "A-1", "TOOLONG-1", "FEAT15"])
def test_id_regex_rejects_nonconforming_shapes(bad):
    assert not tasks._ID_RE.match(bad), bad


def test_parser_refuses_malformed_task_header(malformed_id_file: Path) -> None:
    """The parser must refuse to parse a malformed task header rather than
    silently accepting it — a whole-file parse error, not a partial parse."""
    text = malformed_id_file.read_text(encoding="utf-8")
    with pytest.raises(tasks.TasksParseError):
        tasks.parse_tasks_file(text)


def test_validate_flags_malformed_id_file(malformed_id_file, done_file, capsys):
    """`validate` (run via `main`) must FLAG a file containing a malformed
    task id rather than passing it silently — surfaced here as the parser's
    refusal to load the file at all."""
    rc, out, err = run_captured(capsys, malformed_id_file, done_file, "validate")
    assert rc != 0
    assert "ANL-16-STEP2" in err or "malformed" in err.lower()


# --------------------------------------------------------------------------
# CHANGE 1: path resolution via git repo root, not script-relative / cwd-parent-walk
# --------------------------------------------------------------------------
def test_registry_resolved_from_subdirectory_matches_root(tmp_path: Path, monkeypatch, capsys) -> None:
    proj = tmp_path / "proj"
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "TASKS.md").write_text(FIXTURE, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)

    sub = proj / "src" / "nested"
    sub.mkdir(parents=True)

    monkeypatch.chdir(proj)
    rc_root = tasks.main(["list", "--ids-only"])
    out_root = capsys.readouterr().out
    assert rc_root == 0

    monkeypatch.chdir(sub)
    rc_sub = tasks.main(["list", "--ids-only"])
    out_sub = capsys.readouterr().out
    assert rc_sub == 0
    assert out_sub == out_root


def test_no_git_repo_exits_nonzero_with_clear_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tasks, "_find_repo_root", lambda start: None)
    rc = tasks.main(["list"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "git" in err.lower()


def test_git_repo_without_docs_tasks_exits_nonzero_naming_exact_path(tmp_path: Path, monkeypatch, capsys) -> None:
    proj = tmp_path / "proj-no-docs"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    monkeypatch.chdir(proj)
    rc = tasks.main(["list"])
    assert rc != 0
    err = capsys.readouterr().err
    assert str(proj / "docs" / "TASKS.md") in err


def test_close_without_explicit_done_file_uses_sibling_done_md(tmp_path: Path, monkeypatch, capsys) -> None:
    proj = tmp_path / "proj-sibling"
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "TASKS.md").write_text(FIXTURE, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)

    monkeypatch.chdir(proj)
    rc = tasks.main(["close", "FEAT-15"])
    assert rc == 0
    assert (docs_dir / "DONE.md").exists()
    done_text = (docs_dir / "DONE.md").read_text(encoding="utf-8")
    assert "FEAT-15" in done_text


# --------------------------------------------------------------------------
# edit — surgical in-place edits; round-trip safety for every untouched byte
# --------------------------------------------------------------------------
def _raws_of(text: str) -> dict:
    return {t.id: t.raw for t in tasks.parse_tasks_file(text).all_tasks()}


def test_edit_prio_changes_only_that_bullet_and_stamps_edited(tasks_file, done_file, capsys):
    before = _raws_of(FIXTURE)
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-16", "--prio", "high")
    assert rc == 0
    assert out.strip() == "FEAT-16"
    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    t = parsed.tasks_by_id()["FEAT-16"]
    assert t.prio == "high"
    assert t.meta["edited"] == _dt.date.today().isoformat()
    # untouched pieces of the edited task preserved verbatim
    assert t.goal == "Exclude ETF tickers from sentiment counts."
    assert t.blocked_by == "FEAT-15"
    assert t.done_when == "ETF tickers no longer counted."
    # every other task byte-identical
    after = _raws_of(text)
    assert after["FEAT-15"] == before["FEAT-15"]
    assert after["CLI-05"] == before["CLI-05"]
    # frontmatter updated: bumped, created untouched
    assert "updated: 2026-01-01" not in text
    assert "created: 2026-01-01" in text


def test_edit_clear_blocked_by_removes_bullet_only(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-16", "--clear-blocked-by")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-16"]
    assert t.blocked_by is None
    assert "- blocked-by::" not in t.raw
    assert t.refs == "src/features/sentiment.py, docs/BUSINESS_LOGIC-features-sentiment.md"
    assert t.goal == "Exclude ETF tickers from sentiment counts."


def test_edit_clear_absent_blocked_by_aborts_writing_nothing(tasks_file, done_file, capsys):
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--clear-blocked-by")
    assert rc == 1
    assert "blocked-by" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_set_blocked_by_inserts_at_canonical_position(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--blocked-by", "CLI-05")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    keys = [tasks._BULLET_RE.match(ln).group("key") for ln in t.raw.splitlines() if tasks._BULLET_RE.match(ln)]
    assert keys == ["prio", "module", "created", "blocked-by", "refs", "edited"]
    assert t.blocked_by == "CLI-05"


def test_edit_blocked_by_annotated_value_accepted(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(
        capsys, tasks_file, done_file, "edit", "FEAT-15",
        "--blocked-by", "CLI-05 (waiting on status cleanup, see docs)",
    )
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.blocked_by == "CLI-05 (waiting on status cleanup, see docs)"


def test_edit_blocked_by_not_starting_with_id_rejected(tasks_file, done_file, capsys):
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--blocked-by", "see notes below")
    assert rc == 2
    assert "blocked-by" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_invalid_prio_rejected(tasks_file, done_file, capsys):
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--prio", "urgent")
    assert rc == 2
    assert "prio" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_unknown_id_aborts(tasks_file, done_file, capsys):
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "NOPE-99", "--prio", "low")
    assert rc == 1
    assert "NOPE-99" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_no_flags_is_an_error(tasks_file, done_file, capsys):
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15")
    assert rc == 2
    assert "nothing to change" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_goal_full_replace_preserves_other_sections(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--goal", "A corrected goal.")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.goal == "A corrected goal."
    assert t.details == "- do a thing\n- do another thing"
    assert t.done_when == "scoring is better."


def test_edit_append_details_keeps_existing_lines_verbatim(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--append-details", "- do a third thing")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.details == "- do a thing\n- do another thing\n- do a third thing"
    assert t.goal == "Make sentiment scoring better."
    assert t.done_when == "scoring is better."


def test_edit_append_to_missing_section_aborts(tasks_file, done_file, capsys):
    # FEAT-16 has no Details section
    rc, _, err = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-16", "--append-details", "- note")
    assert rc == 1
    assert "Details" in err
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_replace_creates_missing_details_in_canonical_position(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-16", "--details", "- a new detail")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-16"]
    assert t.details == "- a new detail"
    body = t.raw
    assert body.index("**Goal.**") < body.index("**Details.**") < body.index("**Done when.**")


def test_edit_title_rewrites_header_only(tasks_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--title", "A sharper title")
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.title == "A sharper title"
    assert t.raw.startswith("### FEAT-15 — A sharper title\n")
    assert t.goal == "Make sentiment scoring better."


def test_edit_module_moves_task_to_inferred_section(sections_file, done_file, capsys):
    before = _raws_of(SECTIONS_FIXTURE)
    rc, _, _ = run_captured(capsys, sections_file, done_file, "edit", "CLI-01", "--module", "interfaces.ui")
    assert rc == 0
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    cli_section = next(s for s in parsed.sections if s.name == "interfaces — cli")
    ui_section = next(s for s in parsed.sections if s.name == "interfaces — ui")
    assert "CLI-01" not in [t.id for t in cli_section.tasks]
    assert [t.id for t in ui_section.tasks] == ["UI-01", "CLI-01"]
    moved = parsed.tasks_by_id()["CLI-01"]
    assert moved.module == "interfaces.ui"
    # every other task untouched
    after = _raws_of(sections_file.read_text(encoding="utf-8"))
    for tid in ("FET-01", "ANL-01", "UI-01"):
        assert after[tid] == before[tid]


def test_edit_section_alone_moves_without_touching_module_bullet(sections_file, done_file, capsys):
    rc, _, _ = run_captured(capsys, sections_file, done_file, "edit", "FET-01", "--section", "analysis")
    assert rc == 0
    parsed = tasks.parse_tasks_file(sections_file.read_text(encoding="utf-8"))
    analysis_section = next(s for s in parsed.sections if s.name == "analysis")
    assert "FET-01" in [t.id for t in analysis_section.tasks]
    assert parsed.tasks_by_id()["FET-01"].module == "fetcher"  # bullet unchanged


def test_edit_unknown_target_section_aborts(sections_file, done_file, capsys):
    rc, _, err = run_captured(capsys, sections_file, done_file, "edit", "FET-01", "--section", "nonexistent")
    assert rc == 1
    assert "nonexistent" in err
    assert sections_file.read_text(encoding="utf-8") == SECTIONS_FIXTURE


def test_edit_unmapped_module_without_section_aborts(sections_file, done_file, capsys):
    rc, _, err = run_captured(capsys, sections_file, done_file, "edit", "FET-01", "--module", "unknownmod")
    assert rc == 1
    assert "unknownmod" in err
    assert sections_file.read_text(encoding="utf-8") == SECTIONS_FIXTURE


def test_edit_file_final_task_roundtrips_cleanly(tasks_file, done_file, capsys):
    before = _raws_of(FIXTURE)
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "CLI-05", "--prio", "high")
    assert rc == 0
    text = tasks_file.read_text(encoding="utf-8")
    parsed = tasks.parse_tasks_file(text)
    assert parsed.render() == text  # round-trip survives
    after = _raws_of(text)
    assert after["FEAT-15"] == before["FEAT-15"]
    assert after["FEAT-16"] == before["FEAT-16"]


def test_edit_result_still_validates_clean(tasks_file, done_file, capsys):
    run(tasks_file, done_file, "edit", "FEAT-16", "--prio", "high", "--clear-blocked-by")
    capsys.readouterr()
    rc, out, _ = run_captured(capsys, tasks_file, done_file, "validate")
    assert rc == 0
    assert out == ""


def test_edit_goal_file_reads_from_disk(tasks_file, done_file, tmp_path, capsys):
    gf = tmp_path / "goal.txt"
    gf.write_text("Goal loaded from a file.\n", encoding="utf-8")
    rc, _, _ = run_captured(capsys, tasks_file, done_file, "edit", "FEAT-15", "--goal-file", str(gf))
    assert rc == 0
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.goal == "Goal loaded from a file."


def test_edit_missing_goal_file_aborts_writing_nothing(tasks_file, done_file, tmp_path, capsys):
    rc, _, err = run_captured(
        capsys, tasks_file, done_file, "edit", "FEAT-15", "--goal-file", str(tmp_path / "nope.txt"),
    )
    assert rc == 1
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_edit_second_edit_updates_stamp_in_place(tasks_file, done_file, capsys):
    run(tasks_file, done_file, "edit", "FEAT-15", "--prio", "medium")
    run(tasks_file, done_file, "edit", "FEAT-15", "--prio", "low")
    capsys.readouterr()
    t = tasks.parse_tasks_file(tasks_file.read_text(encoding="utf-8")).tasks_by_id()["FEAT-15"]
    assert t.prio == "low"
    assert t.raw.count("- edited::") == 1


# --------------------------------------------------------------------------
# validate — new structural / field-format checks (errors)
# --------------------------------------------------------------------------
def test_validate_catches_empty_goal(tmp_path, done_file, capsys):
    text = FIXTURE.replace("**Goal.** Make sentiment scoring better.\n", "**Goal.**\n")
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "FEAT-15" in out
    assert "Goal" in out


def test_validate_catches_bad_created_date(tmp_path, done_file, capsys):
    text = FIXTURE.replace("- created:: 2026-01-01\n", "- created:: sometime\n", 1)
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "not a valid ISO date" in out
    assert "created" in out


def test_validate_catches_bad_edited_date(tmp_path, done_file, capsys):
    text = FIXTURE.replace(
        "- created:: 2026-01-01\n- refs:: src/features/sentiment.py\n",
        "- created:: 2026-01-01\n- refs:: src/features/sentiment.py\n- edited:: not-a-date\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "edited" in out
    assert "not a valid ISO date" in out


def test_validate_catches_blocked_by_not_starting_with_id(tmp_path, done_file, capsys):
    text = FIXTURE.replace("- blocked-by:: FEAT-15\n", "- blocked-by:: see notes below\n")
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "malformed blocked-by" in out


def test_validate_accepts_annotated_blocked_by(tmp_path, done_file, capsys):
    """Real registries annotate blockers: 'FEAT-18 (deletes the file, see docs)'.
    An annotation — commas and all — must not fail validation."""
    text = FIXTURE.replace(
        "- blocked-by:: FEAT-15\n",
        "- blocked-by:: FEAT-15 (deletes the vader interface, mirror the new one)\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0
    assert out == ""


def test_validate_catches_empty_refs_token(tmp_path, done_file, capsys):
    text = FIXTURE.replace(
        "- refs:: src/features/sentiment.py\n",
        "- refs:: src/features/sentiment.py,, docs/foo.md\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 1
    assert "refs has an empty entry" in out


def test_validate_accepts_annotated_refs(tmp_path, done_file, capsys):
    """Real refs carry spaces, parentheses and '§' anchors — all legitimate."""
    text = FIXTURE.replace(
        "- refs:: src/features/sentiment.py\n",
        "- refs:: docs/REQUIREMENTS-analysis.md §Claude conviction scoring, src/db/schema.py (annotated_thread.language)\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0


# --------------------------------------------------------------------------
# validate — warnings (reported, exit code stays 0)
# --------------------------------------------------------------------------
def test_validate_warns_on_missing_done_when_without_failing(tmp_path, done_file, capsys):
    text = FIXTURE.replace("**Done when.** scoring is better.\n", "")
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0
    assert "warning:" in out
    assert "Done when" in out
    assert "FEAT-15" in out


def test_validate_warns_on_bare_angle_placeholder_without_failing(tmp_path, done_file, capsys):
    text = FIXTURE.replace(
        "**Goal.** Make sentiment scoring better.\n",
        "**Goal.** Make scoring better for each <ticker> we track.\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0
    assert "warning:" in out
    assert "<ticker>" in out
    assert "backtick" in out


def test_validate_no_warning_for_backticked_placeholder(tmp_path, done_file, capsys):
    text = FIXTURE.replace(
        "**Goal.** Make sentiment scoring better.\n",
        "**Goal.** Make scoring better for each `<ticker>` we track.\n",
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text, encoding="utf-8")
    rc, out, _ = run_captured(capsys, p, done_file, "validate")
    assert rc == 0
    assert out == ""


def test_add_refuses_bare_angle_placeholder_in_goal(tasks_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-99", "--title", "New thing", "--prio", "low", "--module", "features",
        "--goal", "Handle each <ticker> we track.", "--done-when", "d",
    )
    assert rc != 0
    assert "<ticker>" in err
    assert "backtick" in err
    # nothing written — file byte-identical to the fixture
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_add_accepts_backticked_angle_placeholder_in_goal(tasks_file, done_file, capsys):
    rc, out, _ = run_captured(
        capsys, tasks_file, done_file, "add",
        "--id", "FEAT-99", "--title", "New thing", "--prio", "low", "--module", "features",
        "--goal", "Handle each `<ticker>` we track.", "--done-when", "d",
    )
    assert rc == 0
    assert out.strip() == "FEAT-99"


def test_edit_refuses_bare_angle_placeholder_in_goal(tasks_file, done_file, capsys):
    rc, out, err = run_captured(
        capsys, tasks_file, done_file, "edit", "FEAT-15",
        "--append-goal", "Also handle each <ticker> we track.",
    )
    assert rc != 0
    assert "<ticker>" in err
    assert "backtick" in err
    # nothing written — file byte-identical to the fixture
    assert tasks_file.read_text(encoding="utf-8") == FIXTURE


def test_scan_bare_raw_html_unit():
    hits = tasks._scan_bare_raw_html("### X-1 — t\n\n**Goal.** a <date> and `<ID>` and </tag> and <br/>\n")
    tokens = [tok for _, tok in hits]
    assert tokens == ["<date>", "</tag>", "<br/>"]
    # fenced block protects its content
    fenced = "### X-1 — t\n\n**Goal.** g\n\n```\n<date> inside fence\n```\n"
    assert tasks._scan_bare_raw_html(fenced) == []
    # autolinks / URLs never match (':' and '/' aren't tag-name characters)
    assert tasks._scan_bare_raw_html("**Goal.** see <https://example.com> now\n") == []
    # double-backtick code span containing a single backtick still protects
    assert tasks._scan_bare_raw_html("**Goal.** use `` `<date>` `` quoted\n") == []
