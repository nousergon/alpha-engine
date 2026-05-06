# IAM policies (alpha-engine — module-specific roles)

Source-of-truth for the inline IAM policies on this repo's IAM roles.
Module-specific roles only — cross-cutting orchestration roles
(SF execution role, EventBridge cron role, GitHub Actions Lambda
deploy role) live in `alpha-engine-data/infrastructure/iam/` because
their grants are derived from code that lives there.

## Layout

```
infrastructure/iam/
├── apply.sh
├── README.md
└── <role-name>/
    ├── <policy-1>.json
    ├── <policy-2>.json
    └── ...
```

The directory name is the IAM role name; each JSON filename (minus `.json`)
is the inline policy name on that role.

## Roles managed here

- **`alpha-engine-executor-role`** — assumed by the trading EC2 instance
  (`ae-trading`) and any executor processes assuming it. 8 inline policies
  as of 2026-04-27 (was 9 — `alpha-engine-ssm-access` consolidated into
  `alpha-engine-ssm-read`, which already had the superset of actions).
  Trust policy + role creation are NOT managed here (out of scope for the
  flat-file approach).
- **`github-actions-iam-drift-check`** — assumed by GitHub Actions via
  OIDC for the daily IAM-drift-check workflow. Single inline policy
  granting `iam:ListRolePolicies` + `iam:GetRolePolicy` scoped to every
  codified role across alpha-engine + alpha-engine-data + alpha-engine-predictor.
  Trust policy: `repo:cipher813/alpha-engine` + `repo:cipher813/alpha-engine-data`
  (main + pull_request); widened 2026-05-06 to support alpha-engine-data's
  drift-check workflow when the cross-cutting orchestration roles moved
  to that repo.

## Roles owned elsewhere

| Role | Home repo | Why there |
|---|---|---|
| `alpha-engine-step-functions-role` | `alpha-engine-data` | Grants reflect the Lambdas the SF JSON invokes + EC2 instances it SSMs + the trading instance it starts/stops — all defined in `alpha-engine-data/infrastructure/`. |
| `alpha-engine-eventbridge-sfn-role` | `alpha-engine-data` | Grants reflect which SFs the EventBridge cron rules target — same source repo. |
| `github-actions-lambda-deploy` | `alpha-engine-data` | Cross-cutting; assumed by Lambda deploy workflows in multiple repos. |
| `alpha-engine-predictor-role` | `alpha-engine-predictor` | Predictor Lambda's execution role. |

Each repo has its own `apply.sh` + `check-drift.py` scoped to its own
codified roles. The foreign-writer guard (`check-no-foreign-writers.py`)
in this directory scans every sibling repo for codified-role writes
that bypass the home repo's `apply.sh`, regardless of where the role
is codified.

## Out of scope (not codified here)

- Trust policies (`AssumeRolePolicyDocument`) — those are role creation,
  managed manually
- Managed policies (e.g. `AmazonSSMManagedInstanceCore` is attached to
  `alpha-engine-executor-role`) — managed manually via attach commands
- Role creation itself — pre-existing, managed manually

## Usage

```bash
# Apply every policy in this directory tree
./infrastructure/iam/apply.sh

# Apply every policy on one role
./infrastructure/iam/apply.sh --role alpha-engine-executor-role

# Apply one specific policy
./infrastructure/iam/apply.sh --role alpha-engine-executor-role --policy alpha-engine-cloudwatch-metrics

# Print planned commands without executing
./infrastructure/iam/apply.sh --dry-run
```

`apply.sh` calls `aws iam put-role-policy`, which is idempotent — re-running
overwrites the existing inline policy on the role. To remove a policy you
codified here, delete the file AND run `aws iam delete-role-policy` manually
(removal is not yet automated to avoid an `apply.sh` invocation accidentally
wiping policies whose JSON file was deleted in a stale checkout).

## Drift detection (codified vs live AWS)

`check-drift.py` diffs the codified state against AWS for every role
directory under `infrastructure/iam/`. It checks both:

- **Set drift**: every `.json` file matches an inline policy on the role,
  and vice versa.
- **Content drift**: per-policy document equality after JSON normalization.

```bash
# Local
./infrastructure/iam/check-drift.py
./infrastructure/iam/check-drift.py --role alpha-engine-executor-role
```

Exit code 0 = clean, 1 = drift detected, 2 = AWS CLI error or invalid
source JSON.

## Foreign-writer detection (multi-writer regressions)

`check-no-foreign-writers.py` enforces the **single-writer rule**: each
codified role must have exactly one writer (`apply.sh` in this repo).
Any deploy script in any sibling repo that calls `aws iam put-role-policy`
against a codified role name is a regression risk and fails the check.

This catches the regression class behind 4 IAM-clobber incidents in two
months (EB-SFN role 2026-04-21 + 2026-05-04 + 2026-05-06; SF role
2026-05-04 EOD + 2026-05-06 morning). All four had the same shape: a
codified policy with `apply.sh` as the sanctioned writer + a stale
inline `put-role-policy` block in `alpha-engine-data` deploy scripts.
Whichever ran last won.

```bash
# Local — scans this repo + all sibling alpha-engine-* repos that exist
./infrastructure/iam/check-no-foreign-writers.py

# Scope to a single repo
./infrastructure/iam/check-no-foreign-writers.py --repo ~/Development/alpha-engine-data
```

Exit code 0 = clean, 1 = foreign writer detected.

## CI integration

`.github/workflows/iam-drift-check.yml` runs both checks:

- **Drift check** — needs OIDC AWS read access. Compares codified to live.
- **Foreign-writers check** — pure source scan, clones every sibling
  alpha-engine-* repo and greps for `put-role-policy` against codified
  role names. No AWS auth needed.

Triggers: every PR touching `infrastructure/iam/**`, daily at 09:30 UTC,
manual `workflow_dispatch`.

Auth (drift-check only): OIDC via the `github-actions-iam-drift-check`
role (read-only: `iam:ListRolePolicies` + `iam:GetRolePolicy` on the
codified roles).

## When you add a new inline policy

1. Apply it to AWS first (e.g. via `aws iam put-role-policy ...`)
2. Save the JSON document to the matching directory
3. Commit the file with a description of why the grant was needed

## When you remove an inline policy

1. Delete the file from this directory
2. Run `aws iam delete-role-policy --role-name <role> --policy-name <policy>`
3. Commit the deletion

The flat-file approach is intentionally low-ceremony — if the blast radius
grows (cross-account, multiple roles per service, complex trust-policy
state), migrate to CloudFormation/Terraform.
