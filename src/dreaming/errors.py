"""Exit codes and the shared error type.

Exit codes follow screw except for budget exhaustion (design, Error Handling):
0 success (including budget_exhausted), 1 failure, 2 usage or config,
4 skipped (already running), 5 partial.
"""

SUCCESS = 0
FAILURE = 1
USAGE_ERROR = 2
SKIPPED = 4
PARTIAL = 5


class DreamingError(Exception):
    """An error with a process exit code and a stable code for `dream api`;
    `details` adds machine-readable fields to the error response."""

    def __init__(
        self,
        message: str,
        exit_code: int = FAILURE,
        code: str = "error",
        details: dict | None = None,
    ) -> None:
        self.message = message
        self.exit_code = exit_code
        self.code = code
        self.details = details or {}
        super().__init__(message)


class Rejected(Exception):
    """A model's observation or operation that fails the program's rules; it is
    recorded with the reason and not applied (6.2, 19)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Partial(DreamingError):
    """No backend is left for a role: the run saves its progress and ends partial
    (5.3)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, PARTIAL, "partial")
