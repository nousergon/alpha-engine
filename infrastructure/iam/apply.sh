#!/usr/bin/env bash
#
# apply.sh — Apply all IAM inline policies in this directory tree to their
# matching roles.
#
# Layout (directory-per-role to support multiple inline policies per role):
#
#   infrastructure/iam/<role-name>/<policy-name>.json
#
# Each JSON file is a policy document. The directory name is the IAM role
# name. The filename (minus .json) is the inline policy name. This keeps the
# 1:1 file→inline-policy mapping that alpha-engine-data established, while
# accommodating roles that already have multiple inline policies in prod
# (the executor role has 9 as of 2026-04-27).
#
# This is intentionally low-ceremony — no CloudFormation, no Terraform.
# Trust policies + role creation are NOT managed here (out of scope for a
# flat-file approach); the script only updates inline policies on roles
# that already exist.
#
# Usage:
#   ./infrastructure/iam/apply.sh                                # apply every policy
#   ./infrastructure/iam/apply.sh --role alpha-engine-executor-role
#                                                                # one role, all policies
#   ./infrastructure/iam/apply.sh --role <role> --policy <name>  # one specific policy
#   ./infrastructure/iam/apply.sh --dry-run                      # print planned commands
#
# Prerequisites:
#   - AWS CLI configured with iam:PutRolePolicy on the target roles
#   - The target IAM roles already exist in AWS

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"

DRY_RUN=0
TARGET_ROLE=""
TARGET_POLICY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --role)
      TARGET_ROLE="$2"
      shift 2
      ;;
    --policy)
      TARGET_POLICY="$2"
      shift 2
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

apply_one() {
  local file="$1"
  local role
  role="$(basename "$(dirname "$file")")"
  local policy_name
  policy_name="$(basename "$file" .json)"

  if ! python3 -c "import json; json.load(open('$file'))" 2>/dev/null; then
    echo "ERROR: $file is not valid JSON — skipping" >&2
    return 1
  fi

  echo "Applying $file -> role=$role policy=$policy_name"
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] aws iam put-role-policy --role-name $role --policy-name $policy_name --policy-document file://$file --region $REGION"
    return 0
  fi

  aws iam put-role-policy \
    --role-name "$role" \
    --policy-name "$policy_name" \
    --policy-document "file://$file" \
    --region "$REGION"
  echo "  OK"
}

cd "$SCRIPT_DIR"

shopt -s nullglob

if [[ -n "$TARGET_ROLE" && -n "$TARGET_POLICY" ]]; then
  file="${TARGET_ROLE}/${TARGET_POLICY}.json"
  if [[ ! -f "$file" ]]; then
    echo "ERROR: $file not found" >&2
    exit 1
  fi
  apply_one "$file"
elif [[ -n "$TARGET_ROLE" ]]; then
  if [[ ! -d "$TARGET_ROLE" ]]; then
    echo "ERROR: role directory $TARGET_ROLE not found" >&2
    exit 1
  fi
  files=( "$TARGET_ROLE"/*.json )
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No .json policy files found in $TARGET_ROLE/"
    exit 0
  fi
  for file in "${files[@]}"; do
    apply_one "$file"
  done
else
  files=( */*.json )
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No .json policy files found under $SCRIPT_DIR"
    exit 0
  fi
  for file in "${files[@]}"; do
    apply_one "$file"
  done
fi
