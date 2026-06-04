"""C3: the Stop hook stamps the session's project onto the conversation
micro-batch (X-Project-Id header + origin_ref body) so the server links the
chunks (producer B). Without a resolved project, neither is sent → chunks stay
global, exactly the pre-C3 behaviour. Fail-soft: a missing session_ctx never
breaks the hook."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_mod_path = Path(__file__).parent / "stop_hook.py"
sys.path.insert(0, str(_mod_path.parent))
spec = importlib.util.spec_from_file_location("stop_hook", _mod_path)
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)


def _capture_request(project_id, origin_ref):
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock()
        return cm

    with patch.object(sh.urllib.request, "urlopen", side_effect=_fake_urlopen), \
         patch.object(sh, "device_headers", return_value={}):
        sh._post_micro_batch(
            [{"role": "user", "content": "hi"}], "sess123", "ws", "tok",
            project_id=project_id, origin_ref=origin_ref,
        )
    return captured


def test_micro_batch_stamps_project_when_resolved():
    cap = _capture_request("proj-abc", "https://github.com/o/r")
    # urllib title-cases header keys
    assert cap["headers"].get("X-project-id") == "proj-abc"
    assert cap["body"]["origin_ref"] == "https://github.com/o/r"


def test_micro_batch_no_stamp_when_unresolved():
    cap = _capture_request(None, "")
    assert "X-project-id" not in cap["headers"]
    assert "origin_ref" not in cap["body"]


def test_project_stamp_reads_active_project():
    with patch.object(sh, "_read_session_ctx",
                      return_value={"active_project": {"project_id": "p9"}}), \
         patch.object(sh, "_git_remote", return_value="git@github.com:o/r.git"):
        pid, origin = sh._project_stamp()
    assert pid == "p9"
    assert origin == "git@github.com:o/r.git"


def test_project_stamp_failsoft_when_no_ctx():
    with patch.object(sh, "_read_session_ctx", return_value=None), \
         patch.object(sh, "_git_remote", return_value=None):
        pid, origin = sh._project_stamp()
    assert pid is None and origin == ""
