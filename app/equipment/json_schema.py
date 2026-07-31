"""Attacker v1 支持的受限 JSON Schema 校验器，避免运行时引入宽泛动态行为。"""

from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    """表示 Schema 文档或实例不满足 Core 支持的确定性子集。"""


def validate_json_schema_document(schema: object, *, name: str) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"{name}: schema root must be an object")
    if schema.get("type") not in {
        None,
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
    }:
        raise SchemaValidationError(f"{name}: unsupported root type")
    if "required" in schema and not isinstance(schema["required"], list):
        raise SchemaValidationError(f"{name}: required must be an array")
    if "properties" in schema and not isinstance(schema["properties"], dict):
        raise SchemaValidationError(f"{name}: properties must be an object")
    return schema


def validate_instance(value: object, schema: dict[str, Any], *, path: str = "$") -> None:
    expected = schema.get("type")
    if expected and not _matches_type(value, str(expected)):
        raise SchemaValidationError(f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is not in enum")
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise SchemaValidationError(f"{path}.{required}: required property missing")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate_instance(item, properties[key], path=f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise SchemaValidationError(f"{path}.{key}: additional property is not allowed")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_instance(item, schema["items"], path=f"{path}[{index}]")


def _matches_type(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True
