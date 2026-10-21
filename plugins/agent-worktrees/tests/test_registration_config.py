"""Preserve user-owned machine settings when registration refreshes its config."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_worktrees import __main__ as main


def test_reregister_preserves_binding_and_nested_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    original = {
        "repo_name": "example",
        "knowledge_repo": "example-knowledge",
        "headless": True,
        "terminal_profiles": ["custom"],
        "custom": {"enabled": False},
        "repos": {
            "example": {
                "anchor": "old-anchor",
                "remote": "upstream",
                "worktree_root": "custom-worktrees",
                "custom": {"retain": True},
            },
            "other": {"anchor": "other-anchor", "remote": "other-remote"},
        },
    }
    path.write_text(yaml.safe_dump(original), encoding="utf-8")

    main._write_config(path, tmp_path / "anchor", "machine", "windows", "example")

    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    original["repos"]["example"]["anchor"] = str(tmp_path / "anchor")
    assert updated == original


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_adoption_options_are_applied(tmp_path, exists):
    path = tmp_path / "config.yaml"
    if exists:
        path.write_text("knowledge_repo: example-knowledge\n", encoding="utf-8")

    main._write_config(
        path, tmp_path / "anchor", "machine", "windows", "example",
        headless=True, no_terminal_profile=True,
    )

    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert updated["repo_name"] == "example"
    assert updated["repos"]["example"] == {
        "anchor": str(tmp_path / "anchor"), "remote": "origin",
    }
    assert updated["headless"] is True
    assert updated["terminal_profiles"] == []
    if exists:
        assert updated["knowledge_repo"] == "example-knowledge"


@pytest.mark.parametrize("content", [
    "knowledge_repo: [unterminated",
    "- not-a-mapping\n",
    "repos: []\n",
    "repos:\n  example: false\n",
])
def test_invalid_existing_config_is_not_overwritten(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises((ValueError, yaml.YAMLError)):
        main._write_config(path, tmp_path / "anchor", "machine", "windows", "example")

    assert path.read_text(encoding="utf-8") == content


def test_empty_existing_config_can_be_registered(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")

    main._write_config(path, tmp_path / "anchor", "machine", "linux", "example")

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "repo_name": "example",
        "repos": {"example": {"anchor": str(tmp_path / "anchor"), "remote": "origin"}},
    }


def test_registration_writer_preserves_binding_in_fresh_process(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("knowledge_repo: example-knowledge\n", encoding="utf-8")
    source = Path(main.__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import agent_worktrees.__main__ as main
main._write_config(Path(sys.argv[2]), Path(sys.argv[3]), "machine", "linux", "example")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), str(path), str(tmp_path / "anchor")],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["knowledge_repo"] == "example-knowledge"
