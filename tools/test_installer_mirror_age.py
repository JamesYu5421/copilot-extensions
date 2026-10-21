from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_installer_dependency_policy import NOW, policy, settings


@pytest.mark.parametrize("evidence", ["old", "young", "mismatch", "tampered"])
@pytest.mark.parametrize("mode", ["index", "install", "project"])
def test_native_mirror_age_keeps_feed_and_download_hash(tmp_path, monkeypatch, evidence, mode):
    filename = "fixture_mirror-1.0-py3-none-any.whl"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        prefix = "fixture_mirror-1.0.dist-info/"
        wheel.writestr(prefix + "METADATA", "Metadata-Version: 2.1\nName: fixture-mirror\nVersion: 1.0\n")
        wheel.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(prefix + "RECORD", "")
    artifact = buffer.getvalue()
    digest = hashlib.sha256(artifact).hexdigest()
    if evidence == "tampered":
        with zipfile.ZipFile(buffer, "a") as wheel:
            wheel.writestr("changed.py", "changed = True\n")
    served_artifact = buffer.getvalue()
    downloads = []
    record = {
        "filename": filename, "url": "/files/" + filename,
        "hashes": {"sha256": digest},
        "upload-time": (NOW - timedelta(days=1)).isoformat(),
    }

    class Index(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(head=False)

        def do_HEAD(self):
            self.respond(head=True)

        def respond(self, *, head):
            if self.path == "/simple/fixture-mirror/":
                body = json.dumps({"meta": {"api-version": "1.2"}, "files": [record]}).encode()
                content_type = "application/vnd.pypi.simple.v1+json"
            elif self.path == "/files/" + filename:
                if not head:
                    downloads.append(self.path)
                body = served_artifact
                content_type = "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    index = f"http://127.0.0.1:{server.server_port}/simple/"
    projection = settings().replace(
        "Index {\n", f"Index {{\n url: IndexUrl {{ given: Some({json.dumps(index)}), }},\n",
    )
    fetch_original = policy._mirror._fetch
    metadata_requests = []

    def fetch(url):
        if url.startswith("https://pypi.org/"):
            metadata_requests.append(url)
            return json.dumps({"urls": [{
                "filename": filename, "digests": {"sha256": "0" * 64 if evidence == "mismatch" else digest},
                "upload_time_iso_8601": (
                    NOW - timedelta(days=1 if evidence == "young" else 30)
                ).isoformat(),
            }]}).encode(), url
        return fetch_original(url)

    uv = shutil.which("uv")
    assert uv, "native UV is required"
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON")) and key != "VIRTUAL_ENV"
    }
    environment["UV_CACHE_DIR"] = str(tmp_path / "cache")
    environment["UV_NO_CONFIG"] = "true"
    environment["UV_DEFAULT_INDEX"] = index
    environment["UV_PYTHON"] = sys.executable
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    cutoffs = policy.cutoff_arguments(projection, now=NOW)
    monkeypatch.setattr(policy._mirror, "_fetch", fetch)

    def invoke():
        if mode == "project":
            project = tmp_path / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text(
                '[project]\nname="fixture-project"\nversion="1.0"\nrequires-python=">=3.9"\n'
                'dependencies=["fixture-mirror==1.0 ; sys_platform == \'linux\'"]\n',
                encoding="utf-8",
            )
            return policy.run_uv_project(
                uv, project, ["python", "-c", "print('project-ran')"],
                env=environment, now=NOW, capture_output=True, text=True, timeout=30,
            ), None
        if mode == "install":
            return policy.run_uv_install(
                uv, ["--no-config", "--python", sys.executable,
                     "--target", str(tmp_path / "installed"), "fixture-mirror==1.0"],
                env=environment, cwd=tmp_path, now=NOW, capture_output=True, text=True, timeout=30,
            ), None
        arguments = policy._mirror.mirror_index_arguments(
            projection, ["fixture-mirror==1.0"], cutoffs, tmp_path / "index",
            balanced=policy._balanced, error_type=policy.DependencyPolicyError, now=NOW,
        )
        return subprocess.run(
            [uv, "pip", "install", "--no-config", "--default-index", index,
             "--python", sys.executable, "--target", str(tmp_path / "installed"),
             *arguments, *cutoffs, "fixture-mirror==1.0"],
            env=environment, capture_output=True, text=True, timeout=30,
            creationflags=policy._HEADLESS,
        ), arguments

    try:
        if evidence == "tampered":
            with pytest.raises(policy.DependencyPolicyError) as error:
                invoke()
            assert "hash mismatch" in str(error.value.__cause__)
            assert not (tmp_path / "index" / "fixture-mirror" / "index.html").exists()
            return
        result, arguments = invoke()
        assert (result.returncode == 0) == (evidence == "old"), result.stdout + result.stderr
        assert metadata_requests == ["https://pypi.org/pypi/fixture-mirror/1.0/json"]
        if mode == "index":
            assert bool(arguments) == (evidence == "old")
        elif mode == "project" and evidence == "old":
            assert result.stdout.strip() == "project-ran"
            repeated = policy.run_uv_project(
                uv, tmp_path / "project", ["python", "-c", "print('project-ran-again')"],
                env=environment, now=NOW, capture_output=True, text=True, timeout=30,
            )
            assert repeated.returncode == 0, repeated.stdout + repeated.stderr
            assert repeated.stdout.strip() == "project-ran-again"
        if evidence == "old":
            assert downloads and set(downloads) == {"/files/" + filename}
        else:
            assert downloads == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("reason", ["yanked", "empty-yank", "missing-age", "future", "stricter", "unknown-file"])
def test_unproven_or_more_recent_canonical_files_are_not_corrected(tmp_path, reason):
    recent = (NOW - timedelta(days=1)).isoformat()
    record = {
        "filename": "fixture_mirror-1.0-py3-none-any.whl", "url": "artifact.whl",
        "hashes": {"sha256": "a" * 64}, "upload-time": recent,
    }
    canonical = {
        "filename": record["filename"], "digests": {"sha256": "a" * 64},
        "upload_time_iso_8601": (NOW - timedelta(days=10)).isoformat(),
    }
    cutoff = (NOW - timedelta(days=7)).isoformat()
    if reason == "yanked":
        canonical["yanked"] = True
    elif reason == "empty-yank":
        record["yanked"] = ""
    elif reason == "missing-age":
        del canonical["upload_time_iso_8601"]
    elif reason == "future":
        record["upload-time"] = (NOW + timedelta(days=1)).isoformat()
    elif reason == "stricter":
        cutoff = (NOW - timedelta(days=14)).isoformat()
    else:
        canonical["filename"] = "unknown.whl"
    projection = settings().replace(
        "Index {\n", 'Index {\n url: IndexUrl { given: Some("https://mirror.example/simple/"), },\n',
    )

    def fetch(url):
        data = {"urls": [canonical]} if url.startswith("https://pypi.org/") else {"files": [record]}
        return json.dumps(data).encode(), url

    assert policy._mirror.mirror_index_arguments(
        projection, ["fixture-mirror==1.0"], ["--exclude-newer", cutoff], tmp_path / "index",
        balanced=policy._balanced, error_type=policy.DependencyPolicyError, now=NOW, fetch=fetch,
    ) == []
    assert not (tmp_path / "index").exists()


def test_corrected_page_preserves_other_versions_and_late_same_version_files(tmp_path, monkeypatch):
    recent = (NOW - timedelta(days=1)).isoformat()
    files = [{
        "filename": filename, "url": filename, "hashes": {"sha256": digest * 64},
        "upload-time": recent, "yanked": "" if version == "2.0" else False,
    } for filename, version, digest in [
        ("fixture_mirror-1.0-py3-none-any.whl", "1.0", "a"),
        ("fixture_mirror-1.0.tar.gz", "1.0", "b"),
        ("fixture_mirror-2.0-py3-none-any.whl", "2.0", "c"),
    ]]
    canonical = [{
        "filename": file["filename"], "digests": file["hashes"],
        "upload_time_iso_8601": (NOW - timedelta(days=30 if position == 0 else 1)).isoformat(),
    } for position, file in enumerate(files[:2])]

    def fetch(url):
        return json.dumps(
            {"urls": canonical} if url.startswith("https://pypi.org/") else {"files": files},
        ).encode(), url

    monkeypatch.setattr(policy._mirror, "_verified_artifact", lambda *args: (tmp_path / "verified.whl").as_uri())
    projection = settings().replace(
        "Index {\n", 'Index {\n url: IndexUrl { given: Some("https://mirror.example/simple/"), },\n',
    )
    result = policy._mirror.mirror_index_arguments(
        projection, ["fixture-mirror==1.0"], policy.cutoff_arguments(projection, now=NOW),
        tmp_path / "index", balanced=policy._balanced, error_type=policy.DependencyPolicyError,
        now=NOW, fetch=fetch,
    )
    assert result == ["--index", (tmp_path / "index").as_uri()]
    page = policy._mirror._files((tmp_path / "index" / "fixture-mirror" / "index.html").read_bytes())
    assert len(page) == 3
    assert page[0]["upload-time"] == canonical[0]["upload_time_iso_8601"]
    assert page[1]["upload-time"] == page[2]["upload-time"] == recent
    assert page[1]["url"].startswith("https://mirror.example/")
    assert page[2]["yanked"] is True


def test_manager_runner_launches_fresh_project_without_resolving_it_first(tmp_path):
    root = Path(policy.__file__).resolve().parents[2]
    slot = tmp_path / "slot with spaces"
    scripts = slot / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(root / "worktree-manager" / "run.py", slot / "run.py")
    for name in ("installer_dependency_policy.py", "installer_dependency_config.py"):
        shutil.copyfile(Path(policy.__file__).with_name(name), scripts / name)
    (slot / "pyproject.toml").write_text(
        '[project]\nname="fixture-manager"\nversion="1.0"\nrequires-python=">=3.9"\ndependencies=[]\n',
        encoding="utf-8",
    )
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON")) and key != "VIRTUAL_ENV"
    }
    environment.update(UV_OFFLINE="1", UV_PYTHON=sys.executable, UV_CACHE_DIR=str(tmp_path / "cache"))
    result = subprocess.run(
        [shutil.which("uv"), "run", "--quiet", "--no-project", "--no-config", "--no-env-file",
         "python", "-I", str(slot / "run.py"), "python", "-c",
         "import sys; print(sys.argv[1])", "round trip with spaces"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
        creationflags=policy._HEADLESS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "round trip with spaces"
