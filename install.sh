#!/usr/bin/env bash
# Installer for Sticky — https://github.com/adrianleb/sticky
# Usage: curl -fsSL https://raw.githubusercontent.com/adrianleb/sticky/main/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/adrianleb/sticky"
PKG_SPEC="git+${REPO_URL}#subdirectory=apps/sticky"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "sticky requires macOS (the Postbox format is macOS-specific)." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing sticky from ${REPO_URL}…"
uv tool install --force --from "${PKG_SPEC}" sticky

cat <<'EOF'

✓ Sticky installed.

Next:
  sticky init

If `sticky` isn't on your PATH yet, run `uv tool update-shell` and open a new terminal.
EOF
