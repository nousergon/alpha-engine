#!/usr/bin/env python3
"""check-no-foreign-writers.py — Ensure codified IAM roles have exactly
one writer.

A "codified" role is any directory under `infrastructure/iam/<role>/`.
For each such role, scan a configurable set of repository checkouts for
`aws iam put-role-policy --role-name <role>` (or its boto3/yaml/json
equivalents). The codified policy + `apply.sh` is the only sanctioned
writer; any other reference is a regression risk and fails the check.

Why: the alpha-engine system has hit four IAM-clobber incidents in two
months, all rooted in a second writer racing the codified policy
(EB-SFN role 2026-04-21 + 2026-05-04 + 2026-05-06; SF role 2026-05-04
EOD + 2026-05-06 morning). PR #136 closed the EB-SFN twin; PR #151
closed one of two SF-role twins; this check catches the next one
before it merges.

Scope:
  - Files scanned: bash deploy scripts (`*.sh`) + CloudFormation YAML
    + python scripts under `infrastructure/`. Skips `apply.sh` (which
    legitimately writes the codified state) and `check-drift.py` (which
    only reads).
  - Repos scanned: passed via --repo (defaults to the parent of this
    repo's directory). One invocation can cover the alpha-engine
    sibling layout we use locally + in CI.

Usage:
  ./infrastructure/iam/check-no-foreign-writers.py
  ./infrastructure/iam/check-no-foreign-writers.py --repo ~/Development/alpha-engine-data
  ./infrastructure/iam/check-no-foreign-writers.py --repo ~/Development/alpha-engine --repo ~/Development/alpha-engine-data
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

# Files that are allowed to write codified roles.
ALLOWED_WRITERS = {"apply.sh"}

# File extensions to scan for writes.
SCAN_EXTENSIONS = {".sh", ".yaml", ".yml", ".py", ".tf"}

# Skip these directories anywhere in the path.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"}


def _codified_roles() -> set[str]:
    """Return the set of role names codified under this directory."""
    return {p.name for p in SCRIPT_DIR.iterdir() if p.is_dir()}


def _scan_file(path: Path, role_names: set[str]) -> list[str]:
    """Return list of (role_name, line_no, snippet) tuples flagged in this file."""
    findings: list[str] = []
    try:
        text = path.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return findings

    write_patterns = [
        r"put-role-policy",
        r"attach-role-policy",
        r"delete-role-policy",
        r"put_role_policy",
        r"attach_role_policy",
        r"AWS::IAM::RolePolicy\b",
        r"aws_iam_role_policy\b",
    ]
    write_re = re.compile("|".join(write_patterns))

    # Skip pure comment lines for languages where leading `#` (sh, py, yaml)
    # or `//` (terraform) is the comment marker. The check is about real
    # code paths, not docstrings or commit-message-style banners.
    comment_re = re.compile(r"^\s*(#|//)")

    for lineno, line in enumerate(text.splitlines(), 1):
        if comment_re.match(line):
            continue
        if not write_re.search(line):
            continue
        # The match is a write — figure out which role(s) appear in
        # nearby context (same line or lookahead window).
        window_start = max(0, lineno - 5)
        window = "\n".join(text.splitlines()[window_start:lineno + 5])
        for role in role_names:
            if role in window:
                findings.append(f"{path}:{lineno}: writes codified role '{role}'\n    {line.strip()[:120]}")

    return findings


def _walk(root: Path, role_names: set[str]) -> list[str]:
    """Walk `root`, scan eligible files, return findings."""
    findings: list[str] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        if path.name in ALLOWED_WRITERS:
            continue
        # Skip files inside the codified IAM dir itself (apply.sh, JSON
        # docs, this script). Those are sanctioned writers/readers.
        if SCRIPT_DIR in path.parents:
            continue

        findings.extend(_scan_file(path, role_names))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="Repository root to scan (can be passed multiple times). "
             "Default: this repo + every sibling that exists.",
    )
    args = parser.parse_args()

    if args.repo:
        roots = [Path(p).expanduser().resolve() for p in args.repo]
    else:
        # Default: this repo + sibling alpha-engine-* repos that exist.
        this_repo = SCRIPT_DIR.parent.parent
        siblings = [
            this_repo.parent / name
            for name in (
                "alpha-engine",
                "alpha-engine-data",
                "alpha-engine-research",
                "alpha-engine-predictor",
                "alpha-engine-backtester",
                "alpha-engine-dashboard",
                "alpha-engine-lib",
                "alpha-engine-config",
            )
        ]
        roots = [p for p in siblings if p.exists()]

    role_names = _codified_roles()
    if not role_names:
        print("No codified roles found — nothing to check.")
        return 0

    print(f"Scanning for foreign writers of: {sorted(role_names)}")
    print(f"Repos: {[str(r) for r in roots]}")

    all_findings: list[str] = []
    for root in roots:
        if not root.is_dir():
            print(f"  WARNING: {root} is not a directory — skipping")
            continue
        findings = _walk(root, role_names)
        all_findings.extend(findings)

    if all_findings:
        print(f"\nForeign IAM writers detected ({len(all_findings)} finding(s)):")
        for f in all_findings:
            print(f"  - {f}")
        print()
        print("Codified roles must have exactly one writer (apply.sh in their")
        print("home repo). Remove the inline write from the offending file or")
        print("decodify the role if the inline write is intentional.")
        return 1

    print("OK: no foreign writers found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
