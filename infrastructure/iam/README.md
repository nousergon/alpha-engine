# IAM policies (alpha-engine executor)

Source-of-truth for the inline IAM policies on the executor's IAM roles.
Mirrors the `alpha-engine-data` codification pattern (one JSON file per
inline policy + an `apply.sh` runner) with a directory-per-role layout
since the executor role has multiple inline policies.

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
  (`ae-trading`) and any executor processes assuming it. 9 inline policies
  as of 2026-04-27. Trust policy + role creation are NOT managed here
  (out of scope for the flat-file approach).

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

## Drift detection

There is no automated drift detector in this repo today. To check whether
any role's inline policies have drifted from this directory:

```bash
aws iam list-role-policies --role-name alpha-engine-executor-role
```

The set of returned policy names should match the set of `.json` files in
`alpha-engine-executor-role/`.

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
