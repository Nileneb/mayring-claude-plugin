import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _session_ctx as sc

# derive_task: regex-first, imperative + tail
t = sc.derive_task("implementiere die codebook API endpoints", "MayringCoder", "")
assert t and "codebook" in t.lower(), f"derive_task regex: {t!r}"
assert sc.derive_task("hi", "", "") == "", "too-short prompt → empty"

# _git_remote returns str|None, never raises
r = sc._git_remote(cwd="/nonexistent-xyz")
assert r is None, f"_git_remote bad cwd → None, got {r!r}"

# route_project is fail-soft when the API is unreachable
out = sc.route_project("badtoken", None, "vague prompt")
assert isinstance(out, dict) and "project_id" in out, f"route_project shape: {out!r}"

print("PASS test_session_ctx_router")
