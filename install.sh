#!/usr/bin/env bash
# Reya Agent Kit installer — no Poetry required.
#
# Creates .venv inside the kit directory, installs the pinned dependencies
# from requirements.lock, links the kit into agent skills directories, and
# verifies connectivity to the Reya API.
#
# Usage:
#   ./install.sh                         # venv + deps + skill links + health check
#   ./install.sh --release=latest        # first switch this checkout to the latest release (also: --release=v0.3.0)
#   ./install.sh --owner=0x<wallet>      # after installing, start the guided setup (agent init) for that wallet
#   ./install.sh --agent=codex           # link only for Codex (${CODEX_HOME:-~/.codex}/skills)
#   ./install.sh --no-link               # skip the skills-directory symlinks
#   ./install.sh --skip-health           # skip the connectivity check (offline / CI)
#   ./install.sh --bootstrap-python      # no Python 3.12+? install a private copy with uv (no admin rights)
#   ./install.sh --verbose               # print the full doctor and health JSON instead of a summary
#
# The one-line setup an assistant runs for a user:
#   ./install.sh --release=latest --owner=0x<wallet>
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="reya-trading-agent-kit"
MIN_MINOR=12 # Python 3.12+
USAGE="supported: --release=latest|vX.Y.Z, --owner=0x<wallet>, --no-link, --skip-health, --bootstrap-python, --verbose, --agent=claude-code|agentskills|codex|both|all"
UV_VERSION="0.12.19" # pinned uv installer used only with --bootstrap-python

LINK=1
HEALTH=1
BOOTSTRAP_PYTHON=0
VERBOSE=0
AGENT="default"
RELEASE=""
OWNER=""
PASS_ARGS=() # everything except --release, handed to the release's own installer
while [ "$#" -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
        --release=*) RELEASE="${arg#--release=}" ;;
        --release)
            [ "$#" -gt 0 ] || { echo "error: --release needs a value (latest or vX.Y.Z)" >&2; exit 1; }
            RELEASE="$1"
            shift
            ;;
        --owner=*) OWNER="${arg#--owner=}"; PASS_ARGS+=("--owner=$OWNER") ;;
        --owner)
            [ "$#" -gt 0 ] || { echo "error: --owner needs a wallet address" >&2; exit 1; }
            OWNER="$1"
            PASS_ARGS+=("--owner=$OWNER")
            shift
            ;;
        --no-link) LINK=0; PASS_ARGS+=("$arg") ;;
        --agent=claude-code | --agent=agentskills | --agent=codex | --agent=both | --agent=all)
            AGENT="${arg#--agent=}"
            PASS_ARGS+=("$arg")
            ;;
        --skip-health) HEALTH=0; PASS_ARGS+=("$arg") ;;
        --bootstrap-python) BOOTSTRAP_PYTHON=1; PASS_ARGS+=("$arg") ;;
        --verbose) VERBOSE=1; PASS_ARGS+=("$arg") ;;
        *)
            echo "Unknown option: $arg ($USAGE)" >&2
            exit 1
            ;;
    esac
done

# Validate before doing anything slow: a typo should not cost a full install.
if [ -n "$OWNER" ] && ! [[ "$OWNER" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    echo "error: --owner must be a wallet address (0x followed by 40 hex characters), got: $OWNER" >&2
    exit 1
fi

# --- Optionally switch to a release first --------------------------------------
# Resolves "latest" with the GitHub CLI (the release marked Latest) and falls back
# to the highest vX.Y.Z tag. Refuses to touch a checkout with local changes to
# tracked files; untracked files such as .env and .venv are never affected.
# The whole switch runs inside this function and ends in exec, so bash never
# reads further from this file after the checkout replaces it on disk.
switch_to_release() {
    local want="$1" tag="" current=""
    if ! git -C "$KIT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        echo "error: --release needs a git checkout of the kit (this directory is not one)" >&2
        exit 1
    fi
    if [ -n "$(git -C "$KIT_DIR" status --porcelain --untracked-files=no)" ]; then
        echo "error: this checkout has local changes to tracked files; commit or discard them before switching releases" >&2
        exit 1
    fi
    if ! git -C "$KIT_DIR" fetch --quiet --tags origin; then
        echo "warning: could not fetch releases from GitHub; using the tags already in this checkout" >&2
    fi
    if [ "$want" = "latest" ]; then
        if command -v gh >/dev/null 2>&1; then
            tag="$(cd "$KIT_DIR" && gh release view --json tagName --jq .tagName 2>/dev/null || true)"
        fi
        if [ -z "$tag" ]; then
            tag="$(git -C "$KIT_DIR" tag --list 'v[0-9]*' --sort=-v:refname | head -n 1)"
        fi
        if [ -z "$tag" ]; then
            echo "error: no release found; check GitHub access with 'gh auth status'" >&2
            exit 1
        fi
    else
        tag="$want"
        if ! git -C "$KIT_DIR" rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
            echo "error: release $tag not found in this checkout" >&2
            exit 1
        fi
    fi
    current="$(git -C "$KIT_DIR" rev-parse HEAD)"
    if [ "$current" = "$(git -C "$KIT_DIR" rev-parse "$tag^{commit}")" ]; then
        echo "Already on release $tag"
        return 0
    fi
    echo "Switching to release $tag (was $(git -C "$KIT_DIR" describe --tags --always "$current"))"
    git -C "$KIT_DIR" -c advice.detachedHead=false switch --quiet --detach "$tag"
    exec "$KIT_DIR/install.sh" "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
}
if [ -n "$RELEASE" ]; then
    switch_to_release "$RELEASE"
fi

# --- Find a suitable Python (3.12+) ----------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; then
        PY="$(command -v "$candidate")"
        break
    fi
done
# No suitable system Python (a stock Mac ships 3.9): use uv's managed Python if uv is
# available, installing uv itself only when the user opted in with --bootstrap-python.
# uv keeps its Python under the user's home directory; no admin rights are needed.
UV=""
if [ -z "$PY" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV="$HOME/.local/bin/uv"
    elif [ "$BOOTSTRAP_PYTHON" -eq 1 ]; then
        echo "Installing uv $UV_VERSION (to manage a private Python 3.$MIN_MINOR; no admin rights needed)..."
        # Download fully first, then run: a partial transfer must never be executed.
        UV_INSTALLER="$(mktemp)"
        if ! curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$UV_INSTALLER"; then
            rm -f "$UV_INSTALLER"
            echo "error: could not download the uv installer" >&2
            exit 1
        fi
        if ! env UV_NO_MODIFY_PATH=1 sh "$UV_INSTALLER" >/dev/null; then
            rm -f "$UV_INSTALLER"
            echo "error: uv install failed" >&2
            exit 1
        fi
        rm -f "$UV_INSTALLER"
        UV="$HOME/.local/bin/uv"
    fi
    if [ -n "$UV" ] && [ -x "$UV" ]; then
        PY="$("$UV" python find "3.$MIN_MINOR" 2>/dev/null || true)"
        if [ -z "$PY" ]; then
            echo "Installing Python 3.$MIN_MINOR with uv..."
            "$UV" python install "3.$MIN_MINOR" >/dev/null
            PY="$("$UV" python find "3.$MIN_MINOR")"
        fi
    fi
fi
if [ -z "$PY" ]; then
    echo "error: Python 3.$MIN_MINOR+ not found (macOS ships 3.9)." >&2
    echo "Re-run with --bootstrap-python to install a private copy with uv (no admin rights needed)," >&2
    echo "or install it yourself, e.g. 'brew install python@3.12', then re-run this script." >&2
    exit 1
fi
echo "Available interpreter: $("$PY" --version) at $PY"

# --- Create the venv and install pinned deps --------------------------------
VENV="$KIT_DIR/.venv"
if [ -L "$VENV" ]; then
    echo "error: .venv must not be a symlink" >&2
    exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating venv at $VENV"
    "$PY" -m venv "$VENV"
fi
echo "Installing into $VENV with $("$VENV/bin/python" --version)"
"$VENV/bin/pip" install --quiet --upgrade pip
echo "Installing pinned dependencies (pulls reya-python-sdk from GitHub)..."
"$VENV/bin/pip" install --quiet -r "$KIT_DIR/requirements.lock"
"$VENV/bin/pip" install --quiet --no-deps -e "$KIT_DIR"

# --- Link into agent skills directories --------------------------------------
# ~/.claude/skills for Claude Code, ~/.agents/skills for other agentskills.io
# runtimes, ${CODEX_HOME:-~/.codex}/skills for Codex. The default links Claude
# Code and agentskills, plus Codex when its home directory already exists;
# "both" keeps the original two. Idempotent: re-running refreshes the links,
# and a clone that already lives in a skills directory is left alone.
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
if [ "$LINK" -eq 1 ]; then
    kit_real="$(cd "$KIT_DIR" && pwd -P)"
    skills_dirs=()
    case "$AGENT" in default|both|all|claude-code) skills_dirs+=("$HOME/.claude/skills") ;; esac
    case "$AGENT" in default|both|all|agentskills) skills_dirs+=("$HOME/.agents/skills") ;; esac
    if [ "$AGENT" = "codex" ] || [ "$AGENT" = "all" ] || { [ "$AGENT" = "default" ] && [ -d "$CODEX_DIR" ]; }; then
        skills_dirs+=("$CODEX_DIR/skills")
    fi
    for skills_dir in "${skills_dirs[@]}"; do
        dest="$skills_dir/$SKILL_NAME"
        if [ "$(cd "$dest" 2>/dev/null && pwd -P || true)" = "$kit_real" ] && [ ! -L "$dest" ]; then
            echo "Already installed at $dest"
            continue
        fi
        if { [ -e "$dest" ] || [ -L "$dest" ]; } && [ "$(cd "$dest" 2>/dev/null && pwd -P || true)" != "$kit_real" ]; then
            echo "error: $dest belongs to another installation; unlink it explicitly before switching versions." >&2
            exit 1
        fi
        mkdir -p "$skills_dir"
        ln -snf "$KIT_DIR" "$dest"
        echo "Linked: $dest -> $KIT_DIR"
    done
fi

# --- Diagnose ---------------------------------------------------------------------
# A short human summary by default; the full JSON with --verbose.
if [ "$VERBOSE" -eq 1 ]; then
    "$VENV/bin/python" -m agent_kit.scripts.doctor
else
    "$VENV/bin/python" -m agent_kit.scripts.doctor | "$VENV/bin/python" -c '
import json, sys
d = json.load(sys.stdin)
env = "mainnet (real funds)" if d.get("real_funds") else (d.get("api_url") or "custom deployment")
status, version = d.get("status", "unknown"), d.get("kit_version", "?")
print(f"Kit check: {status}, kit {version}, {env}")
for item in d.get("errors") or []:
    print(f"  error: {item}")
for item in d.get("warnings") or []:
    print(f"  warning: {item}")
' || echo "warning: could not summarise doctor output; run with --verbose" >&2
fi

# --- Credentials reminder -----------------------------------------------------
if [ ! -f "$KIT_DIR/.env" ] && [ -z "$OWNER" ]; then
    echo
    echo "No .env yet; none is needed on mainnet (the default). Set up trading with the guided flow:"
    echo "  $VENV/bin/python -m agent_kit.scripts.agent init --owner 0x<your wallet>   # approve an agent key in the app; never paste a private key"
    echo "  For devnet1 or another deployment, copy $KIT_DIR/.env.example to .env and switch the commented block first."
fi

# --- Health check -------------------------------------------------------------
if [ "$HEALTH" -eq 1 ]; then
    echo
    if [ "$VERBOSE" -eq 1 ]; then
        echo "Health check:"
        "$VENV/bin/python" -m agent_kit.scripts.health || HEALTH_FAILED=1
    else
        "$VENV/bin/python" -m agent_kit.scripts.health 2>/dev/null | "$VENV/bin/python" -c '
import json, sys
d = json.load(sys.stdin)
if d.get("status") != "ok":
    sys.exit(1)
api, markets, latency = d.get("api_url"), d.get("markets_available"), d.get("latency_ms")
print(f"Reya API reachable: {api}, {markets} markets, {latency} ms")
' || HEALTH_FAILED=1
    fi
    if [ "${HEALTH_FAILED:-0}" -eq 1 ]; then
        echo "warning: health check failed — the install itself succeeded; check network egress to api.reya.xyz." >&2
    fi
fi

echo
echo "Installed $(cd "$KIT_DIR" && git describe --tags --always 2>/dev/null || echo "from $KIT_DIR"). Invoke the kit from any directory as:"
echo "  $VENV/bin/python -m agent_kit.scripts.query prices"

# --- Guided setup ---------------------------------------------------------------
# With --owner the install continues straight into agent setup, so one command
# takes a new user from nothing to the first step in the app. agent init prints
# JSON whose "next" says what the user does now. It opens nothing yet: the
# assistant asks the practice question first, and the next init run opens the
# setup page. Nothing is signed here: approvals happen in the user's wallet.
if [ -n "$OWNER" ]; then
    echo
    echo "Setup for $OWNER (ask the practice question in \"practice_first\"; for real trading run: $VENV/bin/python -m agent_kit.scripts.agent init --until-ready --timeout 100):"
    "$VENV/bin/python" -m agent_kit.scripts.agent init --owner "$OWNER"
fi
