#!/usr/bin/env bash
# Remove only skill links owned by this checkout. Keep configuration and journals.
set -euo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [ "$#" -ne 0 ]; then
    echo "Usage: ./uninstall.sh (keeps .venv, .env, keys and operation journals)" >&2
    exit 1
fi
for skills_dir in "$HOME/.claude/skills" "$HOME/.agents/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
    dest="$skills_dir/reya-trading-agent-kit"
    if [ -L "$dest" ] && [ "$(cd "$dest" 2>/dev/null && pwd -P || true)" = "$KIT_DIR" ]; then
        rm "$dest"
        echo "Unlinked: $dest"
    fi
done
echo "Skill links removed. Checkout, .venv, configuration and operation journals were preserved."
echo "Do not delete journals while any operation is unresolved."
