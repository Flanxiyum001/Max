# MAX Core — Phase 1 bootstrap (local development only)

**Scope:** This PR adds a narrow owner-authenticated, local-only API and validation scaffolding. This is NOT MAX's full agent, persistent task runner, security hardening, a deployed server, a cloud service, or a remote-ready login system.

## Quickstart (Python 3.11+)

1. Create a dedicated virtual environment and install dependencies:

   ```sh
   python -m venv .venv
   # Activate the venv for your operating system, then:
   python -m pip install -e '.[dev]'
   ```

2. Generate a new token **locally** in the terminal and set it as an environment variable. Never store it in Git, screenshots, log outputs, MAX memory, an `.env` file shared with someone else, or a PR. For example (Linux/Arch):

   ```sh
   export MAX_API_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
   ```

   PowerShell (Windows):

   ```powershell
   $env:MAX_API_BEARER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

3. Start in a trusted development machine's local terminal:

   ```sh
   python -m max_core
   ```

   The listener is **only** `127.0.0.1:8765` by default. The server rejects missing/weak tokens, non-loopback bind hosts and invalid ports. There is deliberately no reverse proxy, CORS permission, browser session or exposed API docs.

4. `GET http://127.0.0.1:8765/health/live` returns `{"status":"alive"}` and `/health/ready` returns `{"status":"ready","scope":"bootstrap-only"}`. The readiness claim means only the **bootstrap process/config** is live, not that a database, model or device is ready. Owner-only routes `/v1/session` and `/v1/status` require an exact `Authorization: Bearer <your-token>` header.

## Verification commands

```sh
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src/max_core
python -m pytest -q
python -m pip_audit --progress-spinner off
```

The workflow runs these checks on PRs against `max` for Python 3.11 and 3.13. A vulnerability scan requires access to an advisory service and may fail closed. A green workflow is necessary but does not establish production safety.

## Security boundaries and limitations

- This is a **single shared bearer token** for **local development only**. It is not multi-user authentication, a hardened session manager, OAuth, MFA or a remote-device identity system. Anyone with the token can call owner routes. Do **not** expose this listener via tunnels, LAN bindings, port forwarding, public DNS, or a Railway deployment.
- Server start fails without explicit high-entropy token; the token is never embedded in source. FastAPI interactive docs/OpenAPI are disabled. No permissive CORS middleware or browser cookies.
- Request logs are a strict JSON field allowlist: random request ID, HTTP method, route **template**, and status; never authorization headers, query params, response bodies or raw URLs. Logging may still be performed by infrastructure outside MAX; do not send sensitive prompts to this prototype.
- The `TrustedHostMiddleware` checks HTTP host headers as defense in depth, **not** as a network firewall. The CLI listener enforces loopback; do not bypass it with an ASGI server bound to a public interface.
- Credentials and requests are not persistent or encrypted at rest because this milestone stores **no user state**. Health endpoints are unauthenticated but return only minimal process status.
- CI uses semver-bounded dependency ranges; exact transitive lockfile, reproducible SBOM, remote authentication, local keychain integration, TLS ingress, secrets manager, and full SAST belong to a later hardened milestone before any deployment.

## Next PR boundaries

Milestone 2: PostgreSQL task state, migrations, checkpoints, cancellations, transactional outbox and idempotency/effect reconciliation. Milestone 3: gateway with two model transport formats. Milestone 4: policy-driven MAX Trust and safe tools. No autonomous device execution, voice, telephony, vault, financial operations, bot changes or production deployment is authorized here.

## Rollback

Revert this PR; no migrations, data, hosted services or persistent side effects exist. Stop any manually launched local development process before reverting.


### Optional local PostgreSQL task mode (milestone 2)

Task persistence is optional during local bootstrap but required to use `/v1/tasks`. Follow [TASK_PERSISTENCE.md](TASK_PERSISTENCE.md) to create your own **local** PostgreSQL database, set `MAX_DATABASE_URL` privately and run Alembic migrations. Do not expose the token-authenticated local server to the network. `/health/ready` will return 503 rather than claim durable readiness if the schema or DB is unavailable.
