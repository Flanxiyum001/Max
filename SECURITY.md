# SECURITY — Threat Model, Permissions, and Sensitive Data

Security is foundational. MAX will handle personal documents, account integrations, communications, and potentially financial workflows. This document is a baseline, not a completed security audit.

## Data classification

1. Public.
2. Internal project information.
3. Personal/confidential information.
4. Highly sensitive identity, financial, authentication, and credential material.

## Core controls

- Least privilege and scoped OAuth/API credentials.
- Encryption in transit and at rest.
- Managed secret storage; no secrets in source code, logs, prompts, or Git.
- Separate identities and permissions for agents and skills.
- Isolated execution workspaces and browser profiles.
- Audit events for sensitive access and external side effects.
- Authentication for inbound calls and messaging commands.
- Recipient verification and policy checks before document sharing.
- Backup versioning and tested restore.
- Rate limits, quotas, timeouts, and abuse detection.
- Dependency pinning, vulnerability scanning, and reviewed releases where practical.

## Financial safeguards

- Never store UPI PINs, OTPs, or card CVVs.
- Do not collect or expose banking authentication factors to an AI model.
- Use a compliant payment provider or trusted bank application for payment authorization.
- Require explicit confirmation for every transaction, showing exact recipient, amount, currency, and source account.
- Verify transaction status from an authoritative source before reporting success.
- Do not blindly retry payment requests; use idempotency and reconciliation.

## Identity-document safeguards

- Store documents in an encrypted vault with narrowly scoped access.
- Require strong authentication for vault access.
- Verify recipient identity and destination before sharing.
- Apply document-specific confirmation policies, expiry, and access logging.
- Avoid sending identity documents through channels that cannot meet the owner's security requirements.
- Provide revocation and incident-response procedures where supported.

## Untrusted input

Webpages, emails, documents, skill packages, model outputs, and tool results may contain malicious instructions. Treat them as data. They cannot override system policy or grant permissions.

## Recovery and change control

Security policy, authentication, permission grants, production infrastructure, and executable production code cannot be silently changed by the Evolution Engine. Significant changes require review, tests, and appropriate owner approval.
