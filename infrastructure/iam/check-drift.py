#!/usr/bin/env python3
"""check-drift.py — Diff codified IAM inline policies against live AWS state.

Walks `infrastructure/iam/<role>/<policy>.json` and compares against
`aws iam list-role-policies` + `aws iam get-role-policy` for each role.

Drift cases (all exit non-zero):
  * Source has a policy file that AWS doesn't have    (missing-in-aws)
  * AWS has an inline policy that source doesn't       (extra-in-aws)
  * Source policy document differs from AWS document   (content-drift)

JSON is compared after normalization (sorted keys, no trailing whitespace),
so cosmetic-only differences in indentation or key order don't trip the check.

Usage:
  ./infrastructure/iam/check-drift.py             # check every codified role
  ./infrastructure/iam/check-drift.py --role X    # check one role

Requires AWS creds with iam:ListRolePolicies + iam:GetRolePolicy on the
target roles. Locally: any admin profile. In CI: an OIDC role scoped to
those two read-only actions.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()


def _aws_iam(*args: str) -> dict | list | str:
    """Call aws iam ... and return the parsed JSON output."""
    result = subprocess.run(
        ["aws", "iam", *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"AWS CLI failed: aws iam {' '.join(args)}\n"
            f"stderr: {result.stderr}\n"
        )
        sys.exit(2)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _canonical_json(doc: dict) -> str:
    """Canonical JSON for byte-stable comparison: sorted keys, no extra ws."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _check_role(role_dir: Path) -> list[str]:
    """Return list of drift findings for a single role. Empty list means clean."""
    role_name = role_dir.name
    findings: list[str] = []

    # ── Set diff ────────────────────────────────────────────────────────────
    source_policies = {p.stem for p in role_dir.glob("*.json")}
    if not source_policies:
        return [f"{role_name}: no .json files in {role_dir} — empty role dir"]

    aws_resp = _aws_iam("list-role-policies", "--role-name", role_name)
    aws_policies = set(aws_resp.get("PolicyNames", []))

    extra_in_aws = aws_policies - source_policies
    missing_in_aws = source_policies - aws_policies

    for p in sorted(missing_in_aws):
        findings.append(
            f"{role_name}/{p}: codified in source but not on AWS role "
            f"(run apply.sh to push)"
        )
    for p in sorted(extra_in_aws):
        findings.append(
            f"{role_name}/{p}: present on AWS role but not codified "
            f"(add JSON file or delete from AWS)"
        )

    # ── Content diff for the policies present on both sides ────────────────
    for policy_name in sorted(source_policies & aws_policies):
        source_path = role_dir / f"{policy_name}.json"
        try:
            source_doc = json.loads(source_path.read_text())
        except json.JSONDecodeError as exc:
            findings.append(
                f"{role_name}/{policy_name}: source JSON invalid ({exc})"
            )
            continue

        aws_resp = _aws_iam(
            "get-role-policy",
            "--role-name", role_name,
            "--policy-name", policy_name,
        )
        aws_doc = aws_resp.get("PolicyDocument", {})

        if _canonical_json(source_doc) != _canonical_json(aws_doc):
            findings.append(
                f"{role_name}/{policy_name}: source document differs from "
                f"AWS document (content drift)"
            )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", help="Check one role (default: every codified role)"
    )
    args = parser.parse_args()

    if args.role:
        role_dirs = [SCRIPT_DIR / args.role]
        if not role_dirs[0].is_dir():
            sys.stderr.write(f"ERROR: {role_dirs[0]} is not a directory\n")
            return 2
    else:
        role_dirs = sorted(p for p in SCRIPT_DIR.iterdir() if p.is_dir())

    if not role_dirs:
        print("No codified role directories found under "
              f"{SCRIPT_DIR} — nothing to check.")
        return 0

    total_findings: list[str] = []
    for role_dir in role_dirs:
        findings = _check_role(role_dir)
        total_findings.extend(findings)

    if total_findings:
        print(f"IAM drift detected ({len(total_findings)} finding(s)):")
        for f in total_findings:
            print(f"  - {f}")
        return 1

    role_names = ", ".join(d.name for d in role_dirs)
    print(f"OK: no IAM drift for {role_names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
