"""Diagnostics: accumulate problems, never raise.

An unparseable statement records a Diagnostic and the run continues. Phase 6
needs exactly this list to produce its "constructs you must handle in the
application layer" section, so collecting them is a feature, not just error
tolerance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# Stable diagnostic codes (part of the public output).
PARSE_ERROR = "PARSE_ERROR"
UNSUPPORTED_STATEMENT = "UNSUPPORTED_STATEMENT"
UNMIGRATABLE = "UNMIGRATABLE"
UNMIGRATABLE_CHECK = "UNMIGRATABLE_CHECK"
DANGLING_FK = "DANGLING_FK"
MISSING_PK = "MISSING_PK"
UNKNOWN_TYPE = "UNKNOWN_TYPE"
ALTER_TARGET_MISSING = "ALTER_TARGET_MISSING"


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    excerpt: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "excerpt": self.excerpt,
        }


def info(code: str, message: str, excerpt: str = "") -> Diagnostic:
    return Diagnostic(Severity.INFO, code, message, excerpt)


def warning(code: str, message: str, excerpt: str = "") -> Diagnostic:
    return Diagnostic(Severity.WARNING, code, message, excerpt)


def error(code: str, message: str, excerpt: str = "") -> Diagnostic:
    return Diagnostic(Severity.ERROR, code, message, excerpt)
