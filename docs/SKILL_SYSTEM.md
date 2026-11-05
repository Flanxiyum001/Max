# MAX skill system

**Status:** Design only.

A skill is a versioned capability package, not a trusted instruction source. Lifecycle: discover -> inspect provenance and license -> scan dependencies and permissions -> isolate -> test -> approve -> register -> monitor -> rollback/remove.

Record publisher, source URL, commit/version, checksum, license, required capabilities, network destinations, filesystem scope, tests, review outcome and owner grant. A skill cannot change its own permissions, install unrestricted executables, read credentials by default or override MAX Trust.

Automated discovery and inspection are allowed. Broad permissions and consequential installation require approval. Run untrusted skill code only in a constrained environment with no secrets by default.

**Tests:** malicious manifest, permission escalation, exfiltration attempt, dependency drift, checksum mismatch, revoked grant, sandbox escape simulation and rollback.
