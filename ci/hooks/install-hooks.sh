#!/usr/bin/env bash
#
# Install the Data Quality git hooks by pointing git at the tracked ci/hooks
# directory. This keeps the hooks under version control (unlike .git/hooks,
# which git never commits) so every clone gets the same shift-left gate.
#
# Usage:  bash ci/hooks/install-hooks.sh
# Undo:   git config --unset core.hooksPath
#
set -eu

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

git config core.hooksPath ci/hooks
chmod +x ci/hooks/pre-commit 2>/dev/null || true

echo "[dq] core.hooksPath set to ci/hooks"
echo "[dq] Pre-commit DDL validation is active (soft mode)."
echo "[dq]   Backend URL:  \${DQ_URL:-http://localhost:8000}"
echo "[dq]   Enforce mode: set DQ_BLOCK=1 to abort commits on blocking findings"
echo "[dq]   Bypass once:  git commit --no-verify"
