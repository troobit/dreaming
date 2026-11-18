# Python and SQLite gotchas

Language and library behaviour met while building dreaming, with where the code
handles it. Verified on CPython 3.15.2 with SQLite 3.54.1 (November 2026).

## Types

- `bool` is a subclass of `int`, so `isinstance(True, int)` holds and `True == 1`.
  Every integer check rejects booleans first: `schema._is` for role output,
  `config` for numeric settings, and `api._limit`, `api._revisions` and
  `api._correction` for request fields.

## sqlite3

- Connections use `sqlite3.connect(..., autocommit=True)` with explicit
  `BEGIN IMMEDIATE`/`COMMIT` (`store.transaction`). In the default legacy mode,
  `executescript` commits any open transaction first and runs each statement on
  its own, so a migration that failed halfway would stay half applied. Migrations
  are split with `store.statements` (`sqlite3.complete_statement`) and executed
  one at a time inside one transaction.
- Opening a WAL store only to read it still writes. Any connection, including
  `mode=ro`, may update the `-shm` file. When the last connection closes, SQLite
  checkpoints a `-wal` left by a dead process into the database file and
  deletes the `-wal` and `-shm` files. `store.memory_copy` (dry run, `dream trial`)
  therefore copies the database and its `-wal` to a private temporary directory,
  re-reading until their inode, size and mtime are stable, and backs up from the
  copy into memory.
- `INSERT OR IGNORE` skips NOT NULL and CHECK violations as well as duplicate
  keys, without an error. Use it only where duplicates are expected
  (observations); imports use plain `INSERT` so a broken export fails
  (`knowledge-api.md`).

## Processes and descriptors

- `python -m pkg.module` puts the working directory first on `sys.path`, so run
  from a checkout's `src/` it imports the checkout, not the installed version.
  The launcher and the detached child run with `-I` (`run-engine.md`).
- `os.kill(pid, 0)` succeeds for a zombie. A test that kills a child it started
  must `wait()` on it before checking whether the pid is gone.
- A descriptor passed with `pass_fds` shares its open file description with the
  parent, and `flock` belongs to that description. The child's own `flock` on it
  is a no-op, and the lock lasts until the last descriptor closes. The child marks
  it non-inheritable so the processes it starts cannot keep the lock, and
  `backends.run_process` also passes `close_fds=True` (the POSIX default) explicitly.
- `os.pipe()` descriptors are non-inheritable; `pass_fds` makes only the listed
  ones inheritable in the child. `os.read` on a pipe's read end returns `b""`
  once every write end is closed, including by the death of the process holding
  it. The ready barrier in `run.start` depends on the child never receiving the
  write end.

## Annotations and syntax (3.14+)

- Annotations are evaluated lazily (PEP 649), so modules have no
  `from __future__ import annotations` (Q144). Names imported only under
  `TYPE_CHECKING` (`roles.py`, `backends/__init__.py`) are safe in
  annotations because nothing evaluates them. `typing.get_type_hints` or
  `__annotations__` would raise `NameError` for such a name; read them with
  `annotationlib.get_annotations(obj, format=Format.FORWARDREF)` instead.
- The fake executables in `tests/fakes/` keep the import: their shebang is
  `#!/usr/bin/env python3`, which can be macOS's 3.9 when run by hand. The
  `fakes` fixture rewrites the shebang to `sys.executable`.
- With `target-version = "py315"`, `ruff format` drops the parentheses from
  multi-exception handlers (`except ValueError, TypeError:`, PEP 758). It
  catches either exception; it is not Python 2's `except X, name:`. With `as`
  the parentheses stay. On 3.13 or older these handlers are a syntax error.

## Tooling

- `ruff format .` also formats ```` ```python ```` blocks inside Markdown, so
  `specs/` is in `extend-exclude`.
