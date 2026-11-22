M35: Sandboxed Code Execution + Allowlisted stdio MCP Bridge

Status: implemented (this milestone)

## Goal

Unlock two previously-excluded capabilities without weakening the trust
model: (1) LLM-generated Python can run, but only inside an ephemeral,
network-disabled, read-only container with a scratch directory; and (2)
local stdio MCP servers (Git, SQLite, filesystem, ...) can be used, but
only through a host-pinned binary allowlist, a stripped child environment,
and the same per-call Trust approval gate as remote MCP tools.

## Sandboxed Python runner (`code.execute_python`)

- New module `max_core/container_runner.py`. `LocalToolRegistry` gains one
  spec: `code.execute_python`, risk tier R2 (`r2_write`), requiring a fresh
  single-use owner approval like every write-class tool.
- Parameters: exactly `{"script": str}`, 1..32,000 chars, valid UTF-8, no
  NUL bytes. Everything else is denied with `invalid_action_parameters`.
- Every run builds a fresh scratch directory under `<workspace>/.max-scratch/run-<uuid>/`,
  writes the validated script as `/work/script.py`, and executes:

      docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --memory 256m --cpus 1.0 \
        --pids-limit 64 --tmpfs /tmp:rw,size=8m,noexec,nodev,nosuid \
        -v <scratch>:/work:rw -w /work -u 65534:65534 \
        python:3.12-slim python -I -B /work/script.py

  - No network at all (`--network none`).
  - Root filesystem read-only; only `/work` and the 8 MB no-exec `/tmp`
    tmpfs are writable.
  - All Linux capabilities dropped, no privilege escalation, 256 MB memory,
    1 CPU, 64 processes.
  - Runs as uid/gid 65534 (`nobody`), Python isolated mode (`-I`) with no
    bytecode writing (`-B`).
- The host environment passed to Docker is the bounded set from
  `bounded_commands` (PATH, HOME, system root, temp dirs, locale only), so
  host API keys are never exposed to the container.
- Images are pinned to a frozenset (`python:3.11-slim`, `python:3.12-slim`);
  no registry tags are accepted from model input.
- Fail-closed: when Docker is missing the call returns `tool_unavailable`;
  there is no host-interpreter fallback. Fixed codes: `invalid_script`,
  `invalid_script_image`, `tool_unavailable`, `tool_timeout`, `tool_failed`.
- The scratch directory is removed in a `finally` block; created files are
  permission-walked so a container-written read-only file cannot block
  cleanup. `.max-scratch` is excluded from `fs.list_files`/`fs.read_file`
  exposure the same way as `.git` and `secrets`.
- Timeout: 120 seconds, enforced by the bounded command runner.

## Allowlisted stdio MCP bridge

- New module `max_core/mcp_stdio.py` with `StdioMCPConnection` and
  `allowlisted_stdio_connection(...)`:
  - The command must resolve to a host-provided allowlist entry. An empty
    allowlist denies everything (`stdio_not_allowed`); an unmatched command
    is denied (`stdio_command_not_allowed`). Bare basenames can only ever
    resolve to an allowlisted absolute path.
  - Connection args are plain validated strings (max 8 args, 256 chars
    each). No shell anywhere; the MCP SDK spawns the process directly.
  - The child environment is rebuilt from scratch: only the safe bounded
    keys (`PATH`, `HOME`, `SYSTEMROOT`, ..., `LANG`) are forwarded, so host
    API keys are never inherited by stdio servers.
- Discovery via `discover_stdio_tools` is bounded (30 s deadline, max 512
  tools, max 16 pages), fails closed on non-approved connections
  (`mcp_discovery_not_approved`), and reuses the same observation rules as
  remote discovery: canonical schema JSON, sha256 pin, readOnlyHint.
- Desktop profile (`mcp_desktop.py`) gains stdio support:
  - `add_stdio(ident, command, args=[])` may only add a binary listed in
    the `MAX_MCP_STDIO_BINARIES` host environment variable (an
    `os.pathsep`-separated list of absolute paths). The variable missing or
    malformed denies with `stdio_not_allowed`; unmatched commands deny with
    `stdio_command_not_allowed`. The profile stores the resolved absolute
    path plus fixed args; no tokens are persisted for stdio entries.
  - Each stdio connection stores its own `discovery_approved` flag; the
    owner runs discovery explicitly, as with endpoints.
  - `discover`/`review`/`runtime` are kind-aware. At runtime the exact
    binary is re-checked against the *current* allowlist, so shrinking the
    allowlist fails closed on previously-added profiles.
  - Profile shape stays version 1: endpoint rows remain unchanged;
    stdio rows carry `kind: "stdio"`, `command`, `args`.
- Execution path (`mcp_execute.py`): dotted names stay
  `mcp.<connection_id>.<tool_name>`; connection ids remain
  dot-free. The executor accepts both `MCPConnection` and
  `StdioMCPConnection`; stdio connections dispatch over a per-call
  `stdio_client` session and HTTPS connections over the bounded
  `EndpointTransport`. Every call still requires the exact
  single-use Trust approval bound to tool, owner, actor, session, device,
  task, call id, and canonical arguments, and still fails closed on any
  observed-schema drift.

## Windows note

Docker Desktop works for both features on Windows. Windows Sandbox is not
used; if Docker is absent the sandboxed runner denies with
`tool_unavailable` instead of running anything on the host.

## Tests

- `tests/test_mcp_stdio.py`: real newline-delimited JSON-RPC stdio fixture
  server; discovery, pagination caps, environment scrubbing, schema-drift
  denial, and a full Trust end-to-end flow (approval -> decide ->
  completed).
- `tests/test_local_tools_dev.py`: `code.execute_python` R2 approval
  gating, hardened argv assertions via a stub `docker` binary, environment
  scrubbing, scratch cleanup, and `tool_unavailable` when Docker is absent.
- `tests/test_mcp_desktop.py`: stdio profile add/discover/review/runtime
  and allowlist fail-closed behavior.
