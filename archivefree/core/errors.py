"""Error types for archive operations.

Every error carries a plain-language message suitable for showing directly to a
user, plus an optional ``detail`` string with the technical cause for the
"Details" expander in error dialogs.
"""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for everything that can go wrong with an archive.

    Args:
        message: A short sentence a non-technical person can act on.
        detail: Raw technical output (tool stderr, exception text). Optional.
        hint: A suggested next step, shown under the message.
    """

    def __init__(self, message: str, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class UnsupportedFormat(ArchiveError):
    """The file is not an archive we know how to read."""


class MissingTool(ArchiveError):
    """A required helper program is not installed.

    This is the one error class that is genuinely fixable by the user, so we
    always try to name the exact package to install.
    """

    def __init__(self, tool: str, purpose: str, packages: str | None = None):
        message = (f"ArchiveFree needs the “{tool}” program to {purpose}, "
                   "but it isn’t installed.")
        hint = f"Install it with:  sudo apt install {packages}" if packages else None
        super().__init__(message, hint=hint)
        self.tool = tool


class PasswordRequired(ArchiveError):
    """The archive is encrypted and no password was supplied."""

    def __init__(self, message: str = "This archive is password-protected."):
        super().__init__(message)


class WrongPassword(ArchiveError):
    """A password was supplied but it did not work."""

    def __init__(self, message: str = "That password didn’t work."):
        super().__init__(message, hint="Passwords are case-sensitive. Try again.")


class CorruptArchive(ArchiveError):
    """The archive is damaged or truncated."""

    def __init__(self, message: str = "This archive appears to be damaged.",
                 detail: str | None = None):
        super().__init__(
            message,
            detail=detail,
            hint="It may have been downloaded incompletely. Try downloading it again.",
        )


class MissingVolume(ArchiveError):
    """A split archive is missing one or more of its parts."""

    def __init__(self, missing: str, detail: str | None = None):
        super().__init__(
            f"Part of this split archive is missing: {missing}",
            detail=detail,
            hint="All parts must be in the same folder before it can be opened.",
        )


class OperationCancelled(ArchiveError):
    """The user cancelled the operation. Not shown as an error."""

    def __init__(self) -> None:
        super().__init__("Cancelled.")


class DiskFull(ArchiveError):
    def __init__(self, path: str):
        super().__init__(
            "There isn’t enough free space to finish extracting.",
            hint=f"Free up some space in {path}, or extract somewhere else.",
        )


class UnsafePath(ArchiveError):
    """An entry tried to escape the destination directory (Zip Slip)."""

    def __init__(self, name: str):
        super().__init__(
            "This archive tried to write files outside the folder you chose, "
            "which is a sign it may be malicious.",
            detail=f"Refused entry: {name}",
            hint="ArchiveFree blocked it. Nothing was written outside the destination.",
        )
