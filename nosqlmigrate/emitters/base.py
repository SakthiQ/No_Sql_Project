"""Shared emitter plumbing."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Artifact:
    """One output file: rendered text + its structured form when applicable."""

    name: str  # file name, e.g. "document.json"
    kind: str  # "json" | "cql" | "cypher" | "markdown"
    content: str
    data: dict | list | None = None


@dataclass
class EmittedIndex:
    keys: list[tuple[str, int]]  # (field path, 1 | -1)
    unique: bool = False
    reason: str = ""
    multikey: bool = False

    def to_dict(self) -> dict:
        return {
            "keys": [{"field": f, "direction": d} for f, d in self.keys],
            "unique": self.unique,
            "reason": self.reason,
            "multikey": self.multikey,
        }


def render_index(collection: str, index: EmittedIndex) -> str:
    keys = ", ".join(f'"{f}": {d}' for f, d in index.keys)
    options = ", unique: true" if index.unique else ""
    return f'db.{collection}.createIndex({{{keys}{options}}})  // {index.reason}'


@dataclass
class FieldSpec:
    name: str
    source: str  # provenance, e.g. "column:books.title" / "embedded:book_details"
    bson_type: str = ""  # bsonType value; "" for arrays-of-objects / nested objects
    required: bool = False
    description: str = ""
    array_of: FieldSpec | None = None  # for arrays of sub-documents
    max_items: int | None = None  # for capped (subset) arrays
    properties: list[FieldSpec] | None = None  # for object fields
    example: object = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "source": self.source, "required": self.required}
        if self.description:
            d["description"] = self.description
        if self.array_of is not None:
            d["array_of"] = self.array_of.to_dict()
        elif self.properties:
            d["object"] = [p.to_dict() for p in self.properties]
        else:
            d["bson_type"] = self.bson_type
            d["example"] = self.example
        return d


def field_to_schema(field: FieldSpec) -> dict:
    """FieldSpec → the $jsonSchema property/fragment for it."""
    if field.array_of is not None:
        inner = field_to_schema(field.array_of)
        schema: dict = {"bsonType": "array", "items": inner}
        if field.max_items is not None:
            schema["maxLength"] = field.max_items  #MongoDB uses maxItems for arrays
            schema["maxItems"] = field.max_items
            schema.pop("maxLength", None)
        return schema
    if field.properties:
        return {"bsonType": "object", **object_schema(field.properties)}
    return {"bsonType": field.bson_type}


def object_schema(fields: list[FieldSpec]) -> dict:
    properties = {f.name: field_to_schema(f) for f in fields}
    required = [f.name for f in fields if f.required]
    out = {"properties": properties}
    if required:
        out["required"] = required
    return out
