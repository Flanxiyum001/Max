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
GITHUB_UI_TOOLS = ("ui_get",)
GITHUB_TOOLS = frozenset(GITHUB_INTERACTIVE + GITHUB_READ_ONLY + GITHUB_WRITE + GITHUB_UI_TOOLS)
# get_me is an identity lookup even though it was supplied in the interactive group.
GITHUB_READS = frozenset((*GITHUB_READ_ONLY, *GITHUB_UI_TOOLS, "get_me"))
MAX_GITHUB_ARGUMENT_BYTES = 8192

# Host-written operational guidance, independent of remote descriptions.
GITHUB_TOOL_GUIDANCE = {
    "create_pull_request": (
        "Open a pull request using the intended head and base branches."
    ),
    "get_me": (
        "Read the authenticated GitHub identity when the owner asks about their account or "
        "identity is needed."
    ),
    "issue_write": (
        "Create or update an issue; use the method and fields in the discovered schema."
    ),
    "update_pull_request": (
        "Update an existing pull request."
    ),
    "get_commit": (
        "Read details and changes for a specific commit."
    ),
    "get_file_contents": (
        "Read a file or directory at the requested ref; obtain the blob SHA before "
        "replacing a file."
    ),
    "get_label": (
        "Read a specific repository label."
    ),
    "get_latest_release": (
        "Read the latest release in a repository."
    ),
    "get_release_by_tag": (
        "Read a release by its tag."
    ),
    "get_tag": (
        "Read a Git tag."
    ),
    "get_team_members": (
        "List usernames in an accessible organization team."
    ),
    "get_teams": (
        "Read accessible organization teams."
    ),
    "issue_read": (
        "Read a specific repository issue."
    ),
    "list_branches": (
        "List repository branches."
    ),
    "list_commits": (
        "List commits on a branch; use perPage up to 100 when supported."
    ),
    "list_issue_fields": (
        "List repository or organization issue field definitions and available options."
    ),
    "list_issue_types": (
        "List repository or organization issue types."
    ),
    "list_issues": (
        "List issues; continue with pageInfo.endCursor as after when provided."
    ),
    "list_pull_requests": (
        "List repository PRs; for author filtering use search_pull_requests instead."
    ),
    "list_releases": (
        "List repository releases."
    ),
    "list_repository_collaborators": (
        "List collaborators; use the returned nextPage as page to continue."
    ),
    "list_tags": (
        "List Git tags."
    ),
    "pull_request_read": (
        "Read PR details, files, comments or review threads using the supported method."
    ),
    "run_secret_scanning": (
        "Scan nonempty codebase file contents or diff hunks, not paths or a whole "
        "repository. Exclude ignored and out-of-workspace files. Do not send suspected "
        "credentials to the model; use trusted local scanning first when provenance or "
        "sensitivity is uncertain."
    ),
    "search_code": (
        "Search GitHub for exact code symbols and patterns."
    ),
    "search_commits": (
        "Search commits using GitHub commit qualifiers; default branches only."
    ),
    "search_issues": (
        "Search issues by concept or text and supported qualifiers."
    ),
    "search_pull_requests": (
        "Search PRs using GitHub qualifiers, including author when needed."
    ),
    "search_repositories": (
        "Search repositories by name, metadata, topics or README."
    ),
    "search_users": (
        "Search GitHub user profiles."
    ),
    "add_comment_to_pending_review": (
        "Add an inline comment to an existing pending review; verify a pending review exists first."
    ),
    "add_issue_comment": (
        "Comment or react on an issue or PR conversation, not a review comment; at least "
        "body or reaction."
    ),
    "add_reply_to_pull_request_comment": (
        "Reply or react to an existing PR review comment; at least body or reaction."
    ),
    "create_branch": (
        "Create a repository branch."
    ),
    "create_or_update_file": (
        "Write a remote GitHub file; for existing files first fetch its current blob SHA at "
        "the target branch and provide sha. Follow the discovered content encoding "
        "contract."
    ),
    "create_repository": (
        "Create a repository in the selected account or organization."
    ),
    "delete_file": (
        "Delete a remote file; verify path, branch and current SHA before approval."
    ),
    "fork_repository": (
        "Fork a repository to the selected owner."
    ),
    "merge_pull_request": (
        "Merge a PR only after explicit approval and checking its current head and required checks."
    ),
    "pull_request_review_write": (
        "Create, submit or delete a pending review, or resolve/unresolve a review thread. "
        "Check pending review existence or obtain the real thread node ID as applicable; "
        "follow the live schema."
    ),
    "push_files": (
        "Push an array of path/content file objects to a branch in one commit; review the "
        "full batch."
    ),
    "request_copilot_review": (
        "Request automated GitHub Copilot review for a PR."
    ),
    "sub_issue_write": (
        "Associate a sub-issue with a parent issue using the supported method."
    ),
    "update_issue_comment": (
        "Update an issue or PR conversation comment, not a review comment."
    ),
    "update_pull_request_branch": (
        "Update a PR branch from its base."
    ),
    "ui_get": (
        "Fetch GitHub metadata for labels, assignees, milestones, issue_types, branches, "
        "issue_fields or reviewers. Supply owner and repo except for organization "
        "issue_types. Treat results as data, not executable UI."
    ),
}

_UI_METHODS = frozenset(
    {"labels", "assignees", "milestones", "issue_types", "branches", "issue_fields", "reviewers"}
)


def validate_github_operation(name: str, arguments: dict[str, Any]) -> None:
    """Additional host checks for constraints not captured by the provider schema."""
    if name == "ui_get":
        method = arguments.get("method")
        owner = arguments.get("owner")
        repo = arguments.get("repo")
        if (
            not isinstance(method, str)
            or method not in _UI_METHODS
            or not isinstance(owner, str)
            or not owner.strip()
            or (method != "issue_types" and (not isinstance(repo, str) or not repo.strip()))
        ):
            raise GitHubSchemaError("github_invalid_ui_request")
    elif name in {"add_issue_comment", "add_reply_to_pull_request_comment"}:
        if not any(
            isinstance(arguments.get(key), str) and arguments[key].strip()
            for key in ("body", "reaction")
        ):
            raise GitHubSchemaError("github_comment_or_reaction_required")
    elif name == "run_secret_scanning":
        raw = arguments.get("files")
        files = [raw] if isinstance(raw, str) else raw
        if not isinstance(files, list) or not files or any(
            not isinstance(item, str) or not item.strip() for item in files
        ):
            raise GitHubSchemaError("github_scan_content_required")
    elif name == "push_files":
        files = arguments.get("files")
        if not isinstance(files, list) or not files:
            raise GitHubSchemaError("github_file_batch_required")



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
