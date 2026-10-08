"""Structured-output schemas reduced to what model providers accept on the wire."""

from typing import Any, cast

# Providers accept a subset of JSON Schema. Anything else is removed from the wire
# schema; Pydantic validates the reply against the full model afterwards.
_KEPT = frozenset(
    {
        "type",
        "required",
        "enum",
        "format",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "title",
        "description",
    }
)


def wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local references and keep only keywords the provider accepts."""
    definitions = cast(dict[str, Any], schema.get("$defs", {}))

    def convert(node: Any, depth: int) -> Any:
        if depth > 16:
            raise ValueError("Schema is nested too deeply")
        if isinstance(node, list):
            return [convert(item, depth + 1) for item in cast(list[Any], node)]
        if not isinstance(node, dict):
            return node
        source = cast(dict[str, Any], node)
        if "$ref" in source:
            return convert(definitions[str(source["$ref"]).rsplit("/", 1)[-1]], depth + 1)
        result: dict[str, Any] = {}
        for key, value in source.items():
            if key == "properties":
                result[key] = {
                    name: convert(child, depth + 1)
                    for name, child in cast(dict[str, Any], value).items()
                }
            elif key in {"items", "anyOf", "prefixItems"}:
                result[key] = convert(value, depth + 1)
            elif key == "additionalProperties":
                result[key] = convert(value, depth + 1) if isinstance(value, dict) else value
            elif key in _KEPT:
                result[key] = value
        return result

    return cast(dict[str, Any], convert(schema, 0))
