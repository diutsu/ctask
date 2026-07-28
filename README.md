# ctask

A CLI over a markdown task registry: `<git-root>/docs/TASKS.md` (open tasks)
and `<git-root>/docs/DONE.md` (completed-task ledger). Lets you ask precise
questions — what's blocking FEAT-24, which tasks touch a given file — without
loading the whole file into context, and edit the registry without hand-editing
markdown.

The registry is resolved via `git rev-parse --show-toplevel` from the current
directory, not from the script's own location — `--file` / `--done-file`
override this.

## Subcommands

- `list [--prio P] [--module M] [--blocked] [--unblocked] [--ids-only]` — list open tasks, optionally filtered.
- `show ID [ID ...]` — print one or more tasks verbatim (falls back to DONE.md, marked `[CLOSED — from DONE.md]`).
- `search TEXT [--field goal|details|done|title|all] [--ids-only] [--include-done]` — find tasks whose text matches, with a snippet.
- `blockers ID` — list the tasks that block ID.
- `blocking ID` — list the tasks that ID blocks.
- `refs [--referencing PATH] [ID]` — show a task's refs, or which tasks reference a path.
- `add --title T --prio P --module M ...` — append a new task (id auto-assigned or explicit).
- `edit ID [--title T] [--prio P] [--module M] ...` — rewrite only the named pieces of one task in place.
- `close ID` — move a task's span from TASKS.md into DONE.md.
- `validate` — check structural invariants (required fields, dangling `blocked-by`, duplicate ids, etc.).

Run `ctask.py <subcommand> --help` for full flag lists.

## Install

Zero runtime dependencies — the script is stdlib-only, so the primary route
is just putting it on `PATH`:

```
ln -s /path/to/ctask/ctask.py ~/.local/bin/ctask
```

Alternatively, install it as a package (adds a `ctask` console-script entry
point):

```
pip install -e .
```

## Tests

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/ -q
```

(Or just `.venv/bin/pip install -q pytest` instead of the editable install —
the tests load `ctask.py` directly via `importlib`, not via the installed
package.)

A working `git` binary is required: several tests build throwaway git repos.

## Registry file format

Each open task is an H3 span under an H2 module-section header:

```
## <section>

### <ID> — <title>

- prio:: high
- module:: interfaces.scheduler
- created:: 2026-07-15
- blocked-by:: SCH-12
- refs:: docs/some-doc.md, src/some/file.py

**Goal.** ...

**Details.**
...

**Done when.** ...
```

`- key:: value` metadata bullets: `prio::`, `module::`, and `created::` are
required; `blocked-by::` and `refs::` are optional; unknown extra keys are
preserved. The prose sections are `**Goal.**`, `**Details.**` (optional), and
`**Done when.**`.

Byte-identical round-tripping of every untouched task span — parse the file,
serialize it back out unmodified, get the same bytes — is the load-bearing
invariant. `close` moves a span (verbatim, plus two appended metadata
bullets) into DONE.md; `add` appends a new span; `edit` rewrites only the
pieces named by its flags and refuses to write if the result doesn't
round-trip or would alter any other task's bytes.
