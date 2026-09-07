"""Canonical identifier handling.

SQL identifiers are case-insensitive unless quoted — and quoting is
dialect-specific. Every identifier is stored with the original spelling (for
output) plus a canonical key (for lookup): unquoted names fold to lowercase,
quoted names keep their exact spelling. Getting this wrong produces the
nastiest bug class available here: two spellings of one table becoming two
tables, silently disconnecting the FK graph.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identifier:
    """An SQL identifier with its original spelling and quoting status."""

    original: str
    quoted: bool = False

    @property
    def canonical(self) -> str:
        """Lookup key: exact spelling when quoted, lowercase otherwise."""
        return self.original if self.quoted else self.original.lower()

    def to_dict(self) -> dict:
        return {"original": self.original, "quoted": self.quoted, "canonical": self.canonical}


def canonical_name(name: str, quoted: bool = False) -> str:
    """Canonicalize a bare name string."""
    return name if quoted else name.lower()
