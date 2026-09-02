from __future__ import annotations

import os
import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class WorkerPathError(ValueError):
    def __init__(self, field: str, reason: str = "invalid") -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid {field} path: {reason}")


@dataclass(frozen=True)
class PathFingerprint:
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ValidatedWorkerPaths:
    python: Path
    script: Path
    hermes_root: Path
    pyvenv_cfg: Path
    fingerprints: tuple[tuple[Path, PathFingerprint], ...]


def _fingerprint(path: Path) -> PathFingerprint:
    return _fingerprint_info(path.lstat())


def _fingerprint_info(info: os.stat_result) -> PathFingerprint:
    directory = stat.S_ISDIR(info.st_mode)
    return PathFingerprint(
        device=info.st_dev,
        inode=info.st_ino,
        mode=info.st_mode,
        uid=info.st_uid,
        size=0 if directory else info.st_size,
        mtime_ns=0 if directory else info.st_mtime_ns,
    )


def _normalize_absolute(path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise WorkerPathError(field, "must be absolute")
    normalized = Path(os.path.abspath(path))
    if normalized != path:
        raise WorkerPathError(field, "must be normalized")
    return normalized


def _validate_node(
    path: Path,
    field: str,
    *,
    uid: int,
    directory: bool,
    executable: bool = False,
    allow_root_owner: bool = False,
) -> PathFingerprint:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkerPathError(field, "unavailable") from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    mode = stat.S_IMODE(info.st_mode)
    if (
        stat.S_ISLNK(info.st_mode)
        or not expected
        or info.st_uid not in ({uid, 0} if allow_root_owner else {uid})
        or mode & 0o022
        or (executable and not mode & 0o111)
    ):
        raise WorkerPathError(field)
    return _fingerprint(path)


def _sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _parse_pyvenv(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkerPathError("pyvenv", "unreadable") from exc
    if len(raw) > 16_384:
        raise WorkerPathError("pyvenv", "too large")
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise WorkerPathError("pyvenv", "malformed")
        key, value = (part.strip() for part in line.split("=", 1))
        folded_key = key.casefold()
        if not key or not value or folded_key in values:
            raise WorkerPathError("pyvenv", "malformed")
        values[folded_key] = value
    if "home" not in values or not ({"version", "version_info"} & values.keys()):
        raise WorkerPathError("pyvenv", "missing home/version")
    return values


def validate_worker_paths(
    python: Path | str,
    script: Path | str,
    hermes_root: Path | str,
    *,
    uid: int | None = None,
) -> ValidatedWorkerPaths:
    runtime_uid = os.getuid() if uid is None else uid
    lexical_python = _normalize_absolute(Path(python), "python")
    lexical_script = _normalize_absolute(Path(script), "script")
    lexical_root = _normalize_absolute(Path(hermes_root), "hermes_root")

    root_fp = _validate_node(lexical_root, "hermes_root", uid=runtime_uid, directory=True)
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise WorkerPathError("hermes_root", "unavailable") from exc
    if resolved_root != lexical_root:
        raise WorkerPathError("hermes_root", "must not traverse symlinks")

    venv = lexical_root / "venv"
    bin_dir = venv / "bin"
    pyvenv_cfg = venv / "pyvenv.cfg"
    venv_fp = _validate_node(venv, "pyvenv", uid=runtime_uid, directory=True)
    bin_fp = _validate_node(bin_dir, "python", uid=runtime_uid, directory=True)
    pyvenv_fp = _validate_node(pyvenv_cfg, "pyvenv", uid=runtime_uid, directory=False)
    if lexical_python.parent != bin_dir or lexical_python.name != "talktohermes-python":
        raise WorkerPathError("python", "must be a regular interpreter inside hermes_root/venv/bin")
    python_fp = _validate_node(
        lexical_python, "python", uid=runtime_uid, directory=False, executable=True
    )
    script_fp = _validate_node(
        lexical_script,
        "script",
        uid=runtime_uid,
        directory=False,
        allow_root_owner=True,
    )
    try:
        resolved_script = lexical_script.resolve(strict=True)
    except OSError as exc:
        raise WorkerPathError("script", "unavailable") from exc
    # The immutable release may be selected by a root-owned `current` symlink.
    # The final script itself must remain regular/non-symlink and its target is
    # fingerprinted, so an unprivileged runtime user cannot redirect it.

    base_link = bin_dir / "python"
    try:
        base_link_fp = _fingerprint(base_link)
        base_link_info = base_link.lstat()
        base_python = base_link.resolve(strict=True)
        base_info = base_python.lstat()
    except OSError as exc:
        raise WorkerPathError("python", "base interpreter unavailable") from exc
    base_mode = stat.S_IMODE(base_info.st_mode)
    if (
        not stat.S_ISLNK(base_link_info.st_mode)
        or not stat.S_ISREG(base_info.st_mode)
        or base_info.st_uid not in {runtime_uid, 0}
        or base_mode & 0o022
        or not base_mode & 0o111
        or base_python.is_relative_to(venv)
    ):
        raise WorkerPathError("python", "invalid base interpreter")
    match = re.fullmatch(r"python(\d+)\.(\d+)", base_python.name)
    if match is None:
        raise WorkerPathError("python", "base interpreter version is ambiguous")
    pyvenv = _parse_pyvenv(pyvenv_cfg)
    expected_version = f"{match.group(1)}.{match.group(2)}"
    try:
        configured_home = Path(pyvenv["home"]).resolve(strict=True)
    except OSError as exc:
        raise WorkerPathError("pyvenv", "home unavailable") from exc
    configured_versions = [
        pyvenv[key] for key in ("version", "version_info") if key in pyvenv
    ]
    if configured_home != base_python.parent or not all(
        value == expected_version or value.startswith(expected_version + ".")
        for value in configured_versions
    ):
        raise WorkerPathError("pyvenv", "does not match base interpreter")
    try:
        if _sha256(lexical_python) != _sha256(base_python):
            raise WorkerPathError("python", "copy does not match base interpreter")
    except OSError as exc:
        raise WorkerPathError("python", "interpreter unreadable") from exc

    root_parent_fp = _validate_node(
        lexical_root.parent, "hermes_root", uid=runtime_uid, directory=True
    )
    script_parent_fp = _validate_node(
        lexical_script.parent,
        "script",
        uid=runtime_uid,
        directory=True,
        allow_root_owner=True,
    )

    tracked = (
        (lexical_root, root_fp),
        (venv, venv_fp),
        (bin_dir, bin_fp),
        (pyvenv_cfg, pyvenv_fp),
        (lexical_python, python_fp),
        (lexical_script, script_fp),
        (base_link, base_link_fp),
        (base_python, _fingerprint(base_python)),
        (lexical_root.parent, root_parent_fp),
        (lexical_script.parent, script_parent_fp),
    )
    return ValidatedWorkerPaths(
        python=lexical_python,
        script=lexical_script,
        hermes_root=lexical_root,
        pyvenv_cfg=pyvenv_cfg,
        fingerprints=tracked,
    )


def assert_worker_paths_unchanged(paths: ValidatedWorkerPaths) -> None:
    for path, expected in paths.fingerprints:
        try:
            actual = _fingerprint(path)
        except OSError as exc:
            raise WorkerPathError("worker", "path changed") from exc
        if actual != expected:
            raise WorkerPathError("worker", "path changed")


def open_validated_worker_script(paths: ValidatedWorkerPaths) -> int:
    expected = dict(paths.fingerprints)[paths.script]
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(paths.script, flags)
        actual = _fingerprint_info(os.fstat(descriptor))
    except OSError as exc:
        raise WorkerPathError("script", "path changed") from exc
    if actual != expected:
        os.close(descriptor)
        raise WorkerPathError("script", "path changed")
    return descriptor


def open_validated_worker_interpreter(paths: ValidatedWorkerPaths) -> int:
    """Open the validated interpreter so exec cannot race a path replacement."""
    expected = dict(paths.fingerprints)[paths.python]
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(paths.python, flags)
        actual = _fingerprint_info(os.fstat(descriptor))
    except OSError as exc:
        raise WorkerPathError("python", "path changed") from exc
    if actual != expected:
        os.close(descriptor)
        raise WorkerPathError("python", "path changed")
    return descriptor
