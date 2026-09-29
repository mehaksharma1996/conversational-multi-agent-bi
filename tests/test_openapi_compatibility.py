"""Tests for the lightweight OpenAPI breaking-change gate."""

from __future__ import annotations

from scripts.check_openapi_compatibility import breaking_override_allowed, compare_openapi


def _document(request_schema: dict | None = None, response_schema: dict | None = None) -> dict:
    operation: dict = {
        "responses": {
            "200": {
                "content": {"application/json": {"schema": response_schema or {"type": "object"}}}
            }
        }
    }
    if request_schema is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": request_schema}},
        }
    return {
        "info": {"version": "1.0.0"},
        "paths": {"/widgets": {"post": operation}},
        "components": {"schemas": {}},
    }


def test_additive_fields_and_operations_are_compatible() -> None:
    base = _document(response_schema={"type": "object", "properties": {"id": {"type": "string"}}})
    current = _document(
        response_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
        }
    )
    current["paths"]["/widgets"]["get"] = {"responses": {"200": {"description": "ok"}}}

    assert compare_openapi(base, current) == []


def test_removed_operation_and_response_field_are_breaking() -> None:
    base = _document(
        response_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
        }
    )
    current = _document(
        response_schema={"type": "object", "properties": {"id": {"type": "string"}}}
    )

    assert compare_openapi(base, {**current, "paths": {}}) == ["removed path /widgets"]
    assert any("removed response field" in issue for issue in compare_openapi(base, current))


def test_new_required_request_field_changed_type_and_narrowed_enum_are_breaking() -> None:
    base = _document(
        request_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["name"],
        }
    )
    current = _document(
        request_schema={
            "type": "object",
            "properties": {
                "name": {"type": "integer"},
                "kind": {"type": "string", "enum": ["a"]},
            },
            "required": ["name", "kind"],
        }
    )

    issues = compare_openapi(base, current)

    assert any("changed type" in issue for issue in issues)
    assert any("narrowed enum" in issue for issue in issues)
    assert any("new required request field" in issue for issue in issues)


def test_component_references_are_resolved() -> None:
    base = _document(response_schema={"$ref": "#/components/schemas/Widget"})
    current = _document(response_schema={"$ref": "#/components/schemas/Widget"})
    base["components"]["schemas"]["Widget"] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    current["components"]["schemas"]["Widget"] = {
        "type": "object",
        "properties": {},
    }

    assert any("removed response field" in issue for issue in compare_openapi(base, current))


def test_breaking_override_requires_adr_reference_and_version_bump() -> None:
    base = _document()
    current = _document()

    assert not breaking_override_allowed(base, current, "ADR-0012")
    current["info"]["version"] = "2.0.0"
    assert not breaking_override_allowed(base, current, "approved")
    assert breaking_override_allowed(base, current, "ADR-0012")


def test_changed_type_inside_nullable_union_is_breaking() -> None:
    base = _document(
        request_schema={
            "type": "object",
            "properties": {"limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
        }
    )
    current = _document(
        request_schema={
            "type": "object",
            "properties": {"limit": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
    )

    assert any("changed type" in issue for issue in compare_openapi(base, current))
