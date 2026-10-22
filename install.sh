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
#   ./install.sh --verbose               # print the full doctor and health JSON instead of a summary
#
# No Python 3.12+ that can make a venv (a stock Mac ships 3.9; Ubuntu ships the venv
# module separately)? The installer uses uv instead, with no admin rights; see below.
# --bootstrap-python is still accepted but no longer needed.
#
# The one-line setup an assistant runs for a user:
#   ./install.sh --release=latest --owner=0x<wallet>
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="reya-trading-agent-kit"
MIN_MINOR=12 # Python 3.12+
USAGE="supported: --release=latest|vX.Y.Z, --owner=0x<wallet>, --no-link, --skip-health, --verbose, --agent=claude-code|agentskills|codex|both|all"

LINK=1
HEALTH=1
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
        --bootstrap-python) PASS_ARGS+=("$arg") ;; # accepted for older instructions; uv is now used when needed
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
#
# Git over the network never waits on a prompt: an assistant's shell has no visible
# terminal, so a username or SSH passphrase prompt would hang the install silently.
# Terminal prompts are off, and SSH runs in batch mode unless the user configured
# their own SSH command.
git_net() {
    if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "$(git -C "$KIT_DIR" config --get core.sshCommand 2>/dev/null || true)" ]; then
        GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git -C "$KIT_DIR" "$@"
    else
        GIT_TERMINAL_PROMPT=0 git -C "$KIT_DIR" "$@"
    fi
}
# While the repository is private, git needs GitHub credentials. A signed-in GitHub CLI
# (gh auth login) supplies them for this one command, the way gh itself does; the
# user's git configuration is not changed. Otherwise git uses its own credentials.
fetch_releases() {
    if command -v gh >/dev/null 2>&1 && gh auth token --hostname github.com >/dev/null 2>&1 &&
        git_net -c credential.https://github.com.helper= \
            -c 'credential.https://github.com.helper=!gh auth git-credential' fetch --quiet --tags origin 2>/dev/null; then
        return 0
    fi
    git_net fetch --quiet --tags origin
}
switch_to_release() {
    local want="$1" tag="" current="" fetched=1
    if ! git -C "$KIT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        echo "error: --release needs a git checkout of the kit (this directory is not one)" >&2
        exit 1
    fi
    if [ -n "$(git -C "$KIT_DIR" status --porcelain --untracked-files=no)" ]; then
        echo "error: this checkout has local changes to tracked files; commit or discard them before switching releases" >&2
        exit 1
    fi
    if ! fetch_releases; then
        fetched=0
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
    fi
    # "latest" can name a release (from gh) that a failed fetch did not bring in.
    if ! git -C "$KIT_DIR" rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
        echo "error: release $tag not found in this checkout" >&2
        if [ "$fetched" -eq 0 ]; then
            echo "git could not reach GitHub. While the repository is private, git needs your GitHub sign-in:" >&2
            echo "run 'gh auth login' in a terminal (or 'gh auth setup-git' if you are already signed in), then run this again." >&2
        fi
        exit 1
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

# --- Create the venv ----------------------------------------------------------------
# With the first system Python 3.12+ that can make one. Ubuntu and Debian ship the venv
# module separately (python3.X-venv), so that can fail; the installer then uses uv, which
# builds the venv without it and supplies a private Python 3.12 when the system has none
# (a stock Mac ships 3.9). A uv already on this machine is reused. Otherwise a pinned uv
# release, checked against the SHA-256 below before it runs, goes inside the kit folder
# (.uv/) together with its Python and cache: no admin rights, no PATH or shell-profile
# changes, and deleting the kit folder removes it.
VENV="$KIT_DIR/.venv"
KIT_UV_DIR="$KIT_DIR/.uv"
UV_VERSION="0.12.19"
if [ -L "$VENV" ]; then
    echo "error: .venv must not be a symlink" >&2
    exit 1
fi

venv_works() {
    "$VENV/bin/python" -m pip --version >/dev/null 2>&1
}

# "<release archive> <sha256>" of the pinned uv for this platform (the static musl
# builds run on any Linux). The values are uv's published checksums for $UV_VERSION.
uv_archive() {
    case "$(uname -s)/$(uname -m)" in
        Darwin/arm64) echo "uv-aarch64-apple-darwin a9a8df1eedeb192f2e47e40e2faabfb387db4b850209118786d42f89dde3e0ba" ;;
        Darwin/x86_64) echo "uv-x86_64-apple-darwin cb5fa57bafe68fc0fb94b17f06bee0b0b9a7feb94ccbd110445afa0696e39273" ;;
        Linux/x86_64) echo "uv-x86_64-unknown-linux-musl db7278c9f57981338fddff1fb250e11964bc0a4fafcb9eed8303fdb117dc067b" ;;
        Linux/aarch64 | Linux/arm64) echo "uv-aarch64-unknown-linux-musl ad8d8448a2ff642ba62c2f684d7dd22a03f8eb3fc9918c2c3e8ec975f4ed6710" ;;
        *) return 1 ;;
    esac
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        return 1
    fi
}

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        return 1
    fi
}

install_kit_uv() {
    local spec name want got tmp
    if ! spec="$(uv_archive)"; then
        echo "error: no pinned uv build for $(uname -s) $(uname -m); install Python 3.12+ with its venv module and run this again" >&2
        return 1
    fi
    name="${spec% *}"
    want="${spec#* }"
    tmp="$(mktemp -d)"
    echo "Downloading uv $UV_VERSION into $KIT_UV_DIR (checked against its pinned SHA-256; nothing outside the kit folder changes)..."
    # Download fully and check it first: a partial or altered file is never run.
    if ! download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$name.tar.gz" "$tmp/uv.tar.gz"; then
        rm -rf "$tmp"
        echo "error: could not download uv (needs curl or wget and access to github.com)" >&2
        return 1
    fi
    if ! got="$(sha256_of "$tmp/uv.tar.gz")"; then
        rm -rf "$tmp"
        echo "error: cannot check the uv download: no sha256sum, shasum or openssl found" >&2
        return 1
    fi
    if [ "$got" != "$want" ]; then
        rm -rf "$tmp"
        echo "error: the uv download does not match its pinned SHA-256; it was not used" >&2
        return 1
    fi
    if ! tar -xzf "$tmp/uv.tar.gz" -C "$tmp" "$name/uv"; then
        rm -rf "$tmp"
        echo "error: could not unpack the uv download" >&2
        return 1
    fi
    mkdir -p "$KIT_UV_DIR/bin"
    mv "$tmp/$name/uv" "$KIT_UV_DIR/bin/uv"
    rm -rf "$tmp"
}

if [ -e "$VENV" ] && ! venv_works; then
    echo "Rebuilding .venv: the one here is incomplete (an earlier install stopped part-way) or its Python is gone"
    rm -rf "$VENV"
fi

SYSTEM_PY="" # a system Python 3.12+ that could not make a venv by itself; uv can still use it
if [ ! -e "$VENV" ]; then
    seen=" "
    for candidate in python3.13 python3.12 python3.14 python3; do
        py="$(command -v "$candidate" 2>/dev/null)" || continue
        "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null || continue
        real="$("$py" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null)" || continue
        case "$seen" in *" $real "*) continue ;; esac
        seen="$seen$real "
        echo "Creating venv at $VENV with $("$py" --version 2>&1) ($py)"
        if output="$("$py" -m venv "$VENV" 2>&1)" && venv_works; then
            break
        fi
        rm -rf "$VENV"
        SYSTEM_PY="${SYSTEM_PY:-$py}"
        case "$output" in
            *ensurepip*) echo "  $py has no working venv module (Ubuntu and Debian ship it as python3.X-venv)" ;;
            *) echo "  $py could not create a venv" ;;
        esac
        if [ "$VERBOSE" -eq 1 ]; then printf '%s\n' "$output" | sed 's/^/    /'; fi
    done
fi

if [ ! -e "$VENV" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
    elif [ -x "$HOME/.local/bin/uv" ]; then # from an earlier --bootstrap-python
        UV="$HOME/.local/bin/uv"
    else
        if [ ! -x "$KIT_UV_DIR/bin/uv" ]; then
            install_kit_uv || exit 1
        fi
        UV="$KIT_UV_DIR/bin/uv"
        # Keep uv's Python, executables and cache inside the kit folder too.
        export UV_PYTHON_INSTALL_DIR="$KIT_UV_DIR/python" UV_PYTHON_BIN_DIR="$KIT_UV_DIR/bin" UV_CACHE_DIR="$KIT_UV_DIR/cache"
    fi
    created=0
    if [ -n "$SYSTEM_PY" ]; then
        echo "Creating venv at $VENV with uv and $("$SYSTEM_PY" --version 2>&1)"
        if "$UV" venv --quiet --seed --python "$SYSTEM_PY" "$VENV" && venv_works; then
            created=1
        else
            rm -rf "$VENV"
        fi
    fi
    if [ "$created" -eq 0 ]; then
        echo "Creating venv at $VENV with uv's Python 3.$MIN_MINOR (downloaded once if needed; no admin rights)..."
        if ! "$UV" venv --quiet --seed --python "3.$MIN_MINOR" "$VENV" || ! venv_works; then
            rm -rf "$VENV"
            echo "error: could not create a Python 3.$MIN_MINOR+ environment, with the system Python or with uv ($UV)." >&2
            echo "Install Python 3.$MIN_MINOR+ with its venv module (e.g. 'brew install python@3.12' or 'sudo apt install python3-venv'), then run this again." >&2
            exit 1
        fi
    fi
fi

echo "Installing into $VENV with $("$VENV/bin/python" --version)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "Installing pinned dependencies (pulls reya-python-sdk from GitHub)..."
"$VENV/bin/python" -m pip install --quiet -r "$KIT_DIR/requirements.lock"
"$VENV/bin/python" -m pip install --quiet --no-deps -e "$KIT_DIR"

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
