"""C2 statusline: canonicalization + colour rendering (pure, no I/O)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "statusline", Path(__file__).parent / "statusline.py")
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)


def test_canonical_repo_ref_github_https():
    assert sl._canonical_repo_ref("https://github.com/Nileneb/MayringCoder") == "nileneb/mayringcoder"


def test_canonical_repo_ref_github_ssh_dotgit():
    assert sl._canonical_repo_ref("git@github.com:Nileneb/app.linn.games.git") == "nileneb/app.linn.games"


def test_canonical_repo_ref_none_empty():
    assert sl._canonical_repo_ref(None) == ""
    assert sl._canonical_repo_ref("") == ""


def test_rgb_parses_hex():
    assert sl._rgb("#3b82f6") == (59, 130, 246)
    assert sl._rgb("22c55e") == (34, 197, 94)


def test_rgb_rejects_bad():
    assert sl._rgb("") is None
    assert sl._rgb("#xyz") is None
    assert sl._rgb(None) is None


def test_short_cwd_home_relative():
    import os
    home = os.path.expanduser("~")
    assert sl._short_cwd(home) == "~"
    assert sl._short_cwd(home + "/Desktop/x") == "~/Desktop/x"
    assert sl._short_cwd("/etc/foo") == "/etc/foo"
