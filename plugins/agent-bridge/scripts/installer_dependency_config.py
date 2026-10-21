"""Private UV configuration, retention, and mirror-age evidence primitives."""
from __future__ import annotations

import json
import hashlib
import html
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

_STRUCTURED_CUTOFF = re.compile(
    r"ExcludeNewerValue\s*\{\s*timestamp:\s*([^,\s]+),\s*span:\s*"
    r"(?:None|Some\(\s*ExcludeNewerSpan\(\s*([^,\s]+),?\s*\),?\s*\)),?\s*\}"
)


def anchor_roots(helper, *, contained, toml, identity, error_type):
    """Attribute CLI source roots to the helper's payload and canonical roster."""
    owner_payload = None
    repository = None
    if helper.parent.name == "scripts":
        payload = helper.parent.parent
        try:
            manifest = json.loads(contained(payload / "plugin.json", (payload,)).read_text(encoding="utf-8"))
            project = toml(contained(payload / "pyproject.toml", (payload,)))["project"]
            if (identity(manifest["name"]) == identity(project["name"])
                    and manifest["version"] == project["version"]
                    and manifest.get("repository") == "https://github.com/ThomasMichon/copilot-extensions"):
                candidate = payload.parent.parent
                if (payload.parent.name == "plugins"
                        and (candidate / ".github" / "plugin" / "marketplace.json").is_file()):
                    repository, owner_payload = candidate, payload
                else:
                    return (payload,)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if repository is None:
            return ()
    # Canonical source graphs are roster-owned plugins plus their explicitly
    # named library references. Standalone projects need caller attestation.
    if repository is None:
        if helper.parent.name != "installer-engine" or helper.parent.parent.name != "libs":
            return ()
        repository = helper.parents[2]
    roster = repository / ".github" / "plugin" / "marketplace.json"
    if not roster.is_file():
        return ()
    try:
        data = json.loads(contained(roster, (repository,)).read_text(encoding="utf-8"))
        if data.get("name") != "copilot-extensions":
            return ()
        roots = set()
        pending = []
        for entry in data["plugins"]:
            source = entry.get("source", "")
            if not isinstance(source, str) or not re.fullmatch(r"plugins/[A-Za-z0-9_-]+", source):
                continue
            path = contained(repository / source, (repository,))
            manifest_path = contained(path / "plugin.json", (repository,))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["name"] != entry["name"] or manifest["version"] != entry["version"]:
                raise error_type("canonical plugin roster identity is inconsistent")
            if (path / "pyproject.toml").is_file():
                project = toml(contained(path / "pyproject.toml", (repository,)))
                if identity(project["project"]["name"]) != identity(entry["name"]):
                    raise error_type("canonical project identity is inconsistent")
                roots.add(path)
                pending.append((path, path / "libs"))
        if owner_payload is not None and owner_payload not in roots:
            raise error_type("installer helper owner is not a roster-attributed project")
        while pending:
            source, library_root = pending.pop()
            mappings = toml(contained(source / "pyproject.toml", (repository,))).get("tool", {}).get("uv", {}).get("sources", {})
            for name, entries in mappings.items():
                for entry in entries if isinstance(entries, list) else [entries]:
                    if not isinstance(entry, dict) or "path" not in entry:
                        continue
                    child = contained(source / entry["path"], (repository,))
                    if not (child.is_relative_to(repository / "libs")
                            or child.is_relative_to(library_root)):
                        raise error_type("canonical local graph lacks library attribution")
                    if identity(toml(contained(child / "pyproject.toml", (repository,)))["project"]["name"]) != identity(name):
                        raise error_type("canonical library identity is inconsistent")
                    if child not in roots:
                        roots.add(child)
                        child_root = repository / "libs" if child.is_relative_to(repository / "libs") else library_root
                        pending.append((child, child_root))
        return tuple(sorted(roots))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise error_type("canonical source ownership could not be established") from exc


def config_inputs(arguments, environment, base, *, argument_pairs, toml, error_type):
    pairs = dict(argument_pairs(arguments))
    if "--no-config" in pairs or environment.get("UV_NO_CONFIG", "").lower() in ("1", "true"):
        return []
    explicit = pairs.get("--config-file") or environment.get("UV_CONFIG_FILE")
    if explicit:
        return [(base / explicit, False)]
    candidates = []
    project = base / (pairs.get("--project") or environment.get("UV_PROJECT") or ".")
    if "--isolated" not in pairs and environment.get("UV_ISOLATED", "").lower() not in ("1", "true"):
        for directory in (project, *project.parents):
            uv = directory / "uv.toml"
            manifest = directory / "pyproject.toml"
            if uv.is_file():
                candidates.append((uv, False))
                break
            if manifest.is_file():
                tool = toml(manifest).get("tool", {})
                if not isinstance(tool, dict):
                    raise error_type("configured project metadata is unsupported")
                if "uv" in tool:
                    if not isinstance(tool["uv"], dict):
                        raise error_type("configured UV metadata is unsupported")
                    candidates.append((manifest, True))
                    break
    if os.name == "nt":
        if environment.get("APPDATA"):
            candidates.append((Path(environment["APPDATA"]) / "uv" / "uv.toml", False))
        if environment.get("SYSTEMDRIVE"):
            candidates.append((
                Path(environment["SYSTEMDRIVE"] + "\\") / "ProgramData" / "uv" / "uv.toml", False,
            ))
    else:
        user = Path(environment.get("XDG_CONFIG_HOME") or (
            Path(environment.get("HOME") or Path.home()) / ".config"
        )) / "uv" / "uv.toml"
        candidates.append((user, False))
        system = Path("/etc/uv/uv.toml")
        for directory in (environment.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":"):
            if not directory:
                break
            candidate = base / directory / "uv" / "uv.toml"
            if candidate.is_file():
                system = candidate
                break
        candidates.append((system, False))
    return [(path, project) for path, project in candidates if path.is_file()]


def metadata_needs_caller_layout(value):
    if isinstance(value, dict):
        # Source-bearing metadata can contain relative references or traversals
        # outside and back into a source root. Keep its established layout.
        if any(value.get(key) for key in (
            "sources", "dependency-groups", "dev-dependencies", "constraint-dependencies",
            "override-dependencies", "build-constraint-dependencies",
            "dependency-metadata", "extra-build-dependencies",
        )):
            return True
        return any(metadata_needs_caller_layout(item) for item in value.values())
    if isinstance(value, list):
        return any(metadata_needs_caller_layout(item) for item in value)
    return isinstance(value, str) and (
        "file:" in value.lower() or re.search(r"(?:^|[\s/\\])\.\.(?:[/\\]|$)", value) is not None
    )


def cutoff_value(value: str, now: datetime, *, error_type) -> datetime:
    value = value.strip().removesuffix(",").strip()
    structured = _STRUCTURED_CUTOFF.fullmatch(value)
    if structured:
        cutoff = cutoff_value(f"Absolute({structured[1]})", now, error_type=error_type)
        if structured[2] is not None:
            relative = cutoff_value(
                f"Relative(ExcludeNewerSpan({structured[2]}))", now, error_type=error_type,
            )
            cutoff = min(cutoff, relative)
        return cutoff
    absolute = re.fullmatch(r"Absolute\(\s*([^,\s]+),?\s*\)", value)
    if absolute:
        timestamp = re.sub(
            r"\.(\d{1,9})(?=Z$|[+-]\d{2}:\d{2}$)",
            lambda match: "." + match[1][:6].ljust(6, "0"), absolute[1],
        )
        try:
            result = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise error_type("UV returned an invalid cutoff timestamp") from exc
        if result.tzinfo is None:
            raise error_type("UV returned a cutoff without a timezone")
        return result.astimezone(timezone.utc)
    relative = re.fullmatch(
        r"Relative\(\s*ExcludeNewerSpan\(\s*([^,\s]+),?\s*\),?\s*\)",
        value,
    )
    if relative:
        span = relative[1]
        parts = re.findall(r"(\d+)(w|d|h|m|s)", span)
        if not parts or "".join(number + unit for number, unit in parts) != span:
            raise error_type("unsupported UV relative cutoff; use an RFC3339 timestamp")
        seconds = sum(int(number) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                      for number, unit in parts)
        try:
            return now - timedelta(seconds=seconds)
        except OverflowError as exc:
            raise error_type("UV relative cutoff exceeds the supported range") from exc
    raise error_type("unsupported UV cutoff representation")


def package_cutoffs(entries, *, balanced, error_type):
    result = []
    entries = entries.strip()
    while entries:
        entry = re.match(r'PackageName\(\s*"([a-z0-9._-]+)",?\s*\):\s*Enabled\(', entries)
        if entry is None:
            raise error_type("disabled or unsupported package-specific seasoning")
        value, stop = balanced(entries, entry.end() - 1, "(", ")")
        result.append((entry[1], value))
        entries = entries[stop:].lstrip(", \r\n\t")
    return result


def _toml_value(value, error_type):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item, error_type) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            json.dumps(key) + " = " + _toml_value(item, error_type) for key, item in value.items()
        ) + "}"
    raise error_type("dependency input TOML value cannot be retained")


def write_toml(path, data, *, error_type):
    text = "\n".join(
        json.dumps(key) + " = " + _toml_value(value, error_type) for key, value in data.items()
    )
    # The private snapshot can contain credential-bearing index settings.
    if path.exists():
        os.chmod(path, 0o600)
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(text + "\n")


def private_retained_root(parent, *, error_type):
    root = parent / "inputs"
    if os.name != "nt":
        root.mkdir(mode=0o700)
        return root
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
    convert.restype = wintypes.BOOL
    create = kernel.CreateDirectoryW
    create.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    create.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    descriptor = wintypes.LPVOID()
    # Protected, owner-only DACL propagates to both files and directories.
    if not convert("D:P(A;OICI;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise error_type("private dependency-input permissions could not be constructed")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(root), ctypes.byref(attributes)):
            raise error_type("private dependency-input directory could not be created")
    finally:
        kernel.LocalFree(descriptor)
    return root


def merge_config(high, low):
    result = dict(low)
    for key, value in high.items():
        previous = result.get(key)
        if isinstance(value, dict) and isinstance(previous, dict):
            result[key] = merge_config(value, previous)
        elif isinstance(value, list) and isinstance(previous, list):
            result[key] = [*value, *previous]
        else:
            result[key] = value
    return result


def resolver_configuration(
    projection, remapper=None, *, balanced, error_type, now=None, freeze_package_cutoffs=False,
):
    now = datetime.now(timezone.utc) if now is None else now
    match = re.search(r"\bsettings: PipSettings \{", projection)
    if match is None:
        raise error_type("UV resolver configuration cannot be compared")
    body, _ = balanced(projection, projection.index("{", match.start()), "{", "}")
    cutoff = re.search(r"(?m)^\s*exclude_newer: ExcludeNewer \{", body)
    if cutoff is None:
        raise error_type("UV resolver cutoff cannot be compared")
    cutoff_body, end = balanced(body, body.index("{", cutoff.start()), "{", "}")
    package = re.search(r"\bpackage:\s*ExcludeNewerPackage\(", cutoff_body)
    if package is None:
        raise error_type("UV package cutoffs cannot be compared")
    package_body, _ = balanced(cutoff_body, package.end() - 1, "(", ")")
    package_entries = re.fullmatch(r"\s*\{(.*)\},?\s*", package_body, re.DOTALL)
    if package_entries is None:
        raise error_type("UV package cutoff settings are unsupported")
    keys = []
    for name, value in package_cutoffs(package_entries[1], balanced=balanced, error_type=error_type):
        timestamp = cutoff_value(value, now, error_type=error_type)
        span = re.search(r"\bExcludeNewerSpan\(\s*([^,\s]+)", value)
        keys.append((name, timestamp.isoformat(), span[1] if span and not freeze_package_cutoffs else None))
    body = body[:cutoff.start()] + "package_cutoffs:" + repr(sorted(keys)) + "\n" + body[end:]
    if remapper is not None:
        for field in ("dependency_metadata", "extra_build_dependencies", "sources"):
            match = re.search(r"(?m)^\s*" + field + r":\s*[^\n({\[]*([({\[])", body)
            if match:
                start = match.start(1)
                opening = body[start]
                _, end = balanced(body, start, opening, {"(": ")", "{": "}", "[": "]"}[opening])
                body = body[:start] + remapper(body[start:end]) + body[end:]
    # Execution always reinstalls the full closure, which also subsumes an
    # admitted package-specific refresh. Neither changes dependency resolution.
    reinstall = re.search(r"(?m)^\s*reinstall: Packages\(", body)
    if reinstall:
        _, end = balanced(body, reinstall.end() - 1, "(", ")")
        if body[end:end + 1] == ",":
            end += 1
        body = body[:reinstall.start()] + body[end:]
    body = re.sub(r"(?m)^\s*reinstall: (?:None|All),", "", body)
    # Discovery provenance changes to explicit-file provenance; index identity,
    # order, authentication and every resolver option must remain identical.
    while origin := re.search(r"(?m)^\s*origin: (?:None,|Some\()", body):
        end = origin.end()
        if body[end - 1] == "(":
            _, end = balanced(body, end - 1, "(", ")")
            if body[end:end + 1] == ",":
                end += 1
        body = body[:origin.start()] + body[end:]
    for field in (
        "constraints_from_workspace", "overrides_from_workspace",
        "excludes_from_workspace", "build_constraints_from_workspace",
    ):
        match = re.search(r"\b" + field + r":\s*\[", projection)
        if match:
            value, _ = balanced(projection, projection.index("[", match.start()), "[", "]")
            if remapper is not None:
                value = remapper(value)
            body += field + ":[" + value + "]"
    global_settings = re.search(r"(?m)^GlobalSettings \{", projection)
    if global_settings:
        value, _ = balanced(projection, projection.index("{", global_settings.start()), "{", "}")
        body += value
    return "\n".join(line.strip() for line in body.splitlines() if line.strip())


_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?\s*==\s*([0-9][A-Za-z0-9.!+_-]*)\s*(?:;.*)?$")


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo else None


def _fetch(url):
    request = Request(url, headers={"Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.9"})
    with urlopen(request, timeout=30) as response:
        body = response.read(_MAX_METADATA_BYTES + 1)
        if len(body) > _MAX_METADATA_BYTES:
            raise ValueError("package metadata exceeds the supported size")
        return body, response.geturl()


def _feed_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("https", "http")
            or (parsed.hostname or "").lower().rstrip(".") in (
                "pypi.org", "www.pypi.org", "files.pythonhosted.org",
            )):
        raise ValueError("mirror artifact URL does not retain a nonpublic feed")
    return url


class _FeedRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return super().redirect_request(request, fp, code, msg, headers, _feed_url(newurl))


def _verified_artifact(url, digest, filename, destination):
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise ValueError("mirror artifact filename is unsafe")
    path = Path(destination) / "artifacts" / digest / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    hashed = hashlib.sha256()
    size = 0
    with build_opener(_FeedRedirects()).open(_feed_url(url), timeout=30) as response, path.open("wb") as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > _MAX_ARTIFACT_BYTES:
                raise ValueError("mirror artifact exceeds the supported size")
            hashed.update(block)
            output.write(block)
    if hashed.hexdigest() != digest:
        path.unlink()
        raise ValueError("mirror artifact hash mismatch")
    return path.as_uri()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            url = attrs["href"]
            self.files.append({
                "filename": unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
                "url": url,
                "hashes": {"sha256": parse_qs(urlsplit(url).fragment).get("sha256", [""])[0]},
                "upload-time": attrs.get("data-upload-time"),
                "requires-python": attrs.get("data-requires-python"),
                "yanked": "data-yanked" in attrs,
            })


def _files(body):
    if body.lstrip().startswith(b"{"):
        files = json.loads(body)["files"]
        if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
            raise ValueError("unsupported Simple metadata")
        return files
    parser = _Links()
    parser.feed(body.decode("utf-8"))
    return parser.files


def _yanked(file):
    return file.get("yanked") not in (None, False)


def pinned_requirements(requirements):
    return sorted({
        (re.sub(r"[-_.]+", "-", match[1]).lower(), match[2])
        for requirement in requirements
        if isinstance(requirement, str) and (match := _PIN.fullmatch(requirement))
    })


def _index_url(projection, balanced):
    indexes = list(re.finditer(r"\bindex_locations: IndexLocations \{", projection))
    if len(indexes) != 1:
        return None
    body, _ = balanced(projection, projection.index("{", indexes[0].start()), "{", "}")
    entries = list(re.finditer(r"\bIndex \{", body))
    # Do not change multi-index or explicitly named package routing.
    if len(entries) != 1 or re.search(r"\bno_index:\s*true\b", body):
        return None
    entry, _ = balanced(body, body.index("{", entries[0].start()), "{", "}")
    if re.search(r"\bexplicit:\s*true\b|\bauthenticate:\s*Always\b", entry):
        return None
    given = re.search(r'\bgiven:\s*Some\(\s*("(?:[^"\\]|\\.)*")\s*,?\s*\)', entry)
    if given is None:
        return None
    url = json.loads(given[1])
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or parsed.hostname in ("pypi.org", "www.pypi.org"):
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return url


def mirror_index_arguments(
    projection, requirements, cutoffs, destination, *, balanced, error_type, now, fetch=None,
    environment=None,
):
    """Correct recent mirror dates only for verified, canonically seasoned pins."""
    environment = {} if environment is None else environment
    if (re.search(r"\bconnectivity:\s*Offline\b|\bkeyring_provider:\s*Subprocess\b", projection)
            or environment.get("UV_OFFLINE", "").lower() in ("1", "true")
            or environment.get("UV_KEYRING_PROVIDER", "disabled").lower() != "disabled"
            or any(re.fullmatch(r"UV_INDEX(?:_[A-Z0-9_]+)?_(?:USERNAME|PASSWORD)", key)
                   for key in environment)):
        # UV owns credential acquisition; never replace its authenticated path
        # with an anonymous metadata or artifact request.
        return []
    fetch = fetch or _fetch
    pins = pinned_requirements(requirements)
    index = _index_url(projection, balanced) if pins else None
    if index is None:
        return []
    global_cutoff = _timestamp(cutoffs[cutoffs.index("--exclude-newer") + 1])
    package_cutoffs = {}
    for offset, argument in enumerate(cutoffs[:-1]):
        if argument == "--exclude-newer-package":
            name, value = cutoffs[offset + 1].split("=", 1)
            package_cutoffs[name] = _timestamp(value)
    pages = {}
    for name, version in pins:
        try:
            if name not in pages:
                body, base = fetch(urljoin(index.rstrip("/") + "/", name + "/"))
                pages[name] = (_files(body), base, False)
            files, base, changed = pages[name]
            cutoff = min(global_cutoff, package_cutoffs.get(name, global_cutoff))
            filename_prefix = r"[-_.]+".join(map(re.escape, name.split("-")))
            candidates = [
                file for file in files
                if re.match(r"^" + filename_prefix + "-" + re.escape(version)
                            + r"(?:-|\.tar\.gz$|\.zip$)", file.get("filename", ""))
                and (uploaded := _timestamp(file.get("upload-time"))) is not None
                and cutoff < uploaded <= now
            ]
            if not candidates:
                continue
            body, canonical_url = fetch(f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json")
            if urlsplit(canonical_url).hostname != "pypi.org":
                raise ValueError("canonical metadata redirected outside PyPI")
            canonical = {file["filename"]: file for file in json.loads(body)["urls"]}
            for file in candidates:
                original = canonical.get(file["filename"])
                if original is None or _yanked(original) or _yanked(file):
                    continue
                digest = original.get("digests", {}).get("sha256")
                uploaded = _timestamp(original.get("upload_time_iso_8601"))
                if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                        or digest != file.get("hashes", {}).get("sha256")
                        or uploaded is None or uploaded >= cutoff
                        or (original.get("requires_python") and file.get("requires-python")
                            and original["requires_python"] != file["requires-python"])):
                    continue
                file["upload-time"] = original["upload_time_iso_8601"]
                # UV does not verify Simple-link hashes by default. Verify the
                # feed download before exposing a private copy to the resolver.
                file["url"] = _verified_artifact(
                    urljoin(base, file["url"]), digest, file["filename"], destination,
                )
                file["requires-python"] = original.get("requires_python") or file.get("requires-python")
                changed = True
            pages[name] = (files, base, changed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise error_type(f"canonical mirror-age evidence unavailable for {name}: {type(exc).__name__}") from exc
    active = [(name, files, base) for name, (files, base, changed) in pages.items() if changed]
    if not active:
        return []
    destination = Path(destination)
    for name, files, base in active:
        links = []
        for file in files:
            url = urljoin(base, file["url"])
            digest = file.get("hashes", {}).get("sha256")
            if digest and re.fullmatch(r"[0-9a-f]{64}", digest):
                url = urlunsplit(urlsplit(url)._replace(fragment="sha256=" + digest))
            attrs = {"href": url}
            for source, target in (("upload-time", "data-upload-time"), ("requires-python", "data-requires-python")):
                if file.get(source) is not None:
                    attrs[target] = file[source]
            if _yanked(file):
                attrs["data-yanked"] = str(file["yanked"])
            attributes = " ".join(f'{key}="{html.escape(str(value), quote=True)}"' for key, value in attrs.items())
            links.append(f"<a {attributes}>{html.escape(file['filename'])}</a>")
        page = destination / name / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<!DOCTYPE html>\n" + "\n".join(links), encoding="utf-8")
    return ["--index", destination.as_uri()]
