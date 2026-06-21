#!/usr/bin/env bash
# Run a plugin hook through the plugin's ISOLATED venv interpreter — never the
# ambient (conda base / system) python.
#
# WHY: hooks.json previously invoked `python3 <hook>`, which resolves to
# whatever python is first on PATH (typically an activated conda base). That
# forced every hook dependency (chromadb, mcp, httpx, …) to be pip-installed
# into the user's base environment, polluting it. Routing through the venv
# keeps all hook deps isolated in ${CLAUDE_PLUGIN_ROOT}/.venv.
#
# Fallback to python3 only until SessionStart has bootstrapped the venv on a
# fresh install; session_start.py itself imports stdlib only, so it runs fine
# under the fallback and builds the venv the rest of the hooks then use.
set -euo pipefail
VENV_PY="${CLAUDE_PLUGIN_ROOT}/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  exec "$VENV_PY" "$@"
fi
exec python3 "$@"
