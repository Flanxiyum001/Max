"""Host-owned GitHub MCP tool selection and bounded input validation.

The catalogue is permission metadata, never permission to execute. The remote
server must actually advertise each tool and the owner must approve each call.
"""

from __future__ import annotations

import hashlib
import json
from importlib import import_module
from typing import Any

GITHUB_MCP_ENDPOINT = "https://api.githubcopilot.com/mcp/"

GITHUB_INTERACTIVE = (
    "create_pull_request",
    "get_me",
    "issue_write",
    "update_pull_request",
)
GITHUB_READ_ONLY = (
    "get_commit",
    "get_file_contents",
    "get_label",
    "get_latest_release",
    "get_release_by_tag",
    "get_tag",
    "get_team_members",
    "get_teams",
    "issue_read",
    "list_branches",
    "list_commits",
    "list_issue_fields",
    "list_issue_types",
    "list_issues",
    "list_pull_requests",
    "list_releases",
    "list_repository_collaborators",
    "list_tags",
    "pull_request_read",
    "run_secret_scanning",
    "search_code",
    "search_commits",
    "search_issues",
    "search_pull_requests",
    "search_repositories",
    "search_users",
)
GITHUB_WRITE = (
    "add_comment_to_pending_review",
    "add_issue_comment",
    "add_reply_to_pull_request_comment",
    "create_branch",
    "create_or_update_file",
    "create_repository",
    "delete_file",
    "fork_repository",
    "merge_pull_request",
    "pull_request_review_write",
    "push_files",
    "request_copilot_review",
    "sub_issue_write",
    "update_issue_comment",
    "update_pull_request_branch",
)
GITHUB_TOOLS = frozenset(GITHUB_INTERACTIVE + GITHUB_READ_ONLY + GITHUB_WRITE)
# get_me is an identity lookup even though it was supplied in the interactive group.
GITHUB_READS = frozenset((*GITHUB_READ_ONLY, "get_me"))
MAX_GITHUB_ARGUMENT_BYTES = 8192


def github_headers() -> dict[str, str]:
    """Use the same fixed selection in discovery and execution.

    MAX has its own exact-argument approval UI, not an embedded MCP Apps host.
    """
    return {
        "X-MCP-Tools": ",".join(sorted(GITHUB_TOOLS)),
        "X-MCP-Toolsets": "secret_protection",
        "X-MCP-Features": "mcp_apps_disable_form_deferral",
    }


class GitHubSchemaError(ValueError):
    """Stable, payload-free schema or argument error."""


def _bounded(value: Any, *, depth: int = 0, schema: bool = False) -> None:
    if depth > 16:
        raise GitHubSchemaError("github_input_too_deep")
    if isinstance(value, dict):
        if len(value) > 128:
            raise GitHubSchemaError("github_input_too_large")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise GitHubSchemaError("github_invalid_key")
            if schema and key in {"$ref", "$dynamicRef", "$recursiveRef", "$schema"}:
                # Do not permit remote reference resolution or dialect selection.
                raise GitHubSchemaError("github_unsupported_schema")
            if schema and key in {"pattern", "patternProperties"}:
                # Avoid executing provider-supplied regexes in this host.
                raise GitHubSchemaError("github_unsupported_schema")
            _bounded(child, depth=depth + 1, schema=schema)
    elif isinstance(value, list):
        if len(value) > 128:
            raise GitHubSchemaError("github_input_too_large")
        for child in value:
            _bounded(child, depth=depth + 1, schema=schema)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise GitHubSchemaError("github_invalid_value")


def _validator(schema: dict[str, Any]) -> Any:
    # jsonschema is an installed dependency of the MCP SDK. Use the fixed
    # Draft 2020-12 validator, without reference fetching or format callbacks.
    return import_module("jsonschema").Draft202012Validator(schema)


def reviewed_github_schema(encoded: str, digest: str) -> dict[str, Any]:
    """Accept only bounded canonical schemas observed from the official endpoint."""
    try:
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise ValueError
        schema = json.loads(encoded)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError
        if json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False) != encoded:
            raise ValueError
        props = schema.get("properties", {})
        if not isinstance(props, dict) or not set(schema.get("required", [])) <= props.keys():
            raise ValueError
        _bounded(schema, schema=True)
        # Regardless of the server's top-level additionalProperties default,
        # model arguments can only use individually observed property names.
        schema["additionalProperties"] = False
        validator = _validator(schema)
        validator.check_schema(schema)
        return schema
    except Exception:
        raise GitHubSchemaError("github_unsupported_schema") from None


def validate_github_arguments(encoded: str, digest: str, arguments: object) -> dict[str, Any]:
    """Validate the exact JSON value that will be displayed and dispatched."""
    schema = reviewed_github_schema(encoded, digest)
    if not isinstance(arguments, dict):
        raise GitHubSchemaError("github_invalid_arguments")
    try:
        _bounded(arguments)
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        # The web approval surface uses ensure_ascii=True and includes spaces.
        if len(json.dumps(arguments, sort_keys=True, ensure_ascii=True)) > MAX_GITHUB_ARGUMENT_BYTES:
            raise ValueError
        clean = json.loads(canonical)
        _validator(schema).validate(clean)
        if not isinstance(clean, dict):
            raise ValueError
        return clean
    except Exception:
        raise GitHubSchemaError("github_invalid_arguments") from None


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Omit remote prose/examples from model catalogue; retain structural constraints."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    {name: strip(prop) for name, prop in child.items()}
                    if key in {"properties", "$defs", "dependentSchemas"}
                    and isinstance(child, dict)
                    else strip(child)
                )
                for key, child in value.items()
                if key not in {"description", "title", "examples", "default", "$comment"}
            }
        if isinstance(value, list):
            return [strip(child) for child in value]
        return value

    result = strip(schema)
    assert isinstance(result, dict)
    if len(json.dumps(result, ensure_ascii=True)) > 12_000:
        raise GitHubSchemaError("github_unsupported_schema")
    return result
