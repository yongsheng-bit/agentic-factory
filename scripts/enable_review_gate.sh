#!/usr/bin/env bash
# One-time setup for the human gate with teeth: branch protection on main.
#
# Run AFTER creating the second GitHub account (the reviewer), and only if
# the token in use has repo admin scope. Requires the gh CLI to be logged in
# as the repo owner.
#
# The contexts below must match the job ids in .github/workflows/quality.yml
# exactly, or the check will never go green.
set -euo pipefail
REPO="${1:-yongsheng-bit/agentic-factory}"

gh api -X PUT "repos/$REPO/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["secrets", "deps", "human-gate", "quality", "e2e"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1
  },
  "restrictions": null
}
JSON

echo "main is protected: 1 approving review + green CI, enforced for admins too."
echo "The agent's own account cannot approve its own PRs; the reviewer account can."
