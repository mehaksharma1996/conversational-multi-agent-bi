"""Detect backward-incompatible changes in the committed OpenAPI contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
ADR_REFERENCE = re.compile(r"^ADR[- ]?\d{4}$", re.IGNORECASE)


def compare_openapi(base: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Return stable descriptions of breaking changes from base to current."""
    issues: list[str] = []
    base_paths = base.get("paths", {})
    current_paths = current.get("paths", {})
    for path, base_path_item in sorted(base_paths.items()):
        current_path_item = current_paths.get(path)
        if current_path_item is None:
            issues.append(f"removed path {path}")
            continue
        for method, base_operation in sorted(base_path_item.items()):
            if method.lower() not in HTTP_METHODS:
                continue
            current_operation = current_path_item.get(method)
            location = f"{method.upper()} {path}"
            if current_operation is None:
                issues.append(f"removed operation {location}")
                continue
            _compare_parameters(
                [*base_path_item.get("parameters", []), *base_operation.get("parameters", [])],
                [
                    *current_path_item.get("parameters", []),
                    *current_operation.get("parameters", []),
                ],
                base,
                current,
                location,
                issues,
            )
            _compare_request(base_operation, current_operation, base, current, location, issues)
            _compare_responses(base_operation, current_operation, base, current, location, issues)
    return issues


def breaking_override_allowed(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    reference: str | None,
) -> bool:
    """Require a well-formed ADR reference and a deliberate API version change."""
    if reference is None or ADR_REFERENCE.fullmatch(reference) is None:
        return False
    base_version = str(base.get("info", {}).get("version", ""))
    current_version = str(current.get("info", {}).get("version", ""))
    return bool(current_version and current_version != base_version)


def _compare_parameters(
    base_parameters: list[dict[str, Any]],
    current_parameters: list[dict[str, Any]],
    base_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    location: str,
    issues: list[str],
) -> None:
    base_by_key = {(item.get("in"), item.get("name")): item for item in base_parameters}
    current_by_key = {(item.get("in"), item.get("name")): item for item in current_parameters}
    for key, base_parameter in sorted(base_by_key.items(), key=lambda item: str(item[0])):
        current_parameter = current_by_key.get(key)
        label = f"{location} parameter {key[0]}:{key[1]}"
        if current_parameter is None:
            issues.append(f"removed {label}")
            continue
        _compare_schema(
            base_parameter.get("schema", {}),
            current_parameter.get("schema", {}),
            base_document,
            current_document,
            label,
            "request",
            issues,
        )
    for key, current_parameter in sorted(current_by_key.items(), key=lambda item: str(item[0])):
        if key not in base_by_key and current_parameter.get("required"):
            issues.append(f"new required {location} parameter {key[0]}:{key[1]}")


def _compare_request(
    base_operation: Mapping[str, Any],
    current_operation: Mapping[str, Any],
    base_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    location: str,
    issues: list[str],
) -> None:
    base_body = base_operation.get("requestBody")
    current_body = current_operation.get("requestBody")
    if base_body is not None and current_body is None:
        issues.append(f"removed request body from {location}")
        return
    if base_body is None:
        if current_body is not None and current_body.get("required"):
            issues.append(f"new required request body on {location}")
        return
    assert current_body is not None
    if not base_body.get("required") and current_body.get("required"):
        issues.append(f"request body became required on {location}")
    _compare_content(
        base_body.get("content", {}),
        current_body.get("content", {}),
        base_document,
        current_document,
        f"{location} request",
        "request",
        issues,
    )


def _compare_responses(
    base_operation: Mapping[str, Any],
    current_operation: Mapping[str, Any],
    base_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    location: str,
    issues: list[str],
) -> None:
    base_responses = base_operation.get("responses", {})
    current_responses = current_operation.get("responses", {})
    for status, base_response in sorted(base_responses.items()):
        current_response = current_responses.get(status)
        response_location = f"{location} response {status}"
        if current_response is None:
            issues.append(f"removed {response_location}")
            continue
        _compare_content(
            base_response.get("content", {}),
            current_response.get("content", {}),
            base_document,
            current_document,
            response_location,
            "response",
            issues,
        )


def _compare_content(
    base_content: Mapping[str, Any],
    current_content: Mapping[str, Any],
    base_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    location: str,
    mode: str,
    issues: list[str],
) -> None:
    for media_type, base_media in sorted(base_content.items()):
        current_media = current_content.get(media_type)
        if current_media is None:
            issues.append(f"removed media type {media_type} from {location}")
            continue
        _compare_schema(
            base_media.get("schema", {}),
            current_media.get("schema", {}),
            base_document,
            current_document,
            f"{location} ({media_type})",
            mode,
            issues,
        )


def _resolve_schema(schema: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        ref = str(schema["$ref"])
        if not ref.startswith("#/components/schemas/"):
            return dict(schema)
        name = ref.rsplit("/", 1)[-1]
        resolved = document.get("components", {}).get("schemas", {}).get(name, {})
        return _resolve_schema(
            {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}, document
        )
    if "allOf" in schema:
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        properties: dict[str, Any] = dict(merged.get("properties", {}))
        required = set(merged.get("required", []))
        for part in schema["allOf"]:
            resolved = _resolve_schema(part, document)
            properties.update(resolved.get("properties", {}))
            required.update(resolved.get("required", []))
            for key, value in resolved.items():
                if key not in {"properties", "required"}:
                    merged.setdefault(key, value)
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = sorted(required)
        return merged
    return dict(schema)


def _unwrap_nullable(schema: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    resolved = _resolve_schema(schema, document)
    union_key = "anyOf" if "anyOf" in resolved else "oneOf" if "oneOf" in resolved else None
    if union_key is None:
        return resolved
    branches = [_resolve_schema(branch, document) for branch in resolved[union_key]]
    non_null = [branch for branch in branches if branch.get("type") != "null"]
    if len(non_null) != 1 or len(non_null) == len(branches):
        return resolved
    siblings = {key: value for key, value in resolved.items() if key != union_key}
    return {**non_null[0], **siblings}


def _type_signature(schema: Mapping[str, Any], document: Mapping[str, Any]) -> frozenset[str]:
    resolved = _resolve_schema(schema, document)
    for union_key in ("anyOf", "oneOf"):
        if union_key in resolved:
            return frozenset(
                item for branch in resolved[union_key] for item in _type_signature(branch, document)
            )
    schema_type = resolved.get("type", "object" if "properties" in resolved else None)
    return frozenset({str(schema_type)}) if schema_type is not None else frozenset()


def _compare_schema(
    base_schema: Mapping[str, Any],
    current_schema: Mapping[str, Any],
    base_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    location: str,
    mode: str,
    issues: list[str],
) -> None:
    base_signature = _type_signature(base_schema, base_document)
    current_signature = _type_signature(current_schema, current_document)
    if base_signature != current_signature:
        issues.append(
            f"changed type at {location}: {sorted(base_signature)!r} -> "
            f"{sorted(current_signature)!r}"
        )
        return
    base = _unwrap_nullable(base_schema, base_document)
    current = _unwrap_nullable(current_schema, current_document)
    base_type = base.get("type", "object" if "properties" in base else None)
    base_enum = set(base.get("enum", []))
    current_enum = set(current.get("enum", []))
    removed_enum_values = base_enum - current_enum if base_enum and current_enum else set()
    if removed_enum_values:
        issues.append(f"narrowed enum at {location}: removed {sorted(removed_enum_values)!r}")
    if base_type == "array":
        _compare_schema(
            base.get("items", {}),
            current.get("items", {}),
            base_document,
            current_document,
            f"{location}[]",
            mode,
            issues,
        )
        return
    if base_type != "object":
        return
    base_properties = base.get("properties", {})
    current_properties = current.get("properties", {})
    for name, base_property in sorted(base_properties.items()):
        if name not in current_properties:
            issues.append(f"removed {mode} field {location}.{name}")
            continue
        _compare_schema(
            base_property,
            current_properties[name],
            base_document,
            current_document,
            f"{location}.{name}",
            mode,
            issues,
        )
    if mode == "request":
        newly_required = set(current.get("required", [])) - set(base.get("required", []))
        for name in sorted(newly_required):
            issues.append(f"new required request field {location}.{name}")


def _load_ref(ref: str, artifact: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={Path.cwd().as_posix()}",
            "show",
            f"{ref}:{artifact.as_posix()}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", type=Path, help="Base OpenAPI JSON file.")
    source.add_argument("--base-ref", help="Git ref containing the base OpenAPI artifact.")
    parser.add_argument("--current", type=Path, default=Path("openapi/openapi.json"))
    parser.add_argument(
        "--allow-breaking",
        default=os.getenv("OPENAPI_BREAK_OVERRIDE"),
        help="Intentional-break override, formatted ADR-NNNN; also requires an API version bump.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    current = json.loads(args.current.read_text(encoding="utf-8"))
    base = (
        json.loads(args.base.read_text(encoding="utf-8"))
        if args.base is not None
        else _load_ref(args.base_ref, args.current)
    )
    issues = compare_openapi(base, current)
    if not issues:
        print("OpenAPI compatibility check passed: no breaking changes detected.")
        return 0
    base_version = str(base.get("info", {}).get("version", ""))
    current_version = str(current.get("info", {}).get("version", ""))
    if breaking_override_allowed(base, current, args.allow_breaking):
        print(
            f"OpenAPI breaking changes explicitly allowed by {args.allow_breaking}; "
            f"version {base_version} -> {current_version}."
        )
        for issue in issues:
            print(f"- {issue}")
        return 0
    print("OpenAPI compatibility check failed:", file=sys.stderr)
    for issue in issues:
        print(f"- {issue}", file=sys.stderr)
    print(
        "Intentional breaks require an API version bump and --allow-breaking ADR-NNNN.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
