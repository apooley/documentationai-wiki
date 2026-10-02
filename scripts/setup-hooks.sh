#!/bin/sh
#
# Enable the repository's git hooks for this clone.
#
# Run once after cloning:
#     scripts/setup-hooks.sh
#
set -eu

root=$(git rev-parse --show-toplevel)
git -C "$root" config core.hooksPath .githooks
chmod +x "$root/.githooks/pre-commit" 2>/dev/null || true

echo "git hooks enabled (core.hooksPath=.githooks)"
echo "The pre-commit hook validates YAML frontmatter on staged .md/.mdx files."