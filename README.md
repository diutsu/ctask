# ctask

A zero-dependency CLI over a markdown task registry: `<git-root>/docs/tasks.md` (open tasks) and `<git-root>/docs/done.md` (completed-task ledger), with automatic fallback to legacy uppercase `docs/TASKS.md` and `docs/DONE.md`.

Lets an agent or human query and mutate the task backlog — what is blocking `FEAT-24`, which tasks touch a given file — without loading the entire markdown file into context or risking hand-edit corruption.

The registry is resolved via `git rev-parse --show-toplevel` from the current working directory (`--file` and `--done-file` override this). Runs natively on **Linux, macOS, and Windows**.

---

## Install & Run (`uvx` / `uv`)

Zero runtime dependencies (Python $\ge 3.9$ stdlib only):

```bash
# 1. Run directly from Git via uvx (no install required)
uvx --from git+https://github.com/diutsu/ctask ctask list

# 2. Install globally onto PATH (Windows, macOS, Linux)
uv tool install git+https://github.com/diutsu/ctask
ctask list

# 3. Run or install from a local clone
uvx --from /path/to/ctask ctask validate
uv tool install /path/to/ctask

# 4. Editable pip install or direct symlink (POSIX)
pip install -e .
ln -s /path/to/ctask/ctask.py ~/.local/bin/ctask
```

---

## Subcommands

- `ctask list [--prio P] [--module M] [--blocked] [--unblocked] [--ids-only]` — list open tasks, optionally filtered.
- `ctask show ID [ID ...]` — print one or more tasks verbatim (falls back to `done.md` / `DONE.md`, marked `[CLOSED — from DONE.md]`).
- `ctask search TEXT [--field goal|details|done|title|all] [--ids-only] [--include-done]` — find tasks whose text matches, with a snippet.
- `ctask blockers ID` — list the tasks that block `ID`.
- `ctask blocking ID` — list the tasks that `ID` blocks.
- `ctask refs [--referencing PATH] [ID]` — show a task's `refs::`, or which tasks reference `PATH` (normalizes `/` and `\` on Windows).
- `ctask add --title T --prio P --module M [--id ID | --id-prefix PREFIX] [--section S] [--blocked-by X] [--refs R] [--goal G] [--details D] [--done-when W]` — append a new task (ID auto-assigned from `tasks.md` $\cup$ `done.md` $\cup$ `git log` history).
- `ctask edit ID [--title T] [--prio P] [--module M] [--section S] [--blocked-by X | --clear-blocked-by] [--refs R | --clear-refs] [--goal G | --append-goal T] [--details D | --append-details T] [--done-when W | --append-done-when T]` — rewrite only the named pieces of one task in place and stamp `edited:: YYYY-MM-DD`.
- `ctask close ID` — move a task's span from `tasks.md` into `done.md` (stamping `completed::` and `commits::`).
- `ctask renumber OLD_ID NEW_ID` — rename an open task's ID in place and rewrite every other open task's `blocked-by::` reference to it.
- `ctask add-section "<name>" --after "<existing-section-name>"` — create a new empty `## <name>` section.
- `ctask remove-section "<name>"` — delete an empty `## <name>` section header (refuses if it still has tasks).
- `ctask validate` — check structural invariants (required fields, ISO dates, dangling `blocked-by`, duplicate IDs, bare `<placeholder>` HTML hazards, and code fences).

Run `ctask <subcommand> --help` for full flag lists.

---

## Tests

```bash
# Linux / macOS / WSL
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -q

# Windows (PowerShell)
python -m pytest tests/test_ctask.py -q
```

---

## Registry File Format (`docs/tasks.md` & `docs/done.md`)

Each open task is an H3 span under an H2 module-section header:

```markdown
## <section>

### <ID> — <title>

- prio:: high
- module:: interfaces.scheduler
- created:: 2026-07-15
- blocked-by:: SCH-12
- refs:: docs/requirements/scheduler.md, src/storage/db/replica.py

**Goal.** ...

**Details.**
...

**Done when.** ...
```

- **Metadata bullets (`- key:: value`):** `prio::`, `module::`, and `created::` are required; `blocked-by::` and `refs::` are optional; unknown extra keys are preserved.
- **Prose sections:** `**Goal.**`, `**Details.**` (optional), and `**Done when.**`.
- **Per-project module map (`docs/.ctask`):** Optional JSON file `{"modules": {"<mod>": {"prefix": "<PFX>", "section": "<section-name>"}}}` that extends/overrides the built-in module-to-prefix map for that repository.
- **Byte-identical round-tripping:** Every untouched task span is preserved verbatim with LF (`\n`) line endings across all platforms.