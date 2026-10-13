"""Exercise trusted Git-data preparation and the real dev publication step."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import prepare_module_baseline as updater  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return updater.git(repo, *args).decode().strip()


def snapshot(repo: Path, counts: dict[str, int], baseline: dict[str, int]) -> str:
    for name, count in counts.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n" * count, encoding="utf-8")
    path = repo / updater.BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "committed dev data")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "dev")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    return tmp_path


def baseline_at(repo: Path, commit: str) -> dict[str, int]:
    return json.loads(git(repo, "show", f"{commit}:{updater.BASELINE}"))


@pytest.mark.parametrize("count,expected", [(1300, 1300), (1100, 1100), (1000, None)])
def test_trusted_policy_widens_tightens_and_graduates(
    repo: Path, count: int, expected: int | None,
) -> None:
    source = snapshot(repo, {"src/legacy.py": count}, {"src/legacy.py": 1200})
    before = git(repo, "status", "--porcelain")
    prepared = updater.prepare(repo, source)
    assert prepared.changed
    assert baseline_at(repo, prepared.commit_sha) == (
        {} if expected is None else {"src/legacy.py": expected}
    )
    assert git(repo, "show", "-s", "--format=%P", prepared.commit_sha) == source
    assert git(repo, "diff", "--name-only", source, prepared.commit_sha) == updater.BASELINE
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "rev-parse", "dev") == source


def test_unchanged_policy_creates_no_commit(repo: Path) -> None:
    source = snapshot(repo, {"src/legacy.py": 1200}, {"src/legacy.py": 1200})
    prepared = updater.prepare(repo, source)
    assert not prepared.changed and prepared.commit_sha == ""


def test_new_over_cap_source_is_never_auto_grandfathered(repo: Path) -> None:
    source = snapshot(repo, {"src/new.py": 1001}, {})
    with pytest.raises(updater.BaselineError, match="not grandfathered"):
        updater.prepare(repo, source)


def test_committed_objects_not_mutable_worktree_or_candidate_tools(repo: Path) -> None:
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    marker = repo / "executed"
    for name in ("tools/check-module-size.py", "tools/prepare_module_baseline.py", "sitecustomize.py"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"open({str(marker)!r}, 'w').write('executed')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "hostile tooling is inert source data")
    source = git(repo, "rev-parse", "HEAD")
    (repo / "src/legacy.py").write_text("pass\n", encoding="utf-8")
    (repo / updater.BASELINE).write_text("{}", encoding="utf-8")
    prepared = updater.prepare(repo, source)
    assert baseline_at(repo, prepared.commit_sha) == {"src/legacy.py": 1300}
    assert not marker.exists()


@pytest.mark.parametrize("bad", ["../outside.py", "/outside.py", "C:/outside.py", "a\\b.py", "a\nb.py"])
def test_unsafe_baseline_paths_fail_closed(repo: Path, bad: str) -> None:
    source = snapshot(repo, {"src/small.py": 2}, {bad: 1200})
    with pytest.raises(updater.BaselineError, match="unsafe"):
        updater.prepare(repo, source)


@pytest.mark.parametrize("bad", [True, "1200", -1, 1.5])
def test_malformed_ceiling_fails_closed(repo: Path, bad: object) -> None:
    snapshot(repo, {"src/small.py": 2}, {})
    (repo / updater.BASELINE).write_text(json.dumps({"src/small.py": bad}), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "malformed baseline")
    with pytest.raises(updater.BaselineError, match="invalid baseline entry"):
        updater.prepare(repo, "HEAD")


def test_git_symlink_source_is_not_followed(repo: Path) -> None:
    source = snapshot(repo, {"src/small.py": 2}, {})
    blob = updater.git(repo, "hash-object", "-w", "--stdin", data=b"../../outside").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},src/link.py")
    git(repo, "commit", "-q", "-m", "unsafe source link")
    with pytest.raises(updater.BaselineError, match="regular Git blob"):
        updater.prepare(repo, "HEAD")
    assert git(repo, "rev-parse", f"{source}^{{commit}}") == source


def test_duplicate_baseline_keys_are_not_silently_collapsed(repo: Path) -> None:
    snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    (repo / updater.BASELINE).write_text(
        '{"src/legacy.py": 1200, "src/legacy.py": 9999}', encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ambiguous baseline")
    with pytest.raises(updater.BaselineError, match="duplicate baseline key"):
        updater.prepare(repo, "HEAD")


def test_replay_uses_identical_existing_proposal(repo: Path) -> None:
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    first = updater.prepare(repo, source)
    second = updater.prepare(repo, source, first.commit_sha)
    assert second.commit_sha == first.commit_sha == second.expected_head


def test_newer_proposal_cannot_be_overwritten_by_older_data(repo: Path) -> None:
    old = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    new = snapshot(repo, {"src/legacy.py": 1400}, {"src/legacy.py": 1200})
    previous = updater.prepare(repo, new)
    with pytest.raises(updater.BaselineError, match="newer or unrelated"):
        updater.prepare(repo, old, previous.commit_sha)


def test_unknown_branch_content_is_not_overwritten(repo: Path) -> None:
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    previous = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1400})
    with pytest.raises(updater.BaselineError, match="not an owned"):
        updater.prepare(repo, source, previous)


def test_claimed_subject_is_not_proof_of_generated_baseline(repo: Path) -> None:
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    (repo / updater.BASELINE).write_text('{"src/legacy.py": 9999}\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", updater.SUBJECT + source)
    previous = git(repo, "rev-parse", "HEAD")
    with pytest.raises(updater.BaselineError, match="does not match trusted policy"):
        updater.prepare(repo, source, previous)


def test_blobs_are_batched_and_duplicate_content_is_measured_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = snapshot(repo, {"src/a.py": 1100, "src/b.py": 1100},
                      {"src/a.py": 1200, "src/b.py": 1200})
    original = updater.git
    batches: list[bytes] = []

    def observed(
        path: Path, *args: str, data: bytes | None = None,
        index: Path | None = None,
    ) -> bytes:
        if args == ("cat-file", "--batch"):
            assert data is not None
            batches.append(data)
        return original(path, *args, data=data, index=index)

    monkeypatch.setattr(updater, "git", observed)
    updater.prepare(repo, source)
    assert len(batches) == 1
    assert len(batches[0].splitlines()) == 2  # one baseline plus one identical source blob


def workflow() -> dict:
    return yaml.safe_load(
        (TOOLS.parent / ".github/workflows/module-size-baseline-widen.yml").read_text(encoding="utf-8")
    )


def test_actual_workflow_separates_trusted_control_data_and_write_credentials() -> None:
    cfg = workflow()
    triggers = cfg.get("on", cfg.get(True))
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert "workflow_dispatch" not in triggers
    assert cfg["permissions"] == {"contents": "read", "pull-requests": "read"}
    job = cfg["jobs"]["widen"]
    assert "head_repository.full_name == github.repository" in job["if"]
    assert "workflow_run.event == 'push'" in job["if"]
    checkout = job["steps"][0]["with"]
    assert checkout["ref"] == "${{ github.sha }}"
    assert checkout["persist-credentials"] is False
    prepare = next(step for step in job["steps"] if step.get("id") == "prepare")
    assert "GH_TOKEN" not in prepare["env"]
    assert "prepare_module_baseline.py" in prepare["run"]
    publish = next(step for step in job["steps"] if "exact branch lease" in step.get("name", ""))
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.APERTURE_RELEASE_TOKEN }}"
    assert "--force-with-lease=" in publish["run"]
    assert "--base dev" in publish["run"] and "pr merge" not in publish["run"]


@pytest.mark.parametrize("branch", [
    "automation/module-size-baseline-widen", updater.BRANCH,
])
def test_actual_reminder_admission_does_not_exempt_baseline_writers(branch: str) -> None:
    node = shutil.which("node")
    assert node is not None, "the reminder contract requires the CI Node runtime"
    cfg = yaml.safe_load(
        (TOOLS.parent / ".github/workflows/base-branch-reminder.yml").read_text(encoding="utf-8")
    )
    script = next(step["with"]["script"] for job in cfg["jobs"].values()
                  for step in job.get("steps", []) if "script" in step.get("with", {}))
    start = script.index("async function isLegitimateAutomatedMainPr()")
    end = script.index("if (await isLegitimateAutomatedMainPr())")
    code = (
        "const PR_AUTHOR='owner',REPO_OWNER='owner',HEAD_REF=" + json.dumps(branch) + ";"
        "const pull_number=1,context={repo:{owner:'example',repo:'repo'}};"
        "const github={rest:{pulls:{listFiles:'files'}},"
        "paginate:async()=>[{filename:'tools/module-size-baseline.json'}]};"
        "(async()=>{" + script[start:end] +
        "console.log(await isLegitimateAutomatedMainPr());})().catch(e=>{console.error(e);process.exit(1)});"
    )
    result = subprocess.run(
        [node, "-e", code], capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "false"


@pytest.fixture
def publisher(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    if os.name == "nt":
        pytest.skip("actual Bash publication boundary is exercised on Linux CI")
    origin = tmp_path_factory.mktemp("baseline-origin")
    git(origin, "init", "--bare", "-q")
    git(repo, "remote", "add", "origin", str(origin))
    bin_dir = tmp_path_factory.mktemp("baseline-cli")
    calls = bin_dir / "calls.jsonl"
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args=sys.argv[1:]\n"
        "def advance(when):\n"
        " if os.environ.get('ADVANCE_REF') and os.environ.get('ADVANCE_ON','list')==when:\n"
        "  with open(os.environ['ADVANCE_REF'],'w') as out: out.write(os.environ['ADVANCE_SHA']+'\\n')\n"
        "if args[:2]==['pr','list']:\n"
        " advance('list')\n"
        " print(os.environ.get('PR_LIST','[]'))\n"
        "elif args[:2] in (['pr','create'],['pr','edit'],['pr','ready']):\n"
        f" with open({str(calls)!r},'a') as out: out.write(json.dumps(args)+'\\n')\n"
        " if args[:2]==['pr','create']: print('https://example.com/example/repo/pull/1')\n"
        " if args[:2]==['pr','edit']: advance('edit')\n"
        " if args[:2]==['pr','ready'] and '--undo' in args:\n"
        "  if os.environ.get('CHECK_DRAFT_HEAD'):\n"
        "   with open(os.environ['LEASE_REF_PATH']) as ref: observed=ref.read()\n"
        "   with open(os.environ['CHECK_DRAFT_HEAD'],'w') as out: out.write(observed)\n"
        "  advance('draft')\n"
        "elif args[:2]!=['auth','setup-git']: raise SystemExit(2)\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               GITHUB_REPOSITORY="example/repo", BASELINE_BRANCH=updater.BRANCH)
    env["RUNNER_TEMP"] = str(bin_dir)
    return calls, env


def publish(repo: Path, update: updater.PreparedUpdate, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    report = Path(env["RUNNER_TEMP"]) / "module-baseline.json"
    report.write_text(json.dumps(asdict(update)), encoding="utf-8")
    env = dict(env, SOURCE_SHA=update.source_sha)
    step = next(s for s in workflow()["jobs"]["widen"]["steps"] if "exact branch lease" in s.get("name", ""))
    bash = shutil.which("bash")
    assert bash is not None, "the publication boundary requires Bash"
    return subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def test_actual_publisher_changes_only_dev_proposal_not_main_or_candidate(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    calls, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    git(repo, "branch", "main", source)
    git(repo, "branch", "release/promote-fixture", source)
    git(repo, "push", "-q", "origin", "dev", "main", "release/promote-fixture")
    prepared = updater.prepare(repo, source)
    result = publish(repo, prepared, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == source
    assert git(repo, "ls-remote", "origin", "refs/heads/release/promote-fixture").split()[0] == source
    assert git(repo, "ls-remote", "origin", "refs/heads/dev").split()[0] == source
    request = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    assert request[request.index("--base") + 1] == "dev"
    assert "--draft" in request
    assert git(repo, "ls-remote", "origin", f"refs/heads/{updater.BRANCH}").split()[0] == prepared.commit_sha


def test_old_main_baseline_collision_is_reproduced_without_a_new_main_writer(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    _, env = publisher
    initial = snapshot(repo, {"src/legacy.py": 1200}, {"src/legacy.py": 1500})
    git(repo, "branch", "main", initial)
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1400})
    candidate = git(repo, "commit-tree", git(repo, "rev-parse", f"{source}^{{tree}}"),
                    "-p", initial, "-m", "generated release fixture")
    git(repo, "branch", "release/promote-fixture", candidate)
    git(repo, "checkout", "-q", "main")
    legacy_main = snapshot(repo, {"src/legacy.py": 1200}, {"src/legacy.py": 1200})
    git(repo, "checkout", "-q", "dev")
    collision = subprocess.run(
        ["git", "merge-tree", "--write-tree", legacy_main, candidate],
        cwd=repo, capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert collision.returncode == 1 and updater.BASELINE in collision.stdout
    git(repo, "push", "-q", "origin", "main", "dev", "release/promote-fixture")
    result = publish(repo, updater.prepare(repo, source), env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == legacy_main
    assert git(repo, "ls-remote", "origin", "refs/heads/release/promote-fixture").split()[0] == candidate


def test_actual_publisher_refuses_dev_advancement(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    calls, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    prepared = updater.prepare(repo, source)
    snapshot(repo, {"src/legacy.py": 1400}, {"src/legacy.py": 1200})
    git(repo, "push", "-q", "origin", "dev")
    result = publish(repo, prepared, env)
    assert result.returncode != 0 and "dev advanced" in result.stdout
    assert not calls.exists()


@pytest.fixture
def existing_ready(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> tuple[updater.PreparedUpdate, Path, dict[str, str]]:
    calls, env = publisher
    old = snapshot(repo, {"src/legacy.py": 1250}, {"src/legacy.py": 1200})
    previous = updater.prepare(repo, old)
    git(repo, "push", "-q", "origin", "dev", f"{previous.commit_sha}:refs/heads/{updater.BRANCH}")
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    git(repo, "push", "-q", "origin", "dev")
    prepared = updater.prepare(repo, source, previous.commit_sha)
    origin = Path(git(repo, "remote", "get-url", "origin"))
    env = dict(env, PR_LIST=json.dumps([{
        "number": 17, "baseRefName": "dev", "isDraft": False,
        "headRefOid": previous.commit_sha, "isCrossRepository": False,
    }]), LEASE_REF_PATH=str(origin / "refs/heads" / updater.BRANCH),
        CHECK_DRAFT_HEAD=str(Path(env["RUNNER_TEMP"]) / "draft-head"))
    return prepared, calls, env


def test_existing_ready_proposal_is_parked_before_its_branch_changes(
    repo: Path, existing_ready: tuple[updater.PreparedUpdate, Path, dict[str, str]],
) -> None:
    prepared, calls, env = existing_ready
    result = publish(repo, prepared, env)
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert requests[0][:2] == ["pr", "ready"] and "--undo" in requests[0]
    assert Path(env["CHECK_DRAFT_HEAD"]).read_text().strip() == prepared.expected_head
    assert requests[-1][:2] == ["pr", "ready"] and "--undo" not in requests[-1]


def test_existing_proposal_stays_draft_when_dev_moves_after_its_push(
    repo: Path, existing_ready: tuple[updater.PreparedUpdate, Path, dict[str, str]],
) -> None:
    prepared, calls, env = existing_ready
    newer = snapshot(repo, {"src/legacy.py": 1400}, {"src/legacy.py": 1200})
    git(repo, "push", "-q", "origin", f"{newer}:refs/heads/rehearsal-new-dev")
    origin = Path(git(repo, "remote", "get-url", "origin"))
    result = publish(repo, prepared, dict(env, ADVANCE_ON="edit",
        ADVANCE_REF=str(origin / "refs/heads/dev"), ADVANCE_SHA=newer))
    assert result.returncode != 0 and "proposal remains draft" in result.stdout
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert requests[0][:2] == ["pr", "ready"] and "--undo" in requests[0]
    assert Path(env["CHECK_DRAFT_HEAD"]).read_text().strip() == prepared.expected_head
    assert not any(r[:2] == ["pr", "ready"] and "--undo" not in r for r in requests)


def test_originally_draft_human_hold_is_never_automatically_cleared(
    repo: Path, existing_ready: tuple[updater.PreparedUpdate, Path, dict[str, str]],
) -> None:
    prepared, calls, env = existing_ready
    identity = json.loads(env["PR_LIST"])
    identity[0]["isDraft"] = True
    result = publish(repo, prepared, dict(env, PR_LIST=json.dumps(identity)))
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not any(r[:2] == ["pr", "ready"] for r in requests)


def test_armed_auto_merge_is_refused_without_mutating_the_proposal(
    repo: Path, existing_ready: tuple[updater.PreparedUpdate, Path, dict[str, str]],
) -> None:
    prepared, calls, env = existing_ready
    identity = json.loads(env["PR_LIST"])
    identity[0]["autoMergeRequest"] = {"mergeMethod": "SQUASH"}
    result = publish(repo, prepared, dict(env, PR_LIST=json.dumps(identity)))
    assert result.returncode != 0 and "auto-merge armed" in result.stdout
    assert not calls.exists()
    assert git(repo, "ls-remote", "origin", f"refs/heads/{updater.BRANCH}").split()[0] == prepared.expected_head


def test_actual_publisher_detects_advancement_during_publication(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    calls, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    prepared = updater.prepare(repo, source)
    git(repo, "push", "-q", "origin", "dev")
    newer = snapshot(repo, {"src/legacy.py": 1400}, {"src/legacy.py": 1200})
    git(repo, "push", "-q", "origin", f"{newer}:refs/heads/rehearsal-new-dev")
    origin = Path(git(repo, "remote", "get-url", "origin"))
    result = publish(repo, prepared, dict(env, ADVANCE_REF=str(origin / "refs/heads/dev"),
                                         ADVANCE_SHA=newer))
    assert result.returncode != 0 and "dev advanced before publication" in result.stdout
    assert git(repo, "ls-remote", "origin", f"refs/heads/{updater.BRANCH}") == ""
    assert not calls.exists()


def test_actual_publisher_lease_refuses_an_intervening_branch_writer(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    calls, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    prepared = updater.prepare(repo, source)
    git(repo, "push", "-q", "origin", "dev", f"{source}:refs/heads/{updater.BRANCH}")
    result = publish(repo, prepared, env)
    assert result.returncode != 0
    assert git(repo, "ls-remote", "origin", f"refs/heads/{updater.BRANCH}").split()[0] == source
    assert not calls.exists()


@pytest.mark.parametrize("branch", [
    "automation/module-size-baseline-widen", updater.BRANCH,
])
def test_actual_main_gate_rejects_baseline_only_writers(
    repo: Path, publisher: tuple[Path, dict[str, str]], branch: str,
) -> None:
    _, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    proposal = updater.prepare(repo, source)
    ci = yaml.safe_load((TOOLS.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(s for s in ci["jobs"]["main-gate"]["steps"] if s.get("run"))
    env = dict(env, PR_AUTHOR="owner", REPO_OWNER="owner", HEAD_REF=branch,
               BASE_SHA=source, HEAD_SHA=proposal.commit_sha, PR_COMMITS="1")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode != 0
    assert "Baseline policy changes require reviewed dev PRs" in result.stdout


def test_actual_required_ci_rejects_stale_measured_parent(
    repo: Path, publisher: tuple[Path, dict[str, str]],
) -> None:
    _, env = publisher
    source = snapshot(repo, {"src/legacy.py": 1300}, {"src/legacy.py": 1200})
    proposal = updater.prepare(repo, source)
    snapshot(repo, {"src/legacy.py": 1400}, {"src/legacy.py": 1200})
    git(repo, "push", "-q", "origin", "dev")
    ci = yaml.safe_load((TOOLS.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(s for job in ci["jobs"].values() for s in job.get("steps", [])
                if s.get("name") == "Baseline automation must still measure current dev")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=repo,
        env=dict(env, HEAD_SHA=proposal.commit_sha), capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode != 0 and "Stale baseline proposal" in result.stdout
