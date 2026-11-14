"""A small JSON Schema validator for role outputs (4.1, 4.7).

The package has no runtime dependencies (Q77), so this covers only the keywords
the role schemas use. `unsupported` lists any other keyword in a schema, and
loading a schema refuses one, so no constraint is silently ignored.
"""

from __future__ import annotations

from typing import Any

SUPPORTED = frozenset(
    {
        "$schema",
        "title",
        "description",
        "type",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
)

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _is(value: Any, name: str) -> bool:
    if isinstance(value, bool) and name in ("integer", "number"):
        return False
    return isinstance(value, _TYPES[name])


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Every way `value` fails `schema`, as `path: problem` lines."""
    kinds = schema.get("type")
    if kinds is not None:
        names = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, name) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    problems = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems += [
            f"{path}: missing {key}"
            for key in schema.get("required", [])
            if key not in value
        ]
        if schema.get("additionalProperties") is False:
            problems += [
                f"{path}: unexpected {key}" for key in value if key not in properties
            ]
        for key, sub in properties.items():
            if key in value:
                problems += validate(value[key], sub, f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: allows at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                problems += validate(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{path}: needs at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: allows at most {schema['maxLength']} characters")
    elif _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: must be at most {schema['maximum']}")
    return problems


def unsupported(schema: dict, path: str = "$") -> list[str]:
    """Keywords this validator would ignore, with where they appear."""
    found = [f"{path}: {key}" for key in schema if key not in SUPPORTED]
    for key, sub in schema.get("properties", {}).items():
        found += unsupported(sub, f"{path}.{key}")
    if isinstance(schema.get("items"), dict):
        found += unsupported(schema["items"], f"{path}[]")
    return found
