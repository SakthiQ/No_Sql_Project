"""Diagrams: Mermaid ER rendering of the source schema (used by the web UI)."""
from __future__ import annotations

from ..analyze import Analysis


def _entity_name(table: str) -> str:
    return table.upper()


def mermaid_er(analysis: Analysis) -> str:
    lines = ["erDiagram"]
    rels = {(r.parent, r.child): r for r in analysis.relationships}

    for name in sorted(analysis.tables):
        table = analysis.model.table(name)
        if table is None:
            continue
        lines.append(f"    {_entity_name(name)} {{")
        shown = 0
        for col in table.columns:
            lines.append(f"        {col.category.value.lower()} {col.canonical}")
            shown += 1
            if shown >= 8:
                lines.append(f"        text ... ({len(table.columns) - shown} more)")
                break
        lines.append("    }")

    def card(rel) -> str:
        if rel.cardinality == "1:1":
            return "||--||"
        nullable_fk = not _fk_required(analysis, rel.child, rel.parent)
        return "||--o|" if nullable_fk else "||--|{"

    emitted: set[tuple[str, str]] = set()
    for key in sorted(rels):
        rel = rels[key]
        left, right = _entity_name(rel.parent), _entity_name(rel.child)
        pair = tuple(sorted((left, right)))
        label = "self" if rel.parent == rel.child else f"via {rel.fk_name or 'fk'}"
        if pair in emitted and rel.parent != rel.child:
            label = f"via {rel.fk_name or 'fk'}"
        lines.append(f"    {left} {card(rel)} {right} : {label}")
        emitted.add(pair)

    for jname, info in sorted(analysis.junctions.items()):
        for side in info.sides:
            lines.append(f"    {_entity_name(side)} ||--o{{ {_entity_name(jname)} : junction")

    return "\n".join(lines)


def _fk_required(analysis: Analysis, child: str, parent: str) -> bool:
    table = analysis.model.table(child)
    if table is None:
        return True
    for fk in table.foreign_keys:
        if fk.target_table == parent:
            return all(not table.is_nullable(c) for c in fk.source_columns)
    return True
