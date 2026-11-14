"""Exit codes and the shared error type.

Exit codes follow screw except for budget exhaustion (design, Error Handling):
0 success (including budget_exhausted), 1 failure, 2 usage or config,
4 skipped (already running), 5 partial.
"""

from __future__ import annotations

SUCCESS = 0
FAILURE = 1
USAGE_ERROR = 2
SKIPPED = 4
PARTIAL = 5


class DreamingError(Exception):
    """An error with a process exit code and a stable code for `dream api`."""

    def __init__(
        self, message: str, exit_code: int = FAILURE, code: str = "error"
    ) -> None:
        self.message = message
        self.exit_code = exit_code
        self.code = code
        super().__init__(message)
