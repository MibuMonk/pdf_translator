#!/usr/bin/env python3
"""
Contract validator — agents call this to verify their own output before writing.

Usage (inside any agent):
    from contracts.validate import validate_output
    validate_output(data, "parsed")   # raises on violation
"""

import json
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent

def _load_schema(name: str) -> dict:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"No schema found for '{name}' at {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_output(data: dict, schema_name: str) -> list[str]:
    """
    Validate data against the named schema.
    Returns a list of violation strings (empty = valid).
    Does NOT raise — agents decide how to handle violations.

    schema_name: one of parsed | translated | layout_plan | qa_report | consolidator_log
    """
    schema = _load_schema(schema_name)
    violations = []
    _check(data, schema, schema, path="$", violations=violations)
    return violations


def _check(data, schema: dict, root_schema: dict, path: str, violations: list):
    """Recursive schema checker (subset of JSON Schema draft-07)."""

    # $ref resolution
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            key = ref.split("/")[-1]
            _check(data, root_schema["$defs"][key], root_schema, path, violations)
            return
        # cross-file refs: skip (treated as valid)
        return

    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(data, dict):
            violations.append(f"{path}: expected object, got {type(data).__name__}")
            return
        for req in schema.get("required", []):
            if req not in data:
                violations.append(f"{path}: missing required field '{req}'")
        for key, sub_schema in schema.get("properties", {}).items():
            if key in data:
                _check(data[key], sub_schema, root_schema, f"{path}.{key}", violations)

    elif schema_type == "array":
        if not isinstance(data, list):
            violations.append(f"{path}: expected array, got {type(data).__name__}")
            return
        min_items = schema.get("minItems", 0)
        max_items = schema.get("maxItems", float("inf"))
        if len(data) < min_items:
            violations.append(f"{path}: array too short ({len(data)} < {min_items})")
        if len(data) > max_items:
            violations.append(f"{path}: array too long ({len(data)} > {max_items})")
        if "items" in schema:
            for i, item in enumerate(data):
                _check(item, schema["items"], root_schema, f"{path}[{i}]", violations)

    elif schema_type == "string":
        if not isinstance(data, str):
            violations.append(f"{path}: expected string, got {type(data).__name__}")
        elif "enum" in schema and data not in schema["enum"]:
            violations.append(f"{path}: '{data}' not in {schema['enum']}")
        elif "pattern" in schema:
            import re
            if not re.fullmatch(schema["pattern"], data):
                violations.append(f"{path}: '{data}' does not match pattern '{schema['pattern']}'")

    elif schema_type == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            violations.append(f"{path}: expected integer, got {type(data).__name__}")
        else:
            if "minimum" in schema and data < schema["minimum"]:
                violations.append(f"{path}: {data} < minimum {schema['minimum']}")
            if "enum" in schema and data not in schema["enum"]:
                violations.append(f"{path}: {data} not in {schema['enum']}")

    elif schema_type == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            violations.append(f"{path}: expected number, got {type(data).__name__}")
        else:
            if "minimum" in schema and data < schema["minimum"]:
                violations.append(f"{path}: {data} < minimum {schema['minimum']}")
            if "maximum" in schema and data > schema["maximum"]:
                violations.append(f"{path}: {data} > maximum {schema['maximum']}")

    elif schema_type == "boolean":
        if not isinstance(data, bool):
            violations.append(f"{path}: expected boolean, got {type(data).__name__}")


if __name__ == "__main__":
    # CLI: python validate.py <json_file> <schema_name>
    if len(sys.argv) != 3:
        print("Usage: validate.py <json_file> <schema_name>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    violations = validate_output(data, sys.argv[2])
    if violations:
        print(f"INVALID ({len(violations)} violations):")
        for v in violations:
            print(f"  {v}")
        sys.exit(1)
    else:
        print("VALID")
