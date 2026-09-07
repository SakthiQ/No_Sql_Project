"""Dialect handling. Every dialect conditional lives here, nowhere else."""
from __future__ import annotations

import re

SUPPORTED_DIALECTS = ("mysql", "postgres")

_ALIASES = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
}

_MYSQL_HINTS = re.compile(r"`\w+`|AUTO_INCREMENT\s*=\s*\d+|ENGINE\s*=\s*InnoDB", re.I)
_PG_HINTS = re.compile(
    r"\bSERIAL\b|\bBIGSERIAL\b|\bJSONB\b|::\w+|\bILIKE\b|\bnow\(\)\b|\bRETURNING\b",
    re.I,
)


def normalize_dialect(name: str | None) -> str:
    """Map user-facing dialect names/aliases onto the two we support."""
    if not name or name.lower() == "auto":
        return "auto"
    key = name.lower()
    if key not in _ALIASES:
        raise ValueError(f"unsupported dialect {name!r}; expected one of {SUPPORTED_DIALECTS} or 'auto'")
    return _ALIASES[key]


def detect_dialect(sql: str) -> str:
    """Structural sniffing when the user didn't say. Backticks and InnoDB are
    MySQL; SERIAL/JSONB/ILIKE/`::` casts are Postgres. MySQL wins ties because
    backticks are unambiguous."""
    if _MYSQL_HINTS.search(sql):
        return "mysql"
    if _PG_HINTS.search(sql):
        return "postgres"
    return "postgres"  # neutral default: plain ANSI-ish DDL


def resolve_dialect(sql: str, requested: str | None) -> str:
    normalized = normalize_dialect(requested)
    if normalized != "auto":
        return normalized
    return detect_dialect(sql)


def sqlglot_dialect(dialect: str) -> str:
    """Internal dialect name → sqlglot read dialect."""
    return dialect if dialect in SUPPORTED_DIALECTS else "postgres"
