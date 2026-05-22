#!/bin/bash
# Local-dev helper for MayringCoder.
#
# The supported install path is the Claude Code marketplace:
#   /plugin marketplace add Nileneb/MayringCoder
#   /plugin install mayring-coder@MayringCoder
#
# After enabling the plugin, the SessionStart hook (claude-plugin/hooks/session_start.py)
# bootstraps the venv and the OAuth JWT on the next Claude session — no manual
# script needed.
#
# This script exists for the rare case where you want to set up the plugin
# manually against a local clone of the repo (e.g. while developing the plugin
# itself). It does the same work the SessionStart hook does, eagerly.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"

if [ ! -f "$REPO_ROOT/src/api/local_mcp.py" ]; then
    echo "Error: $REPO_ROOT does not look like a MayringCoder clone (no src/api/local_mcp.py)." >&2
    echo "Use the marketplace install instead: /plugin marketplace add Nileneb/MayringCoder" >&2
    exit 1
fi

VENV_DIR="$_SCRIPT_DIR/.venv"
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "Creating venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "Installing client dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$REPO_ROOT/requirements-client.txt"

JWT_FILE="$HOME/.config/mayring/hook.jwt"
if [ ! -s "$JWT_FILE" ]; then
    echo "Setting up Mayring hook JWT (OAuth PKCE)..."
    "$VENV_DIR/bin/python" "$REPO_ROOT/tools/oauth_install.py" --jwt-file "$JWT_FILE"
else
    echo "JWT already present: $JWT_FILE"
fi

echo "Done. Restart Claude Code to load the plugin."
