#!/bin/bash
# Installs the git hooks: pre-commit refuses commits on main, and pre-push runs
# bin/test.sh before every push.
set -e

cd "$(dirname "$0")/.."

cat > .git/hooks/pre-commit << 'EOF2'
#!/bin/bash
if [ "$(git branch --show-current)" = main ]; then
  echo "❌ main takes changes only through a PR. Commit on a branch from origin/main." >&2
  exit 1
fi
EOF2

cat > .git/hooks/pre-push << 'EOF2'
#!/bin/bash
set -e
echo "🔒 Running pre-push checks (bin/test.sh)..."
"$(git rev-parse --show-toplevel)/bin/test.sh"
EOF2

chmod +x .git/hooks/pre-commit .git/hooks/pre-push
echo "✅ Hooks installed: .git/hooks/pre-commit, .git/hooks/pre-push"
