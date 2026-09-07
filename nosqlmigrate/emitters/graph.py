"""Neo4j emitter: nodes, relationships, constraints.

The FK graph maps almost directly: tables become nodes (or properties, for
lookups nothing ever traverses), FKs become relationships, junctions become
relationships — with properties when they carry payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..analyze import Analysis
from ..rules.engine import DecisionSet

_IRREGULAR = {"copies": "copy", "branches": "branch", "statuses": "status",
              "indices": "index", "addresses": "address"}


def _singular(table: str) -> str:
    lowered = table.lower()
    if lowered in _IRREGULAR:
        return _IRREGULAR[lowered]
    if lowered.endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.endswith(("ches", "shes", "sses", "xes")):
        return lowered[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _label(table: str) -> str:
    # "Genres" → "Genre", "book_copies" → "BookCopy"
    name = _singular(table)
    return "".join(part.capitalize() for part in name.split("_"))


@dataclass
class GraphNode:
    label: str
    source_table: str
    key_property: str | None
    properties: list[str]

    def to_dict(self) -> dict:
        return {"label": self.label, "source_table": self.source_table,
                "key": self.key_property, "properties": self.properties}


@dataclass
class GraphRelationship:
    rel_type: str
    source: str  # label
    target: str  # label
    origin: str  # fk / junction / self-ref
    properties: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {"type": self.rel_type, "source": self.source, "target": self.target,
                "origin": self.origin, "properties": self.properties, "note": self.note}


@dataclass
class GraphResult:
    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)
    property_placements: list[dict] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
            "property_placements": self.property_placements,
            "indexes": self.indexes,
        }

    def render(self) -> str:
        lines = [
            "// Neo4j graph proposal",
            "// Nodes from tables (except never-traversed lookups, which become",
            "// properties); FKs become relationships; junctions become relationships",
            "// with properties when they carry payload.",
            "",
            "// -- constraints (unique keys) -------------------------------------",
        ]
        for node in self.nodes:
            if node.key_property:
                lines.append(
                    f"CREATE CONSTRAINT {node.label.lower()}_{node.key_property}_unique "
                    f"IF NOT EXISTS FOR (n:{node.label}) REQUIRE n.{node.key_property} IS UNIQUE;"
                )
        lines += ["", "// -- indexes on frequently filtered properties ----------------------"]
        for idx in self.indexes:
            # idx = "CREATE INDEX FOR (n:Label) ON (n.prop)"
            body = idx[len("CREATE INDEX "):]
            label = body.split(":")[1].split(")")[0]
            prop = body.split("n.")[1].rstrip(");")
            name = f"{label.lower()}_{prop}_idx"
            lines.append(f"CREATE INDEX {name} IF NOT EXISTS{body};")
        lines += ["", "// -- model ----------------------------------------------------------"]
        for rel in self.relationships:
            props = f" {{{', '.join(rel.properties)}}}" if rel.properties else ""
            note = f"  // {rel.note}" if rel.note else ""
            lines.append(f"(:{rel.source})-[:{rel.rel_type}{props}]->(:{rel.target}){note}")
        if self.property_placements:
            lines += ["", "// -- lookups placed as properties (never traversed) -----------------"]
            for p in self.property_placements:
                lines.append(f"// {p['table']} → property {p['property']} on :{p['on']}")
        lines += ["", "// -- example queries from the workload -------------------------------"]
        lines += _example_queries(self)
        return "\n".join(lines) + "\n"


def _example_queries(result: GraphResult) -> list[str]:
    keys = {n.label: n.key_property for n in result.nodes}
    out = []
    for rel in result.relationships[:6]:
        key = keys.get(rel.source) or "id"
        out.append(
            f"MATCH (a:{rel.source})-[r:{rel.rel_type}]->(b:{rel.target}) "
            f"WHERE a.{key} = $id RETURN b;"
        )
    return out


def emit_graph(analysis: Analysis, decisions: DecisionSet) -> GraphResult:
    model = analysis.model
    dispositions = decisions.dispositions
    result = GraphResult()

    junction_tables = set(analysis.junctions)

    # FKs that appear in some query's join edges → the parent was traversed
    traversed_tables: set[str] = set()
    for q in analysis.workload.queries:
        for a, b in q.joins:
            traversed_tables.update((a, b))

    # D05 duplicated lookups: property placement vs node
    duplicated_into: dict[str, list[str]] = {}  # lookup table -> [fields]
    for d in decisions.decisions:
        if d.action == "DUPLICATE":
            duplicated_into[d.parent] = d.signals.get("duplicated_fields", [])

    # nodes: everything except junctions, 1:1-embedded children, and
    # lookups that no query ever traverses
    for name in sorted(model.tables):
        if name in junction_tables:
            continue
        disp = dispositions.get(name)
        if disp is not None and disp.disposition == "embedded":
            table = model.table(name)
            # 1:1 collapse: properties on the parent node
            parent = disp.into
            if table is not None and parent:
                props = [c.canonical for c in table.columns
                         if table.pk is None or c.canonical not in table.pk.columns]
                result.property_placements.append({
                    "table": name, "on": _label(parent), "property": f"{_singular(name)}_*",
                    "note": f"1:1 collapse ({disp.rule_id}): {', '.join(props)} "
                            f"become properties on :{_label(parent)}",
                })
            continue
        if name in duplicated_into and name not in traversed_tables:
            # never traversed → its fields are properties on referencing nodes
            for child_dec in [d for d in decisions.decisions
                              if d.action == "DUPLICATE" and d.parent == name]:
                result.property_placements.append({
                    "table": name, "on": _label(child_dec.child),
                    "property": ", ".join(duplicated_into[name]),
                    "note": f"lookup never traversed ({child_dec.rule_id}): fields "
                            f"ride as properties on :{_label(child_dec.child)}",
                })
            continue

        table = model.table(name)
        if table is None:
            continue
        key = table.pk.columns[0] if table.pk and len(table.pk.columns) == 1 else None
        props = [c.canonical for c in table.columns]
        # duplicated lookup fields ride along as properties here too
        for d in decisions.decisions:
            if d.action == "DUPLICATE" and d.child == name:
                props.extend(f for f in d.signals.get("duplicated_fields", []) if f not in props)
        result.nodes.append(GraphNode(
            label=_label(name), source_table=name, key_property=key, properties=props,
        ))

    node_labels = {n.source_table: n.label for n in result.nodes}

    # relationships from FK edges
    for rel in analysis.relationships:
        if rel.child in junction_tables or rel.parent in junction_tables:
            continue  # handled via junction decisions below
        if rel.parent not in node_labels or rel.child not in node_labels:
            continue
        if rel.parent == rel.child:
            result.relationships.append(GraphRelationship(
                rel_type="PARENT_OF",
                source=node_labels[rel.parent], target=node_labels[rel.child],
                origin=f"self-reference {rel.fk_name or ''}".strip(),
                note="adjacency list; materialized path is an option for subtree reads",
            ))
            continue
        result.relationships.append(GraphRelationship(
            rel_type=f"HAS_{relabel(rel.child).upper()}",
            source=node_labels[rel.parent], target=node_labels[rel.child],
            origin=rel.fk_name or "fk",
        ))

    # relationships from junctions
    for d in decisions.decisions:
        if d.action == "FOLD":
            bounded = d.signals.get("bounded_side")
            host = d.signals.get("host")
            if bounded in node_labels and host in node_labels:
                result.relationships.append(GraphRelationship(
                    rel_type=f"HAS_{relabel(bounded).upper()}",
                    source=node_labels[host], target=node_labels[bounded],
                    origin=f"junction {d.child}",
                    note="one-way from the junction fold; membership is also "
                         "queryable via the id array on the document target",
                ))
        elif d.action == "KEEP" and d.child:
            info = analysis.junctions.get(d.child)
            if info is None:
                continue
            a, b = info.sides
            if a in node_labels and b in node_labels:
                payload = []
                table = model.table(d.child)
                if table is not None and table.pk is not None:
                    payload = [c.canonical for c in table.columns
                               if c.canonical not in table.pk.columns]
                result.relationships.append(GraphRelationship(
                    rel_type=relabel(d.child).upper(),
                    source=node_labels[a], target=node_labels[b],
                    origin=f"junction {d.child}",
                    properties=payload,
                    note="junction kept as its own collection on the document "
                         "target; here it is a first-class relationship",
                ))

    # indexes: frequently filtered properties
    seen_idx: set[str] = set()
    for q in analysis.workload.queries:
        if q.is_write:
            continue
        for p in q.predicates:
            if p.table in node_labels and p.op in ("=", "IN", "LIKE", "ILIKE"):
                label = node_labels[p.table]
                idx = f"FOR (n:{label}) ON (n.{p.column})"
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    result.indexes.append(f"CREATE INDEX {idx}")

    result.relationships.sort(key=lambda r: (r.source, r.rel_type, r.target))
    return result


def relabel(table: str) -> str:
    """Relationship type fragment: singular, snake→upper."""
    return _singular(table).upper()
