# MAX PR Sandbox — isolated, free-first PR validation

MAX's hosted orb can request temporary GitHub-hosted PR sandboxes **after the owner approves each run**. It does not run pull request code on the Render web server, and a successful sandbox cannot merge, push, or deploy anything.

## Owner flow

Say or type: **"MAX, review PR #69"** (defaults to `Flanxiyum001/Max`) or **"MAX, test PR #18 in Flanxiyum001/Ryoko"**.

MAX:

1. Checks that GitHub is connected using the existing MAX Apps connection (no extra sandbox token or environment variable).
2. Uses GitHub's API to confirm that the PR is open, belongs to a repository **owned by the connected account**, and is **not from a fork**.
3. Confirms that the trusted sandbox workflow exists on that repository's default branch; pins the PR's exact SHA.
4. Shows **Approve / Deny** with the owner-visible repository, PR number, SHA, and operation description.
5. On approval alone, dispatches `.github/workflows/max-pr-sandbox.yml` on the trusted default branch, using a GitHub workflow API request with run details. If the PR head changed since the approval preview, dispatch is refused.
6. Returns a GitHub Actions run URL (where supported), which can be opened from the orb. The runner produces its own Actions summary and a tiny downloadable validation report.

The workflow:

- starts a **fresh GitHub-hosted Ubuntu VM** with a temporary PostgreSQL test container;
- checks out the exact approved SHA with `persist-credentials: false`;
- executes fixed, repository-language-specific checks for Python, Rust or Node.js; unsupported project shapes fail explicitly;
- has **read-only** GitHub permissions, no production secrets, and no deploy/push/merge instructions;
- has an 18-minute upper runtime limit and two-day report retention;
- terminates after testing. No durable virtual machine or Docker daemon is needed on Render.

The first workflow template is installed in the MAX repo. To use the same sandbox with other repositories (Ryoko, Aventas, etc.), install the reviewed workflow file into **each target repository's default branch**. The backend chooses the target repository and PR only after the owner requests it; arbitrary model-provided script commands are not run.

## What is free — and what is not guaranteed

- **Public repositories:** GitHub standard hosted Actions runners are free, subject to GitHub's abuse/fair-use restrictions.
- **Private repositories:** GitHub provides an included monthly Actions usage allowance. Running out of free quota can stop jobs or incur charges **depending on your account's spending limit and billing settings**. To target strictly ₹0, set an Actions budget/limit accordingly and avoid extra-large runners.
- The MAX Render service does not rent a separate sandbox worker, run Docker-in-Docker or need a second server.
- GitHub may require repo Actions permissions and GitHub OAuth/installation access; this workflow does not bypass them.

[GitHub Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage) · [GitHub Actions security](https://docs.github.com/en/actions/reference/security/secure-use)

## Security boundaries

This is an *ephemeral CI testing environment*, not a perfectly sealed security container. PR code can execute within the temporary runner, and third-party dependencies may make network requests. **Do not include production credentials or privileged GitHub tokens** in the sandbox job. Do not give it access to home folders, connected MAX credentials, or Render services. A malicious PR could still attempt to exfiltrate source code already checked out in its sandbox, so restrict to trusted owner repositories in v1.

Never run PR code using `pull_request_target` with secrets or write permissions. The sandbox's workflow definition must live on the trusted default branch, and only the PR **source commit** is checked out. The job checks the exact head SHA again before checkout; changing the PR after owner review requires a new run.

A green sandbox means its configured checks passed; it **does not** prove that the code is free of vulnerabilities, logical errors, or malicious behavior. Human review plus approvals are still mandatory before a merge.

## Next iterations

This first stage handles **read-only validation** and results. A full autonomous coding-agent sandbox that edits files, proposes patches, creates PRs, tests them, and then requests a separate explicit merge approval would require a controlled writable branch/patch publication pipeline. It must remain separate from the no-secret test runner and requires stronger change provenance and security review.

Before testing through Render, PR #69 and the sandbox workflow must be merged into the default branch and deployed. GitHub OAuth setup remains the prerequisite described in [MCP_CONNECTION_HUB.md](MCP_CONNECTION_HUB.md).
