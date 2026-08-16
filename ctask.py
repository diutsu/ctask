#!/usr/bin/env python3
"""Helper CLI for reading / listing / searching / writing tasks in `docs/TASKS.md`.

`docs/TASKS.md` (v2 format — see docs/plan-tasks-format-v2 spec) holds every open
task as an H3 entry under a `## <module>` section:

    ### SCH-17 — mimir disk pressure blocks the queue model

    - prio:: high
    - module:: interfaces.scheduler
    - created:: 2026-07-15
    - blocked-by:: SCH-12
    - refs:: docs/executed-mimir-diagnostic-2026-07-11.md, src/storage/db/replica.py

    **Goal.** ...

    **Details.**
    - ...

    **Done when.** ...

This script exists so an agent (or the user) can ask a precise question — "what's
blocking FEAT-24", "which tasks touch src/storage/db/repository.py" — and get a
short, greppable answer back instead of loading the whole (many-KB) file into
context.

Round-trip safety is the load-bearing property: the file lives under a gitignored
Obsidian vault symlink, so a bad write is unrecoverable. Parsing keeps every task
as a verbatim raw span of the source text; `close` moves a span (verbatim, plus
two appended metadata bullets) into `docs/DONE.md`, `add` appends a new span, and
untouched spans are never re-rendered — so a parse of an unmodified file,
serialized back out, is byte-identical to the input. If parsing fails for any
reason, nothing is written.

Per-project module map: an optional `<repo_root>/docs/.ctask` JSON file
(`{"modules": {"cr": {"prefix": "CR", "section": "..."}}}`) extends and
overrides the builtin `_MODULE_PREFIX_MAP` for that repo only — the builtin
map is always the fallback/default. See `_load_project_module_map`.

This script lives in a global skill directory shared by every project, so the
registry is resolved from the current git repository, not from the script's own
location: `git rev-parse --show-toplevel` finds the repo root, and the registry
is `<root>/docs/TASKS.md` (done ledger: `<root>/docs/DONE.md`). Use `--file` /
`--done-file` to override (mainly for tests).

Usage:
    tasks.py list [--prio P] [--module M] [--blocked] [--unblocked] [--ids-only]
    tasks.py show ID [ID ...]
        (falls back to docs/DONE.md for any id not open in TASKS.md, marking
        the entry "[CLOSED — from DONE.md]"; only genuinely absent from both
        is reported as unknown)
    tasks.py search TEXT [--field goal|details|done|title|all] [--ids-only]
        [--include-done]  (also search docs/DONE.md; archived hits are
        labelled "[CLOSED]" so they're never mistaken for open work)
    tasks.py blockers ID
    tasks.py blocking ID
    tasks.py refs [--referencing PATH] [ID]
    tasks.py add --id ID | --id-prefix PREFIX | (nothing, inferred from --module)
        --title T --prio P --module M [--section "## name" override, else inferred from --module]
        [--blocked-by X] [--refs R] [--goal G] [--details D] [--done-when W]
        [--goal-file F] [--details-file F] [--done-when-file F] [--stdin]
    tasks.py edit ID [--title T] [--prio P]
        [--module M] [--section "## name"]      (either one moves the task's span)
        [--blocked-by X | --clear-blocked-by] [--refs R | --clear-refs]
        [--goal G | --goal-file F | --append-goal T]
        [--details D | --details-file F | --append-details T]
        [--done-when W | --done-when-file F | --append-done-when T]
    tasks.py close ID
    tasks.py validate
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_VALID_PRIOS = {"high", "medium", "low", "deferred"}
# The one ID shape, enforced everywhere an ID is parsed or accepted: 2-4
# uppercase letters, a hyphen, then digits (e.g. FEAT-15, SCH-17). No
# hyphenated-prefix or non-numeric-suffix accommodations — those shapes
# (ANL-16-STEP2, DB-TS-MIGRATION) are retired; historical references to them
# live only in git history / old docs and are never re-parsed by this script.
_ID_PREFIX_PATTERN = r"[A-Z]{2,4}"
_ID_PATTERN = rf"{_ID_PREFIX_PATTERN}-[0-9]+"
_ID_RE = re.compile(rf"^{_ID_PATTERN}$")
_ID_PREFIX_RE = re.compile(rf"^{_ID_PREFIX_PATTERN}$")
_TASK_HEADER_RE = re.compile(rf"^### (?P<id>{_ID_PATTERN}) — (?P<title>.+?)\s*\n?$")
_SECTION_HEADER_RE = re.compile(r"^## (?P<name>.+?)\s*\n?$")
_BULLET_RE = re.compile(r"^- (?P<key>[a-zA-Z][a-zA-Z-]*):: ?(?P<value>.*?)\s*\n?$")
_BODY_MARK_RE = re.compile(r"^\*\*(?P<label>Goal|Details|Done when)\.\*\* ?(?P<rest>.*)$")
_FENCE_RE = re.compile(r"^```")

# '## ' headers that use H2 markup but are structural preamble, not task-bearing
# module sections (spec: "the H1, the TOC, and 'How to use this file' stay").
_NON_MODULE_SECTION_NAMES = {"TOC", "How to use this file"}

# --module -> (id-prefix, default "## <section>" name). Matched on the module's
# leading segment(s), longest-match-wins (e.g. "interfaces.cli" -> CLI /
# "interfaces — cli", not a bare "interfaces" entry, which doesn't exist).
# Single source of truth for BOTH id-prefix inference and section placement —
# a task's `--module` (e.g. "analysis.signals") almost never spells its
# section header ("analysis") literally, so these must not be two maps that
# can drift apart. Source of truth: docs/TASKS.md §"How to use this file" +
# its actual "## " section headers (verified against the real file).
_MODULE_PREFIX_MAP = {
    "fetcher": ("FET", "fetcher"),
    "features": ("FEAT", "features"),
    "backtest": ("BACK", "backtest"),
    "analysis": ("ANL", "analysis"),
    "storage": ("STOR", "storage"),
    "interfaces.cli": ("CLI", "interfaces — cli"),
    "interfaces.ui": ("UI", "interfaces — ui"),
    "interfaces.scheduler": ("SCH", "interfaces — scheduler"),
    "interfaces.notifications": ("NOTI", "interfaces — notifications"),
    "interfaces.mcp": ("MCP", "interfaces — mcp"),
    "meta": ("META", "meta — cross-cutting"),
}


class TasksParseError(Exception):
    """Raised on any structural problem in TASKS.md. Never triggers a write."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Task:
    id: str
    title: str
    section: str  # the ## module-section name this task lives under
    meta: dict[str, str]  # ordered, all "- key:: value" bullets (known + extra)
    goal: str
    details: str | None
    done_when: str
    raw: str  # verbatim source text of the whole block (header through end)
    header_line: int  # 1-based line number of the "### ID" line

    @property
    def prio(self) -> str | None:
        return self.meta.get("prio")

    @property
    def module(self) -> str | None:
        return self.meta.get("module")

    @property
    def created(self) -> str | None:
        return self.meta.get("created")

    @property
    def blocked_by(self) -> str | None:
        return self.meta.get("blocked-by")

    @property
    def refs(self) -> str | None:
        return self.meta.get("refs")


@dataclass
class Section:
    header_raw: str  # "## <name>\n" verbatim
    name: str
    preamble_raw: str  # text between the header and the first task (usually blank line)
    tasks: list[Task] = field(default_factory=list)

    def render(self) -> str:
        return self.header_raw + self.preamble_raw + "".join(t.raw for t in self.tasks)


@dataclass
class ParsedFile:
    frontmatter_raw: str
    preamble_raw: str  # H1 / TOC / "How to use this file", verbatim
    sections: list[Section]

    def render(self) -> str:
        return self.frontmatter_raw + self.preamble_raw + "".join(s.render() for s in self.sections)

    def all_tasks(self) -> list[Task]:
        return [t for s in self.sections for t in s.tasks]

    def tasks_by_id(self) -> dict[str, Task]:
        out: dict[str, Task] = {}
        for t in self.all_tasks():
            out.setdefault(t.id, t)  # first wins; duplicates are a validate error
        return out


def _split_body(raw_body: str) -> tuple[str, str | None, str]:
    """Split the text after the metadata block into (goal, details, done_when)."""
    matches = list(re.finditer(_BODY_MARK_RE.pattern, raw_body, re.MULTILINE))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group("label")
        rest = m.group("rest")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_body)
        sections[label] = (rest + raw_body[start:end]).strip("\n")
    goal = sections.get("Goal", "").strip()
    details = sections.get("Details")
    if details is not None:
        details = details.strip()
    done_when = sections.get("Done when", "").strip()
    return goal, details, done_when


def _parse_task_block(lines: list[str], start_idx: int, end_idx: int, section_name: str, line_no_base: int) -> Task:
    """Parse one task's raw lines (lines[start_idx:end_idx]) into a Task.

    line_no_base: 1-based line number of lines[start_idx] in the source file.
    """
    header_line = lines[start_idx]
    m = _TASK_HEADER_RE.match(header_line)
    if not m:
        raise TasksParseError(f"line {line_no_base}: malformed task header: {header_line!r}")
    task_id = m.group("id")
    title = m.group("title")

    raw = "".join(lines[start_idx:end_idx])

    i = start_idx + 1
    # skip blank lines before the metadata block
    while i < end_idx and lines[i].strip() == "":
        i += 1
    meta: dict[str, str] = {}
    while i < end_idx:
        bm = _BULLET_RE.match(lines[i])
        if not bm:
            break
        meta[bm.group("key")] = bm.group("value")
        i += 1
    # remainder is the body
    body_text = "".join(lines[i:end_idx])
    goal, details, done_when = _split_body(body_text)

    return Task(
        id=task_id,
        title=title,
        section=section_name,
        meta=meta,
        goal=goal,
        details=details,
        done_when=done_when,
        raw=raw,
        header_line=line_no_base,
    )


def parse_tasks_file(text: str) -> ParsedFile:
    lines = text.splitlines(keepends=True)
    n = len(lines)
    i = 0

    # -- frontmatter --
    frontmatter_lines: list[str] = []
    if n and lines[0].rstrip("\n") == "---":
        frontmatter_lines.append(lines[0])
        i = 1
        while i < n and lines[i].rstrip("\n") != "---":
            frontmatter_lines.append(lines[i])
            i += 1
        if i >= n:
            raise TasksParseError("frontmatter opened with '---' but never closed")
        frontmatter_lines.append(lines[i])
        i += 1
    frontmatter_raw = "".join(frontmatter_lines)

    # -- preamble (H1 / TOC / how-to-use) up to the first real module section --
    preamble_start = i
    while i < n and not lines[i].startswith("## "):
        i += 1
    # TOC and "How to use this file" use H2 markup like a module section but are
    # NOT task-bearing (per spec: "the H1, the TOC, and 'How to use this file'
    # stay"). They may contain their own explanatory H3 subheadings ("### Use
    # the script, not your eyes") that must not be mistaken for task headers,
    # so absorb them into the preamble instead of parsing them as sections.
    while i < n and lines[i].startswith("## "):
        sm = _SECTION_HEADER_RE.match(lines[i])
        name = sm.group("name") if sm else None
        if name not in _NON_MODULE_SECTION_NAMES:
            break
        j = i + 1
        while j < n and not lines[j].startswith("## "):
            j += 1
        i = j
    preamble_raw = "".join(lines[preamble_start:i])

    # -- sections --
    sections: list[Section] = []
    while i < n and lines[i].startswith("## "):
        sm = _SECTION_HEADER_RE.match(lines[i])
        if not sm:
            raise TasksParseError(f"line {i + 1}: malformed section header: {lines[i]!r}")
        header_raw = lines[i]
        section_name = sm.group("name")
        i += 1
        content_start = i
        while i < n and not lines[i].startswith("## "):
            i += 1
        content_end = i
        content_lines = lines[content_start:content_end]

        # split section content into preamble (before first '### ') + task blocks
        task_starts = [j for j, ln in enumerate(content_lines) if ln.startswith("### ")]
        pre_len = task_starts[0] if task_starts else len(content_lines)
        section_preamble_raw = "".join(content_lines[:pre_len])

        tasks: list[Task] = []
        for k, ts in enumerate(task_starts):
            te = task_starts[k + 1] if k + 1 < len(task_starts) else len(content_lines)
            abs_line_no = content_start + ts + 1  # 1-based
            tasks.append(_parse_task_block(content_lines, ts, te, section_name, abs_line_no))

        sections.append(Section(header_raw=header_raw, name=section_name, preamble_raw=section_preamble_raw, tasks=tasks))

    parsed = ParsedFile(frontmatter_raw=frontmatter_raw, preamble_raw=preamble_raw, sections=sections)
    return parsed


# --------------------------------------------------------------------------
# Frontmatter helpers
# --------------------------------------------------------------------------
def _bump_updated(frontmatter_raw: str, today: str) -> str:
    out_lines = []
    bumped = False
    for ln in frontmatter_raw.splitlines(keepends=True):
        if re.match(r"^updated:", ln):
            out_lines.append(f"updated: {today}\n" if ln.endswith("\n") else f"updated: {today}")
            bumped = True
        else:
            out_lines.append(ln)
    if not bumped:
        raise TasksParseError("frontmatter has no 'updated:' field to bump")
    return "".join(out_lines)


# --------------------------------------------------------------------------
# docs/DONE.md — the completed-task ledger `close` moves tasks into.
# --------------------------------------------------------------------------
def _default_done_text(today: str) -> str:
    return (
        "---\n"
        "type: requirement\n"
        "state: done\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "cross-file-references: [docs/TASKS.md]\n"
        "---\n"
        "# Done\n"
        "Ledger of completed tasks, moved out of docs/TASKS.md on close. Never reuse a listed id.\n"
        "\n"
    )


def _insert_meta_bullets(raw: str, bullets: list[tuple[str, str]]) -> str:
    """Append `bullets` to the end of a task's existing "- key:: value" block."""
    lines = raw.splitlines(keepends=True)
    i = 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    while i < len(lines) and _BULLET_RE.match(lines[i]):
        i += 1
    insert = [f"- {k}:: {v}\n" for k, v in bullets]
    return "".join(lines[:i]) + "".join(insert) + "".join(lines[i:])


def _parse_single_task_raw(raw: str, section_name: str) -> Task:
    lines = raw.splitlines(keepends=True)
    return _parse_task_block(lines, 0, len(lines), section_name, -1)


# --------------------------------------------------------------------------
# span surgery — shared by `edit`. Every helper takes a task's verbatim raw
# span, changes exactly the piece it was asked to, and leaves every other
# byte of the span alone.
# --------------------------------------------------------------------------
# Canonical bullet order (matches _render_new_task). Unknown keys (note::,
# deferral-rationale:: — both live in real registries) sort after refs;
# edited:: always last so it reads as the trailing audit stamp.
_BULLET_ORDER = {"prio": 0, "module": 1, "created": 2, "blocked-by": 3, "refs": 4}
_BODY_ORDER = {"Goal": 0, "Details": 1, "Done when": 2}


def _bullet_sort_key(key: str) -> int:
    if key == "edited":
        return 90
    return _BULLET_ORDER.get(key, 50)


def _bullet_bounds(lines: list[str]) -> tuple[int, int]:
    """(start, end) line indices of the "- key:: value" block in a task span."""
    i = 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    start = i
    while i < len(lines) and _BULLET_RE.match(lines[i]):
        i += 1
    return start, i


def _set_bullet(raw: str, key: str, value: str) -> str:
    """Replace `- key:: value` in place, or insert it at its canonical position."""
    lines = raw.splitlines(keepends=True)
    b_start, b_end = _bullet_bounds(lines)
    for i in range(b_start, b_end):
        bm = _BULLET_RE.match(lines[i])
        if bm and bm.group("key") == key:
            lines[i] = f"- {key}:: {value}\n"
            return "".join(lines)
    insert_at = b_end
    for i in range(b_start, b_end):
        bm = _BULLET_RE.match(lines[i])
        if bm and _bullet_sort_key(bm.group("key")) > _bullet_sort_key(key):
            insert_at = i
            break
    return "".join(lines[:insert_at]) + f"- {key}:: {value}\n" + "".join(lines[insert_at:])


def _remove_bullet(raw: str, key: str) -> str:
    """Remove `- key:: ...`. Raises ValueError if the bullet isn't present."""
    lines = raw.splitlines(keepends=True)
    b_start, b_end = _bullet_bounds(lines)
    for i in range(b_start, b_end):
        bm = _BULLET_RE.match(lines[i])
        if bm and bm.group("key") == key:
            return "".join(lines[:i]) + "".join(lines[i + 1:])
    raise ValueError(f"no '- {key}::' bullet to clear")


def _find_body_spans(lines: list[str], start: int) -> list[tuple[str, int, int]]:
    """(label, start_idx, end_idx) per **Goal./Details./Done when.** section,
    fence-aware so a ``` block can't fake a section marker."""
    marks: list[tuple[str, int]] = []
    in_fence = False
    for idx in range(start, len(lines)):
        if _FENCE_RE.match(lines[idx]):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _BODY_MARK_RE.match(lines[idx].rstrip("\n"))
        if m:
            marks.append((m.group("label"), idx))
    spans: list[tuple[str, int, int]] = []
    for k, (label, idx) in enumerate(marks):
        end = marks[k + 1][1] if k + 1 < len(marks) else len(lines)
        spans.append((label, idx, end))
    return spans


def _render_body_section(label: str, text: str) -> str:
    """Same shapes as _render_new_task: Goal/Done when inline, Details block."""
    text = text.strip("\n")
    if label == "Details":
        return f"**Details.**\n{text}\n"
    return f"**{label}.** {text}\n"


def _trailing_blank_count(seg: list[str]) -> int:
    n = 0
    while n < len(seg) and seg[-1 - n].strip() == "":
        n += 1
    return n


def _replace_body_section(raw: str, label: str, text: str) -> str:
    """Full-replace one prose section; creates it (canonically placed) if absent.
    The section's trailing blank-line run is preserved verbatim, so the spacing
    to the next section / task / '## ' header never shifts."""
    lines = raw.splitlines(keepends=True)
    _, b_end = _bullet_bounds(lines)
    spans = _find_body_spans(lines, b_end)
    target = next((s for s in spans if s[0] == label), None)
    if target is None:
        return _insert_body_section(raw, label, text)
    _, s0, s1 = target
    nblank = _trailing_blank_count(lines[s0:s1])
    new_seg = _render_body_section(label, text) + "\n" * nblank
    return "".join(lines[:s0]) + new_seg + "".join(lines[s1:])


def _insert_body_section(raw: str, label: str, text: str) -> str:
    lines = raw.splitlines(keepends=True)
    _, b_end = _bullet_bounds(lines)
    spans = _find_body_spans(lines, b_end)
    order = _BODY_ORDER[label]
    insert_at = None
    trailing = ""
    for lab, s0, _s1 in spans:
        if _BODY_ORDER.get(lab, 99) > order:
            insert_at = s0
            trailing = "\n"  # blank line before the section that now follows
            break
    if insert_at is None:
        # append at the end of the span, before its trailing blank run (which
        # is preserved — it's the separation from the next task / header / EOF)
        insert_at = len(lines) - _trailing_blank_count(lines)
    seg = _render_body_section(label, text) + trailing
    if insert_at > 0 and lines[insert_at - 1].strip() != "":
        seg = "\n" + seg
    return "".join(lines[:insert_at]) + seg + "".join(lines[insert_at:])


def _append_body_section(raw: str, label: str, text: str) -> str:
    """Append `text` as new line(s) at the end of an existing section's content,
    leaving the section's existing bytes (and trailing blanks) untouched."""
    lines = raw.splitlines(keepends=True)
    _, b_end = _bullet_bounds(lines)
    spans = _find_body_spans(lines, b_end)
    target = next((s for s in spans if s[0] == label), None)
    if target is None:
        raise ValueError(f"task has no **{label}.** section to append to — use the replace flag instead")
    _, s0, s1 = target
    content_end = s1 - _trailing_blank_count(lines[s0:s1])
    text = text.strip("\n")
    insertion = "".join(ln + "\n" for ln in text.split("\n"))
    return "".join(lines[:content_end]) + insertion + "".join(lines[content_end:])


# --------------------------------------------------------------------------
# git history id scan — retired-id protection, source 3 of 3.
#
# `add`'s reuse-prevention historically only looked at TASKS.md (live) union
# DONE.md (closed-through-the-script). Nothing has ever been closed *through*
# the script, so DONE.md has never existed and that union was, in practice,
# just "live ids" — every id retired before this script existed (renamed,
# renumbered, or closed by hand) was silently re-issuable. STOR-15 (a live
# architectural concept referenced from docs, landed 2026-07-03) got handed
# out again as a fresh id because of exactly this gap. Git commit history is
# the one durable record of every id ever used, so it's now a mandatory third
# source.
_COMMIT_SCOPE_RE = re.compile(r"^[a-zA-Z]+\(([^()]+)\):")
_SCOPE_ID_RE = re.compile(r"^([a-zA-Z]+(?:-[a-zA-Z]+)*)-([0-9]+)")

# Renamed this session: git history still carries the old spellings, but they
# must count as used under the new prefix — otherwise the old number is
# re-issuable under the new prefix (e.g. INT-SCH-13 in history must block
# SCH-13 today).
_HISTORICAL_PREFIX_NORMALIZE = {
    "INT-CLI": "CLI",
    "INT-UI": "UI",
    "INT-SCH": "SCH",
    "INT-NOTIF": "NOTI",
    "INT-MCP": "MCP",
}


def _extract_id_from_scope(scope: str) -> str | None:
    """Pull the leading `PREFIX-NNN` id out of a commit scope slug.

    Scopes are sometimes a bare id (`fet-38`), sometimes an id with a
    trailing descriptive slug (`stor-15-ui-replica`, `int-sch-13-queue-stats`
    -> `stor-15` / `int-sch-13`), and sometimes not an id at all
    (`backtest-rebuild`) — that last shape returns None rather than raising.
    """
    m = _SCOPE_ID_RE.match(scope)
    if not m:
        return None
    prefix = _HISTORICAL_PREFIX_NORMALIZE.get(m.group(1).upper(), m.group(1).upper())
    return f"{prefix}-{m.group(2)}"


def _git_history_ids(repo_root: Path) -> tuple[set[str], str | None]:
    """Every id ever used as a commit scope across all of git history.

    Returns `(ids, warning)`. `warning` is set (and `ids` is whatever could
    still be salvaged — empty on total failure) whenever history could not be
    fully consulted: no git binary, `repo_root` isn't a repo, or any other
    failure. Never raises — a broken git call must degrade the reuse check,
    not crash the command.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%s"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return set(), f"could not read git history ({e}) — auto-assigned/explicit ids may collide with a retired one"
    if result.returncode != 0:
        return set(), "could not read git history (git log failed) — auto-assigned/explicit ids may collide with a retired one"

    ids: set[str] = set()
    for subject in result.stdout.splitlines():
        m = _COMMIT_SCOPE_RE.match(subject)
        if not m:
            continue
        extracted = _extract_id_from_scope(m.group(1))
        if extracted:
            ids.add(extracted)
    return ids, None


def _find_commits_for_id(task_id: str, repo_root: Path) -> str:
    """`git log --oneline --all --grep=<id> -i`, formatted as '<sha> <subject>, ...'.

    Matches both the `[TASK-ID] subject` and historical `action(task-id): subject`
    commit conventions (case-insensitive grep on the bare id covers both). Never
    fails the close: any git error or empty match yields '(none found)'.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--all", "--grep", task_id, "-i"],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "(none found)"
    if result.returncode != 0:
        return "(none found)"
    entries = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if not entries:
        return "(none found)"
    return ", ".join(entries)


# --------------------------------------------------------------------------
# Atomic write
# --------------------------------------------------------------------------
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load(path: Path) -> tuple[str, ParsedFile]:
    text = path.read_text(encoding="utf-8")
    parsed = parse_tasks_file(text)
    return text, parsed


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def cmd_list(parsed: ParsedFile, args: argparse.Namespace) -> int:
    prios: set[str] | None = None
    if args.prio:
        prios = set()
        for p in args.prio:
            prios.update(x.strip() for x in p.split(",") if x.strip())

    tasks = parsed.all_tasks()
    out: list[Task] = []
    for t in tasks:
        if prios is not None and (t.prio or "") not in prios:
            continue
        if args.module and (t.module or "") != args.module:
            continue
        has_blocker = bool(t.blocked_by and t.blocked_by.strip())
        if args.blocked and not has_blocker:
            continue
        if args.unblocked and has_blocker:
            continue
        out.append(t)

    if args.ids_only:
        for t in out:
            print(t.id)
        return 0

    if not out:
        return 0
    id_w = max(len(t.id) for t in out)
    prio_w = max(len(t.prio or "") for t in out)
    mod_w = max(len(t.module or "") for t in out)
    for t in out:
        print(f"{t.id.ljust(id_w)}  {(t.prio or '').ljust(prio_w)}  {(t.module or '').ljust(mod_w)}  {t.title}")
    return 0


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------
def cmd_show(parsed: ParsedFile, parsed_done: ParsedFile, args: argparse.Namespace) -> int:
    by_id = parsed.tasks_by_id()
    done_by_id = parsed_done.tasks_by_id()
    rc = 0
    for i, task_id in enumerate(args.id):
        t = by_id.get(task_id)
        closed = False
        if t is None:
            t = done_by_id.get(task_id)
            closed = t is not None
        if t is None:
            print(f"unknown id: {task_id} (not in TASKS.md or DONE.md)", file=sys.stderr)
            rc = 1
            continue
        if i:
            print()
        if closed:
            print("[CLOSED — from DONE.md]")
        sys.stdout.write(t.raw if t.raw.endswith("\n") else t.raw + "\n")
    return rc


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
_FIELD_ORDER = ("title", "goal", "details", "done")


def _field_text(t: Task, field_name: str) -> str:
    if field_name == "title":
        return t.title
    if field_name == "goal":
        return t.goal
    if field_name == "details":
        return t.details or ""
    if field_name == "done":
        return t.done_when
    raise ValueError(field_name)


def _snippet(text: str, needle: str, width: int = 30) -> str:
    flat = " ".join(text.split())
    idx = flat.lower().find(needle.lower())
    if idx < 0:
        return flat[: width * 2]
    start = max(0, idx - width)
    end = min(len(flat), idx + len(needle) + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def cmd_search(parsed: ParsedFile, parsed_done: ParsedFile, args: argparse.Namespace) -> int:
    needle = args.text
    fields = _FIELD_ORDER if args.field == "all" else (args.field,)
    hits: list[tuple[Task, str, str, bool]] = []  # (task, field, snippet, closed)
    for t in parsed.all_tasks():
        for f in fields:
            text = _field_text(t, f)
            if needle.lower() in text.lower():
                hits.append((t, f, _snippet(text, needle), False))
                break  # one hit per task, first matching field in priority order
    if args.include_done:
        for t in parsed_done.all_tasks():
            for f in fields:
                text = _field_text(t, f)
                if needle.lower() in text.lower():
                    hits.append((t, f, _snippet(text, needle), True))
                    break

    if args.ids_only:
        for t, _, _, _ in hits:
            print(t.id)
        return 0

    for t, f, snip, closed in hits:
        tag = "  [CLOSED]" if closed else ""
        print(f"{t.id}  {t.title}{tag}")
        print(f"    {f}: {snip}")
    return 0


# --------------------------------------------------------------------------
# blockers / blocking
# --------------------------------------------------------------------------
def _split_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def cmd_blockers(parsed: ParsedFile, args: argparse.Namespace) -> int:
    by_id = parsed.tasks_by_id()
    t = by_id.get(args.id)
    if t is None:
        print(f"unknown id: {args.id}", file=sys.stderr)
        return 1
    for token in _split_ids(t.blocked_by):
        blocker = by_id.get(token)
        if blocker is not None:
            print(f"{blocker.id}  {blocker.title}")
    return 0


def cmd_blocking(parsed: ParsedFile, args: argparse.Namespace) -> int:
    by_id = parsed.tasks_by_id()
    if args.id not in by_id:
        print(f"unknown id: {args.id}", file=sys.stderr)
        return 1
    for t in parsed.all_tasks():
        if args.id in _split_ids(t.blocked_by):
            print(f"{t.id}  {t.title}")
    return 0


# --------------------------------------------------------------------------
# refs
# --------------------------------------------------------------------------
def cmd_refs(parsed: ParsedFile, args: argparse.Namespace) -> int:
    if args.referencing:
        needle = args.referencing.lower()
        for t in parsed.all_tasks():
            if t.refs and needle in t.refs.lower():
                print(f"{t.id}  {t.title}")
        return 0

    if not args.id:
        print("refs: need an ID or --referencing PATH", file=sys.stderr)
        return 2
    by_id = parsed.tasks_by_id()
    t = by_id.get(args.id)
    if t is None:
        print(f"unknown id: {args.id}", file=sys.stderr)
        return 1
    for ref in _split_ids(t.refs):
        print(ref)
    return 0


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------
def _load_project_module_map(repo_root: Path | None) -> dict[str, tuple[str, str]]:
    """Load `<repo_root>/docs/.ctask`'s "modules" map, or {} if `repo_root`
    is None or the file doesn't exist.

    Config shape: `{"modules": {"cr": {"prefix": "CR", "section": "..."}}}`.
    Malformed config (bad JSON, missing "modules", a bad entry) raises
    ValueError naming the file path and what's wrong — never silently
    ignored or partially applied.
    """
    if repo_root is None:
        return {}
    config_path = repo_root / "docs" / ".ctask"
    if not config_path.exists():
        return {}
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"{config_path}: could not read file ({e})") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{config_path}: invalid JSON ({e})") from e
    if not isinstance(data, dict) or "modules" not in data:
        raise ValueError(f"{config_path}: missing required 'modules' key")
    modules = data["modules"]
    if not isinstance(modules, dict):
        raise ValueError(f"{config_path}: 'modules' must be a dict")
    out: dict[str, tuple[str, str]] = {}
    for key, entry in modules.items():
        if not isinstance(entry, dict) or "prefix" not in entry or "section" not in entry:
            raise ValueError(f"{config_path}: module {key!r} entry must have 'prefix' and 'section'")
        prefix = entry["prefix"]
        section = entry["section"]
        if not isinstance(prefix, str) or not _ID_PREFIX_RE.match(prefix):
            raise ValueError(f"{config_path}: module {key!r} prefix {prefix!r} must be 2-4 uppercase letters")
        if not isinstance(section, str) or not section.strip():
            raise ValueError(f"{config_path}: module {key!r} section must be a non-empty string")
        out[key] = (prefix, section)
    return out


def _lookup_module_mapping(module: str, repo_root: Path | None = None) -> tuple[str, str]:
    """(id-prefix, default "## <section>" name) for a --module value.

    Longest dotted-segment match wins: "interfaces.cli" -> ("CLI",
    "interfaces — cli") (specific submodule mapping beats nothing);
    "analysis.signals" -> ("ANL", "analysis") (no submodule-specific mapping,
    falls back to the top-level "analysis" entry).

    Single lookup backs both `add`'s id-prefix inference and its default
    section placement — they must never be two maps that can drift apart.

    `repo_root`, when given, layers `<repo_root>/docs/.ctask` (project
    config) over the builtin `_MODULE_PREFIX_MAP` — project config wins on
    key collision, builtin is the fallback/default. `repo_root=None`
    (default) preserves exactly the builtin-only behavior.
    """
    project_map = _load_project_module_map(repo_root)
    combined = {**_MODULE_PREFIX_MAP, **project_map}
    segments = module.split(".")
    for k in range(len(segments), 0, -1):
        candidate = ".".join(segments[:k])
        if candidate in combined:
            return combined[candidate]
    known = ", ".join(f"{m} -> {p}" for m, (p, _s) in sorted(combined.items()))
    raise ValueError(f"--module {module!r} doesn't map to a known id prefix; known mappings: {known}")


def _infer_prefix_from_module(module: str, repo_root: Path | None = None) -> str:
    return _lookup_module_mapping(module, repo_root)[0]


def _infer_section_from_module(module: str, repo_root: Path | None = None) -> str:
    return _lookup_module_mapping(module, repo_root)[1]


def _next_free_id(prefix: str, used_ids: set[str]) -> str:
    """Auto-assign PREFIX-NN, the next free numeric suffix for `prefix`.

    Every live id conforms to `_ID_PATTERN` (the parser refuses to parse
    anything that doesn't), so every `prefix + "-"` id in `used_ids` is
    guaranteed to end in a plain number — no non-conforming shape to guard
    against.
    """
    numeric_pat = re.compile(rf"^{re.escape(prefix)}-([0-9]+)$")
    conforming = [m.group(1) for tid in used_ids if (m := numeric_pat.match(tid))]

    if not conforming:
        return f"{prefix}-01"

    width = max(2, max(len(n) for n in conforming))
    next_num = max(int(n) for n in conforming) + 1
    return f"{prefix}-{next_num:0{width}d}"


def _read_file_arg(path_str: str | None) -> str | None:
    if path_str is None:
        return None
    return Path(path_str).read_text(encoding="utf-8").strip()


def _render_new_task(
    task_id: str, title: str, prio: str, module: str, created: str,
    blocked_by: str | None, refs: str | None,
    goal: str | None, details: str | None, done_when: str | None,
) -> str:
    lines = [f"### {task_id} — {title}\n", "\n"]
    lines.append(f"- prio:: {prio}\n")
    lines.append(f"- module:: {module}\n")
    lines.append(f"- created:: {created}\n")
    if blocked_by:
        lines.append(f"- blocked-by:: {blocked_by}\n")
    if refs:
        lines.append(f"- refs:: {refs}\n")
    lines.append("\n")
    if goal:
        lines.append(f"**Goal.** {goal}\n")
        lines.append("\n")
    if details:
        lines.append("**Details.**\n")
        lines.append(f"{details}\n")
        lines.append("\n")
    if done_when:
        lines.append(f"**Done when.** {done_when}\n")
        lines.append("\n")
    return "".join(lines)


def cmd_add(
    parsed: ParsedFile, done_ids: set[str], history_ids: set[str],
    args: argparse.Namespace, tasks_path: Path, repo_root: Path | None = None,
) -> int:
    by_id = parsed.tasks_by_id()
    live_ids = set(by_id.keys())
    retired_ids = done_ids | history_ids  # never re-issuable: closed-through-script + ever used in git history

    if args.id and args.id_prefix:
        print("add: pass either --id or --id-prefix, not both", file=sys.stderr)
        return 2

    if args.id:
        if not _ID_RE.match(args.id):
            print(f"add: --id {args.id!r} is not a valid task id (expect e.g. FEAT-15)", file=sys.stderr)
            return 2
        if args.id in live_ids:
            print(f"add: refuses to reuse live id {args.id}", file=sys.stderr)
            return 1
        if args.id in done_ids:
            print(f"add: refuses to reuse closed id {args.id} (see docs/DONE.md)", file=sys.stderr)
            return 1
        if args.id in history_ids:
            print(f"add: refuses to reuse retired id {args.id} (found in git commit history)", file=sys.stderr)
            return 1
        task_id = args.id
    else:
        if args.id_prefix:
            prefix = args.id_prefix
            if not _ID_PREFIX_RE.match(prefix):
                print(f"add: --id-prefix {prefix!r} must be 2-4 uppercase letters (e.g. FEAT, SCH)", file=sys.stderr)
                return 2
        else:
            try:
                prefix = _infer_prefix_from_module(args.module, repo_root)
            except ValueError as e:
                print(f"add: {e}", file=sys.stderr)
                return 1
        try:
            task_id = _next_free_id(prefix, live_ids | retired_ids)
        except ValueError as e:
            print(f"add: {e}", file=sys.stderr)
            return 1
        if task_id in live_ids or task_id in retired_ids:
            print(f"add: auto-assigned id {task_id} collides — inconsistent id sequence", file=sys.stderr)
            return 1

    if args.prio not in _VALID_PRIOS:
        print(f"add: --prio must be one of {sorted(_VALID_PRIOS)}", file=sys.stderr)
        return 2

    if args.section:
        section_name = args.section
    else:
        try:
            section_name = _infer_section_from_module(args.module, repo_root)
        except ValueError as e:
            print(f"add: {e}", file=sys.stderr)
            return 1

    section = next((s for s in parsed.sections if s.name == section_name), None)
    if section is None:
        available = ", ".join(s.name for s in parsed.sections)
        print(f"add: no '## {section_name}' section in {tasks_path} (available: {available})", file=sys.stderr)
        return 1

    today = _dt.date.today().isoformat()

    if args.stdin:
        body_block = sys.stdin.read()
        if not body_block.endswith("\n"):
            body_block += "\n"
        header = [f"### {task_id} — {args.title}\n", "\n"]
        meta = [f"- prio:: {args.prio}\n", f"- module:: {args.module}\n", f"- created:: {today}\n"]
        if args.blocked_by:
            meta.append(f"- blocked-by:: {args.blocked_by}\n")
        if args.refs:
            meta.append(f"- refs:: {args.refs}\n")
        new_raw = "".join(header) + "".join(meta) + "\n" + body_block
        if not new_raw.endswith("\n\n"):
            new_raw += "\n"
    else:
        goal = args.goal or _read_file_arg(args.goal_file)
        details = args.details or _read_file_arg(args.details_file)
        done_when = args.done_when or _read_file_arg(args.done_when_file)
        new_raw = _render_new_task(
            task_id, args.title, args.prio, args.module, today,
            args.blocked_by, args.refs, goal, details, done_when,
        )

    new_task = _parse_single_task_raw(new_raw, section_name)

    bare = _scan_bare_raw_html(new_task.raw)
    if bare:
        offset, token = bare[0]
        print(
            f"add: refusing to write — {token!r} renders as raw HTML in CommonMark/Obsidian "
            f"and vanishes; backtick-quote it (`{token}`)",
            file=sys.stderr,
        )
        return 1

    section.tasks.append(new_task)

    new_text = parsed.render()
    _atomic_write(tasks_path, new_text)
    print(task_id)
    return 0


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------
def cmd_edit(parsed: ParsedFile, args: argparse.Namespace, tasks_path: Path, repo_root: Path | None = None) -> int:
    by_id = parsed.tasks_by_id()
    t = by_id.get(args.id)
    if t is None:
        print(f"unknown id: {args.id}", file=sys.stderr)
        return 1

    requested = any(
        v is not None and v is not False
        for v in (
            args.title, args.prio, args.module, args.section,
            args.blocked_by, args.clear_blocked_by, args.refs, args.clear_refs,
            args.goal, args.goal_file, args.append_goal,
            args.details, args.details_file, args.append_details,
            args.done_when, args.done_when_file, args.append_done_when,
        )
    )
    if not requested:
        print("edit: nothing to change — pass at least one edit flag", file=sys.stderr)
        return 2

    if args.prio is not None and args.prio not in _VALID_PRIOS:
        print(f"edit: --prio must be one of {sorted(_VALID_PRIOS)}", file=sys.stderr)
        return 2
    if args.blocked_by is not None and not re.match(_ID_PATTERN, args.blocked_by.strip()):
        print(
            f"edit: --blocked-by {args.blocked_by!r} must start with a task id (e.g. FEAT-15, "
            "optionally followed by more ids and/or a parenthetical note)",
            file=sys.stderr,
        )
        return 2

    # resolve the target section BEFORE mutating anything
    target_section_name: str | None = None
    if args.section:
        target_section_name = args.section
    elif args.module:
        try:
            target_section_name = _infer_section_from_module(args.module, repo_root)
        except ValueError as e:
            print(f"edit: {e} (or pass --section explicitly)", file=sys.stderr)
            return 1
    target_section: Section | None = None
    if target_section_name is not None:
        target_section = next((s for s in parsed.sections if s.name == target_section_name), None)
        if target_section is None:
            available = ", ".join(s.name for s in parsed.sections)
            print(f"edit: no '## {target_section_name}' section in {tasks_path} (available: {available})", file=sys.stderr)
            return 1

    other_raws = {tid: task.raw for tid, task in by_id.items() if tid != t.id}
    today = _dt.date.today().isoformat()
    new_raw = t.raw

    try:
        if args.title is not None:
            lines = new_raw.splitlines(keepends=True)
            nl = "\n" if lines[0].endswith("\n") else ""
            lines[0] = f"### {t.id} — {args.title}{nl}"
            new_raw = "".join(lines)
        if args.prio is not None:
            new_raw = _set_bullet(new_raw, "prio", args.prio)
        if args.module is not None:
            new_raw = _set_bullet(new_raw, "module", args.module)
        if args.blocked_by is not None:
            new_raw = _set_bullet(new_raw, "blocked-by", args.blocked_by)
        if args.clear_blocked_by:
            new_raw = _remove_bullet(new_raw, "blocked-by")
        if args.refs is not None:
            new_raw = _set_bullet(new_raw, "refs", args.refs)
        if args.clear_refs:
            new_raw = _remove_bullet(new_raw, "refs")

        goal = args.goal if args.goal is not None else _read_file_arg(args.goal_file)
        details = args.details if args.details is not None else _read_file_arg(args.details_file)
        done_when = args.done_when if args.done_when is not None else _read_file_arg(args.done_when_file)
        if goal is not None:
            new_raw = _replace_body_section(new_raw, "Goal", goal)
        if details is not None:
            new_raw = _replace_body_section(new_raw, "Details", details)
        if done_when is not None:
            new_raw = _replace_body_section(new_raw, "Done when", done_when)
        if args.append_goal is not None:
            new_raw = _append_body_section(new_raw, "Goal", args.append_goal)
        if args.append_details is not None:
            new_raw = _append_body_section(new_raw, "Details", args.append_details)
        if args.append_done_when is not None:
            new_raw = _append_body_section(new_raw, "Done when", args.append_done_when)
    except (ValueError, OSError) as e:
        print(f"edit: {e}", file=sys.stderr)
        return 1

    # audit stamp — docs/ is a gitignored vault symlink, so without this a
    # silent in-place edit would be genuinely untraceable
    new_raw = _set_bullet(new_raw, "edited", today)

    # the edited span must itself still parse, under the same id
    section_name = target_section_name or t.section
    try:
        edited = _parse_single_task_raw(new_raw, section_name)
    except TasksParseError as e:
        print(f"edit: refusing to write — edited task no longer parses: {e}", file=sys.stderr)
        return 1
    if edited.id != t.id:
        print(f"edit: refusing to write — edited task id changed ({t.id} -> {edited.id})", file=sys.stderr)
        return 1

    bare = _scan_bare_raw_html(edited.raw)
    if bare:
        offset, token = bare[0]
        print(
            f"edit: refusing to write — {token!r} renders as raw HTML in CommonMark/Obsidian "
            f"and vanishes; backtick-quote it (`{token}`)",
            file=sys.stderr,
        )
        return 1

    moving = target_section is not None and target_section_name != t.section
    if moving:
        for s in parsed.sections:
            if t in s.tasks:
                s.tasks.remove(t)
                break
        if not edited.raw.endswith("\n\n"):  # keep a blank line before whatever follows
            edited.raw += "\n" if edited.raw.endswith("\n") else "\n\n"
        target_section.tasks.append(edited)
    else:
        for s in parsed.sections:
            for i, task in enumerate(s.tasks):
                if task is t:
                    s.tasks[i] = edited
    parsed.frontmatter_raw = _bump_updated(parsed.frontmatter_raw, today)

    # final whole-file guard: must re-parse, must round-trip, and every OTHER
    # task's bytes must be exactly what they were — else write nothing
    new_text = parsed.render()
    try:
        reparsed = parse_tasks_file(new_text)
    except TasksParseError as e:
        print(f"edit: refusing to write — edited file no longer parses: {e}", file=sys.stderr)
        return 1
    if reparsed.render() != new_text:
        print("edit: refusing to write — edited file does not round-trip", file=sys.stderr)
        return 1
    re_raws = {task.id: task.raw for task in reparsed.all_tasks()}
    for tid, raw in other_raws.items():
        if re_raws.get(tid) != raw:
            print(f"edit: refusing to write — would alter untouched task {tid}", file=sys.stderr)
            return 1

    _atomic_write(tasks_path, new_text)
    print(t.id)
    return 0


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------
def cmd_close(
    parsed: ParsedFile, parsed_done: ParsedFile, args: argparse.Namespace,
    tasks_path: Path, done_path: Path, repo_root: Path,
) -> int:
    by_id = parsed.tasks_by_id()
    t = by_id.get(args.id)
    if t is None:
        print(f"unknown id: {args.id}", file=sys.stderr)
        return 1

    for s in parsed.sections:
        if t in s.tasks:
            s.tasks.remove(t)
            break

    today = _dt.date.today().isoformat()
    commits = _find_commits_for_id(t.id, repo_root)
    new_raw = _insert_meta_bullets(t.raw, [("closed", today), ("commits", commits)])
    moved_task = _parse_single_task_raw(new_raw, t.section)

    done_section = next((s for s in parsed_done.sections if s.name == t.section), None)
    if done_section is None:
        done_section = Section(header_raw=f"## {t.section}\n", name=t.section, preamble_raw="\n", tasks=[])
        parsed_done.sections.append(done_section)
    done_section.tasks.append(moved_task)

    parsed.frontmatter_raw = _bump_updated(parsed.frontmatter_raw, today)
    parsed_done.frontmatter_raw = _bump_updated(parsed_done.frontmatter_raw, today)

    new_tasks_text = parsed.render()
    new_done_text = parsed_done.render()

    _atomic_write(tasks_path, new_tasks_text)
    _atomic_write(done_path, new_done_text)

    print(t.id)
    for other in parsed.all_tasks():
        if t.id in _split_ids(other.blocked_by):
            print(f"STALE  {other.id}  {other.title}")
    return 0


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
# CommonMark §6.8: a bare `<word>` (letter, then letters/digits/hyphens) is a
# valid open tag ON ITS OWN — no attributes, no closing tag needed — so any
# CommonMark renderer (Obsidian included) eats it as raw inline HTML and it
# vanishes from the rendered output. The convention (write-docs skill,
# "Placeholders in prose") is to backtick-quote such placeholders.
#
# `_scan_bare_raw_html`/`_RAW_HTML_RE` are also used by `add`/`edit` (above,
# despite the later definition here — plain top-level name resolution, fine
# since the whole module loads before any command dispatches) to REFUSE to
# write new/edited content containing this pattern, rather than merely warn
# after the fact. `validate` stays a warning (not a hard error) here because
# it scans the WHOLE file, including legacy content written before this
# guard existed — a hard error would brick validate for every registry that
# predates the rule. Matches
# open, close, and self-closing shapes; autolinks (`<https://…>`) contain
# characters a tag name can't, so they never match.
_RAW_HTML_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*\s*/?>")
# Inline code spans: a backtick run, content, then an identical run. Good
# enough to strip protected spans before the raw-HTML scan.
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")


def _scan_bare_raw_html(raw: str) -> list[tuple[int, str]]:
    """(line_offset, token) for each bare tag-shaped `<word>` in a task span,
    ignoring anything inside a fenced block or an inline code span."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for off, line in enumerate(raw.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _RAW_HTML_RE.finditer(_CODE_SPAN_RE.sub("", line)):
            out.append((off, m.group(0)))
    return out


def _valid_iso_date(value: str) -> bool:
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def cmd_validate(parsed: ParsedFile, done_ids: set[str], tasks_path: Path) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    seen: dict[str, int] = {}
    for t in parsed.all_tasks():
        if t.id in seen:
            problems.append(f"{tasks_path}:{t.header_line}: duplicate id {t.id} (first seen line {seen[t.id]})")
        else:
            seen[t.id] = t.header_line

    all_ids = set(seen.keys())

    for t in parsed.all_tasks():
        for required in ("prio", "module", "created"):
            if not t.meta.get(required):
                problems.append(f"{tasks_path}:{t.header_line}: {t.id} missing required field '{required}'")
        if t.prio and t.prio not in _VALID_PRIOS:
            problems.append(f"{tasks_path}:{t.header_line}: {t.id} invalid prio {t.prio!r} (expect one of {sorted(_VALID_PRIOS)})")
        for key in ("created", "edited"):
            value = t.meta.get(key)
            if value and not _valid_iso_date(value):
                problems.append(f"{tasks_path}:{t.header_line}: {t.id} {key}:: {value!r} is not a valid ISO date (YYYY-MM-DD)")
        # blocked-by legitimately carries parenthetical annotations after the
        # id(s) ("FEAT-18 (FEAT-18 deletes …)"), so only the leading token is
        # held to the id shape — a value not starting with an id can never be
        # resolved by blockers/blocking and is structurally broken.
        if t.blocked_by is not None:
            bb = t.blocked_by.strip()
            if not bb or not re.match(_ID_PATTERN, bb):
                problems.append(f"{tasks_path}:{t.header_line}: {t.id} malformed blocked-by {t.blocked_by!r} (must start with a task id)")
        for token in _split_ids(t.blocked_by):
            if _ID_RE.match(token) and token not in all_ids and token not in done_ids:
                problems.append(f"{tasks_path}:{t.header_line}: {t.id} blocked-by dangling reference {token!r}")
        # refs is free-form (paths with annotations, '§' section anchors) —
        # only an empty value / empty comma-token is flagged.
        if t.refs is not None:
            if not t.refs.strip() or any(not tok.strip() for tok in t.refs.split(",")):
                problems.append(f"{tasks_path}:{t.header_line}: {t.id} refs has an empty entry: {t.refs!r}")
        if not t.goal.strip():
            problems.append(f"{tasks_path}:{t.header_line}: {t.id} has an empty or missing **Goal.** section")
        # warning, not error: real registries predate the rule (4 tasks in
        # repit lack Done when today) — a hard error would brick validate for
        # every existing file at once.
        if not t.done_when.strip():
            warnings.append(f"{tasks_path}:{t.header_line}: {t.id} has no **Done when.** section")
        # warning, not error, for the same reason: live registries already
        # contain bare placeholders, and validate must stay usable while
        # they're cleaned up.
        for offset, token in _scan_bare_raw_html(t.raw):
            warnings.append(
                f"{tasks_path}:{t.header_line + offset}: {t.id} bare {token!r} renders as raw HTML "
                f"in CommonMark/Obsidian and vanishes — backtick-quote it (`{token}`)"
            )
        if t.id in done_ids:
            problems.append(f"{tasks_path}:{t.header_line}: {t.id} is also present in DONE.md")
        # Fence check is scoped to task bodies only — fenced code in the preamble
        # (e.g. a usage example under "## How to use this file") is legitimate;
        # a fence INSIDE a task span is the actual migration debt (v1 wrapped
        # every task in a ``` block).
        for offset, ln in enumerate(t.raw.splitlines()):
            if _FENCE_RE.match(ln):
                problems.append(f"{tasks_path}:{t.header_line + offset}: {t.id} leftover code fence")

    for p in problems:
        print(p)
    for w in warnings:
        print(f"warning: {w}")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# repo / registry discovery
# --------------------------------------------------------------------------
def _find_repo_root(start: Path) -> Path | None:
    """`git rev-parse --show-toplevel` run from `start`; None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctask", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default=None, help="path to TASKS.md (default: <git repo root>/docs/TASKS.md)")
    p.add_argument("--done-file", default=None, help="path to the completed-task ledger (default: <TASKS.md's dir>/DONE.md)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list")
    sp.add_argument("--prio", action="append")
    sp.add_argument("--module")
    sp.add_argument("--blocked", action="store_true")
    sp.add_argument("--unblocked", action="store_true")
    sp.add_argument("--ids-only", action="store_true")

    sp = sub.add_parser("show")
    sp.add_argument("id", nargs="+")

    sp = sub.add_parser("search")
    sp.add_argument("text")
    sp.add_argument("--field", choices=("goal", "details", "done", "title", "all"), default="all")
    sp.add_argument("--ids-only", action="store_true")
    sp.add_argument("--include-done", action="store_true", help="also search docs/DONE.md; archived hits are labelled [CLOSED]")

    sp = sub.add_parser("blockers")
    sp.add_argument("id")

    sp = sub.add_parser("blocking")
    sp.add_argument("id")

    sp = sub.add_parser("refs")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--referencing")

    sp = sub.add_parser("add")
    sp.add_argument("--id")
    sp.add_argument("--id-prefix")
    sp.add_argument("--title", required=True)
    sp.add_argument("--prio", required=True)
    sp.add_argument("--module", required=True)
    sp.add_argument("--section", help="'## <name>' section to place the task under (default: inferred from --module)")
    sp.add_argument("--blocked-by")
    sp.add_argument("--refs")
    sp.add_argument("--goal")
    sp.add_argument("--details")
    sp.add_argument("--done-when")
    sp.add_argument("--goal-file")
    sp.add_argument("--details-file")
    sp.add_argument("--done-when-file")
    sp.add_argument("--stdin", action="store_true", help="read the full body block (metadata extras aside) verbatim from stdin")

    sp = sub.add_parser(
        "edit",
        description=(
            "Edit one open task in place. Only the pieces named by flags are rewritten — "
            "every other byte of the task (and of every other task) is preserved verbatim. "
            "Prose sections support full-replace (--goal/--details/--done-when, or *-file) "
            "and append (--append-*: adds new line(s) at the end of an existing section, "
            "the safe mode for clarifying notes since the existing text is never re-rendered). "
            "--module/--section move the task's span under the matching '## ' header. "
            "Every successful edit stamps '- edited:: <today>' — docs/ is typically a "
            "gitignored vault symlink, so this is the only audit trail an in-place edit gets. "
            "If anything cannot be applied safely, nothing is written."
        ),
    )
    sp.add_argument("id")
    sp.add_argument("--title")
    sp.add_argument("--prio")
    sp.add_argument("--module", help="new module:: value; also moves the task under the module's '## ' section unless --section is given")
    sp.add_argument("--section", help="'## <name>' section to move the task under")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--blocked-by")
    g.add_argument("--clear-blocked-by", action="store_true")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--refs")
    g.add_argument("--clear-refs", action="store_true")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--goal")
    g.add_argument("--goal-file")
    g.add_argument("--append-goal")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--details")
    g.add_argument("--details-file")
    g.add_argument("--append-details")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--done-when")
    g.add_argument("--done-when-file")
    g.add_argument("--append-done-when")

    sp = sub.add_parser("close")
    sp.add_argument("id")

    sub.add_parser("validate")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.file:
        tasks_path = Path(args.file)
    else:
        repo_root = _find_repo_root(Path.cwd())
        if repo_root is None:
            print(
                f"tasks.py: not inside a git repository (cwd={Path.cwd()}); pass --file to override",
                file=sys.stderr,
            )
            return 1
        tasks_path = repo_root / "docs" / "TASKS.md"

    if not tasks_path.exists():
        print(f"tasks file not found: {tasks_path}", file=sys.stderr)
        return 1

    if args.done_file:
        done_path = Path(args.done_file)
    else:
        done_path = tasks_path.parent / "DONE.md"

    try:
        _, parsed = _load(tasks_path)
    except TasksParseError as e:
        print(f"parse error in {tasks_path}: {e}", file=sys.stderr)
        return 1

    today = _dt.date.today().isoformat()
    if done_path.exists():
        try:
            _, parsed_done = _load(done_path)
        except TasksParseError as e:
            print(f"parse error in {done_path}: {e}", file=sys.stderr)
            return 1
    else:
        parsed_done = parse_tasks_file(_default_done_text(today))

    done_ids = set(parsed_done.tasks_by_id().keys())

    if args.cmd == "list":
        return cmd_list(parsed, args)
    if args.cmd == "show":
        return cmd_show(parsed, parsed_done, args)
    if args.cmd == "search":
        return cmd_search(parsed, parsed_done, args)
    if args.cmd == "blockers":
        return cmd_blockers(parsed, args)
    if args.cmd == "blocking":
        return cmd_blocking(parsed, args)
    if args.cmd == "refs":
        return cmd_refs(parsed, args)
    # tasks_path is always <repo root>/docs/TASKS.md, whether resolved via
    # git or via --file pointing at a like-shaped fixture — two parents up
    # is the repo root either way.
    repo_root_for_git = tasks_path.parent.parent

    if args.cmd == "add":
        # One git-log call per `add` invocation (not per candidate id) —
        # both the explicit --id check and the auto-assign path below reuse
        # this same set.
        history_ids, history_warning = _git_history_ids(repo_root_for_git)
        if history_warning:
            print(f"tasks.py: {history_warning}", file=sys.stderr)
        return cmd_add(parsed, done_ids, history_ids, args, tasks_path, repo_root_for_git)
    if args.cmd == "edit":
        return cmd_edit(parsed, args, tasks_path, repo_root_for_git)
    if args.cmd == "close":
        return cmd_close(parsed, parsed_done, args, tasks_path, done_path, repo_root_for_git)
    if args.cmd == "validate":
        return cmd_validate(parsed, done_ids, tasks_path)

    raise AssertionError(f"unhandled subcommand {args.cmd!r}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(1)
