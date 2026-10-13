"""Prepare a dev-only baseline commit using trusted policy and inert Git objects."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
from types import ModuleType

BASELINE = "tools/module-size-baseline.json"
BRANCH = "automation/module-size-baseline-dev"
SUBJECT = "tools: refresh module-size baseline for dev@"
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


class BaselineError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedUpdate:
    source_sha: str
    branch: str
    expected_head: str
    commit_sha: str
    changed: bool


def git(
    repo: Path, *args: str, data: bytes | None = None,
    index: Path | None = None,
) -> bytes:
    """Run a bounded local Git operation without ambient Git redirection."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return result.stdout


def oid(repo: Path, ref: str) -> str:
    """Resolve a ref once to an immutable commit identity."""
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def safe_path(path: str) -> None:
    """Reject traversal, control characters and non-portable source paths."""
    pure = PurePosixPath(path)
    if (
        pure.is_absolute() or str(pure) != path or "\\" in path or ":" in path
        or any(part in (".", "..", ".git") for part in pure.parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise BaselineError(f"unsafe source path: {path!r}")


def policy() -> ModuleType:
    """Load the trusted sibling guard rather than the snapshot's tooling."""
    # Never import the data snapshot's copy of this executable policy.
    path = Path(__file__).with_name("check-module-size.py")
    spec = importlib.util.spec_from_file_location("trusted_module_size", path)
    if spec is None or spec.loader is None:
        raise BaselineError("trusted module-size policy is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate policy keys while decoding baseline JSON."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError(f"duplicate baseline key: {key!r}")
        result[key] = value
    return result


def blobs(repo: Path, sizes: dict[str, int]) -> dict[str, bytes]:
    """Read bounded, deduplicated blobs in one batch without checking out data."""
    if sum(sizes.values()) > MAX_TOTAL_BYTES:
        raise BaselineError("snapshot exceeds the measurement budget")
    output = git(repo, "cat-file", "--batch", data=("\n".join(sizes) + "\n").encode())
    contents: dict[str, bytes] = {}
    offset = 0
    for expected, size in sizes.items():
        end = output.index(b"\n", offset)
        if output[offset:end] != f"{expected} blob {size}".encode():
            raise BaselineError("Git returned an unexpected source blob")
        offset = end + 1
        contents[expected] = output[offset:offset + size]
        offset += size
        if output[offset:offset + 1] != b"\n":
            raise BaselineError("Git returned a truncated source blob")
        offset += 1
    if offset != len(output):
        raise BaselineError("Git returned trailing source data")
    return contents


def measure(repo: Path, source: str) -> tuple[dict[str, int], dict[str, int]]:
    """Measure regular source blobs from one pinned tree under trusted policy."""
    guard = policy()
    baseline: dict[str, int] | None = None
    counts: dict[str, int] = {}
    selected: dict[str, str] = {}
    sizes: dict[str, int] = {}
    entries = git(repo, "ls-tree", "-r", "-l", "-z", source).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if path != BASELINE and (not path.endswith(".py") or guard._is_test_file(path)):
            continue
        safe_path(path)
        mode, kind, blob, size = meta.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise BaselineError(f"source must be a regular Git blob: {path}")
        if int(size) > MAX_BLOB_BYTES:
            raise BaselineError(f"source exceeds the measurement budget: {path}")
        if path in selected:
            raise BaselineError(f"duplicate source path: {path!r}")
        selected[path] = blob
        sizes[blob] = int(size)
    if BASELINE not in selected:
        raise BaselineError("pinned dev snapshot has no regular baseline file")
    contents = blobs(repo, sizes)
    for path, blob in selected.items():
        content = contents[blob]
        if path == BASELINE:
            try:
                parsed = json.loads(content, object_pairs_hook=unique_object)
            except json.JSONDecodeError as exc:
                raise BaselineError("invalid baseline JSON") from exc
            if not isinstance(parsed, dict):
                raise BaselineError("baseline must be a JSON object")
            baseline = {}
            for key, value in parsed.items():
                safe_path(key)
                if not key.endswith(".py") or type(value) is not int or value <= 0:
                    raise BaselineError(f"invalid baseline entry: {key!r}")
                baseline[key] = value
        else:
            counts[path] = len(content.decode("utf-8", errors="replace").splitlines())
    if baseline is None:
        raise BaselineError("pinned dev snapshot has no regular baseline file")
    return baseline, counts


def verify_previous(repo: Path, previous: str, source: str) -> str:
    """Prove an existing proposal is canonical and not newer than the source."""
    parents = git(repo, "show", "-s", "--format=%P", previous).decode().split()
    if len(parents) != 1:
        raise BaselineError("existing maintenance head has no single source parent")
    parent = parents[0]
    subject = git(repo, "show", "-s", "--format=%s", previous).decode().strip()
    changed = git(repo, "diff", "--name-only", parent, previous).decode().splitlines()
    if subject != SUBJECT + parent or changed != [BASELINE]:
        raise BaselineError("existing maintenance head is not an owned baseline-only update")
    baseline, counts = measure(repo, parent)
    guard = policy()
    expected = guard.refresh_baseline(baseline, allow_widen=True, line_counts=counts)
    content = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()
    entry = git(repo, "ls-tree", previous, "--", BASELINE).split()
    if (
        expected == baseline or not entry or entry[0] != b"100644"
        or git(repo, "show", f"{previous}:{BASELINE}") != content
    ):
        raise BaselineError("existing maintenance output does not match trusted policy; refusing overwrite")
    try:
        git(repo, "merge-base", "--is-ancestor", parent, source)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        raise BaselineError("refusing to overwrite a newer or unrelated maintenance proposal") from exc
    return parent


def prepare(repo: Path, source_ref: str, previous_ref: str | None = None) -> PreparedUpdate:
    """Prepare a baseline-only commit without moving refs or working-tree files."""
    source = oid(repo, source_ref)
    previous = oid(repo, previous_ref) if previous_ref is not None else ""
    previous_source = verify_previous(repo, previous, source) if previous else None
    baseline, counts = measure(repo, source)
    guard = policy()
    updated = guard.refresh_baseline(baseline, allow_widen=True, line_counts=counts)
    violations = guard.check(updated, line_counts=counts)
    if violations:
        raise BaselineError("new over-cap source is not grandfathered:\n" + "\n".join(violations))
    if updated == baseline:
        return PreparedUpdate(source, BRANCH, previous, "", False)
    content = (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
    if previous_source == source and git(repo, "show", f"{previous}:{BASELINE}") == content:
        return PreparedUpdate(source, BRANCH, previous, previous, True)
    with tempfile.TemporaryDirectory(prefix="module-baseline-index-") as temp:
        index = Path(temp) / "index"
        git(repo, "read-tree", source, index=index)
        blob = git(repo, "hash-object", "-w", "--stdin", data=content).decode().strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{BASELINE}", index=index)
        tree = git(repo, "write-tree", index=index).decode().strip()
        commit = git(repo, "commit-tree", tree, "-p", source, "-m", SUBJECT + source).decode().strip()
    return PreparedUpdate(source, BRANCH, previous, commit, True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--source", required=True)
    parser.add_argument("--previous")
    args = parser.parse_args()
    try:
        report = prepare(args.repo, args.source, args.previous)
    except (BaselineError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        message = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            message += "\n" + exc.stderr.decode("utf-8", errors="replace")
        print(f"module baseline preparation failed: {message}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
