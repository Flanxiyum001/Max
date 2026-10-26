"""Autonomous self-improvement: policy-guarded, test-gated, reversible."""

from .analysis import Finding, scan_python_file, scan_repo  # noqa: F401
from .pipeline import (  # noqa: F401
    ImproveError,
    Proposal,
    approve,
    list_reports,
    load_report,
    rollback,
    run,
    run_test_suite,
)
from .policy import Policy, PolicyIntegrityError, PolicyError, write_integrity  # noqa: F401

__all__ = [
    "Finding",
    "ImproveError",
    "Policy",
    "PolicyError",
    "PolicyIntegrityError",
    "Proposal",
    "approve",
    "list_reports",
    "load_report",
    "rollback",
    "run",
    "run",
    "run_test_suite",
    "scan_python_file",
    "scan_repo",
    "write_integrity",
]
