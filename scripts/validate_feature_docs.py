#!/usr/bin/env python3
"""Validate Crew's repository-owned feature dossier contract."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


FEATURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMIT_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
INDEX_MARKER = "<!-- feature-index:append-before -->"
REQUIRED_FILES = {
    "feature.json",
    "README.md",
    "spec.md",
    "evidence.md",
    "explainer.html",
}
ALLOWED_STATUSES = {"planned", "in-progress", "verified", "deprecated"}
ALLOWED_SURFACES = {"agent", "cli", "dashboard", "public-http", "storage"}
DOC_KEYS = {"overview", "spec", "evidence", "explainer"}
CANONICAL_DOCS = {
    "overview": "README.md",
    "spec": "spec.md",
    "evidence": "evidence.md",
    "explainer": "explainer.html",
}
TEST_KEYS = {"backend", "frontend", "integration", "live", "browser"}
TOP_LEVEL_KEYS = {
    "schema_version",
    "id",
    "title",
    "status",
    "summary",
    "created",
    "surfaces",
    "depends_on",
    "docs",
    "published_explainer",
    "code_paths",
    "test_paths",
    "delivery",
    "verification",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_EVIDENCE_BYTES = 25 * 1024 * 1024
SCAFFOLD_SENTINEL = "TODO(feature):"
README_HEADINGS = {
    "## What it does",
    "## User experience",
    "## Delivery slices",
    "## Read next",
}
SPEC_HEADINGS = {
    "## Goals",
    "## Architecture",
    "## Public interface",
    "## Security",
    "## Rollout and reversal",
}
EVIDENCE_HEADINGS = {
    "## Tested commit",
    "## Commands and results",
    "## Media evidence",
    "## Safety and redaction",
}
REQUIRED_CSP = {
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    "script-src 'unsafe-inline'",
    "connect-src 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "img-src 'self' data:",
    "media-src 'self' data:",
}


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
    )


def _confined_file(base: Path, value: str) -> Path | None:
    if not _safe_relative(value):
        return None
    candidate = base.joinpath(*PurePosixPath(value).parts)
    cursor = candidate
    while cursor != base:
        if cursor.is_symlink():
            return None
        cursor = cursor.parent
    try:
        resolved_base = base.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_base)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _has_symlink_component(root: Path, target: Path) -> bool:
    cursor = root
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _read_text(path: Path, label: str, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{label}: cannot read UTF-8 text: {exc}")
        return None


def _json_without_duplicates(text: str) -> Any:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=pairs_hook)


def _missing_headings(text: str, headings: set[str]) -> list[str]:
    lines = {line.strip() for line in text.splitlines()}
    return sorted(headings - lines)


def _has_duplicate_strings(values: list[Any]) -> bool:
    strings = [value for value in values if isinstance(value, str)]
    return len(strings) != len(set(strings))


def _table_cell(value: str) -> str:
    value = html.escape(" ".join(value.split()))
    for character in ("\\", "|", "[", "]", "(", ")"):
        value = value.replace(character, "\\" + character)
    return value


def _commit_exists(root: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _declared_content_entries(
    manifest: dict[str, Any]
) -> list[tuple[str, str]]:
    """Return category/path pairs covered by feature verification."""
    entries: list[tuple[str, str]] = []
    code_paths = manifest.get("code_paths")
    if isinstance(code_paths, list):
        entries.extend(
            ("code", item) for item in code_paths if isinstance(item, str)
        )
    test_paths = manifest.get("test_paths")
    if isinstance(test_paths, dict):
        for category, values in test_paths.items():
            if isinstance(values, list):
                entries.extend(
                    (f"test:{category}", item)
                    for item in values
                    if isinstance(item, str)
                )
    return sorted(entries)


def _digest_entries(entries: list[tuple[str, str, str, bytes]]) -> str:
    digest = hashlib.sha256()
    for category, path, mode, content in entries:
        for value in (
            category.encode("utf-8"),
            path.encode("utf-8"),
            mode.encode("ascii"),
            content,
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def revision_content_sha256(
    root: Path, manifest: dict[str, Any], revision: str
) -> str:
    """Hash declared blob contents, categories, paths, and modes at a revision."""
    declared = _declared_content_entries(manifest)
    if not declared:
        raise ValueError("no code or test paths are declared")
    entries: list[tuple[str, str, str, bytes]] = []
    for category, value in declared:
        tree = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-tree",
                "-z",
                revision,
                "--",
                f":(literal){value}",
            ],
            capture_output=True,
            check=False,
        )
        if tree.returncode != 0 or not tree.stdout:
            raise ValueError(f"revision {revision} does not contain {value}")
        records = [record for record in tree.stdout.split(b"\0") if record]
        if len(records) != 1 or b"\t" not in records[0]:
            raise ValueError(f"revision {revision} has an ambiguous path {value}")
        metadata, stored_path = records[0].split(b"\t", 1)
        try:
            mode, object_type, object_id = metadata.decode("ascii").split()
            decoded_path = stored_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"cannot read Git metadata for {value}") from exc
        if object_type != "blob" or decoded_path != value:
            raise ValueError(f"revision {revision} does not contain file {value}")
        blob = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_id],
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise ValueError(f"cannot read {value} from revision {revision}")
        entries.append((category, value, mode, blob.stdout))
    return _digest_entries(entries)


def _feature_anchor_commit(root: Path, feature_id: str) -> str:
    manifest_path = f"docs/features/{feature_id}/feature.json"
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--format=%H",
            "--diff-filter=A",
            "--",
            manifest_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    commits = [line for line in result.stdout.splitlines() if line]
    if result.returncode != 0 or len(commits) != 1:
        raise ValueError(
            "delivery 'self' must resolve to exactly one dossier-creating commit"
        )
    return commits[0]


def _single_parent(root: Path, commit: str) -> str:
    ancestry = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--parents", "-n", "1", commit],
        capture_output=True,
        text=True,
        check=False,
    )
    fields = ancestry.stdout.strip().split()
    if ancestry.returncode != 0 or len(fields) != 2:
        raise ValueError(f"revision {commit} must have exactly one parent")
    return fields[1]


def _feature_commit_changed_paths(
    root: Path, feature_id: str, commit: str
) -> set[str]:
    parent = _single_parent(root, commit)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "-z",
            parent,
            commit,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot inspect feature commit changes")
    changed = {
        value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    }
    dossier_prefix = f"docs/features/{feature_id}/"
    return {
        path
        for path in changed
        if not path.startswith(dossier_prefix)
        and path != "docs/features/README.md"
    }


def _git_path_is_dirty(root: Path, paths: list[str]) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *(f":(literal){path}" for path in paths),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot inspect candidate commit cleanliness")
    return bool(result.stdout)


def _validate_manifest(
    directory: Path, manifest: Any, errors: list[str]
) -> tuple[str | None, list[str], dict[str, str]]:
    label = directory.name
    root = directory.parents[2]
    if not isinstance(manifest, dict):
        errors.append(f"{label}/feature.json: root must be an object")
        return None, [], {}

    unknown = sorted(set(manifest) - TOP_LEVEL_KEYS)
    missing_fields = sorted(TOP_LEVEL_KEYS - set(manifest))
    if unknown:
        errors.append(
            f"{label}/feature.json: unknown fields: {', '.join(unknown)}"
        )
    if missing_fields:
        errors.append(
            f"{label}/feature.json: missing fields: {', '.join(missing_fields)}"
        )

    if manifest.get("schema_version") != 2:
        errors.append(f"{label}/feature.json: schema_version must be 2")

    feature_id = manifest.get("id")
    if feature_id != label:
        errors.append(
            f"{label}/feature.json: id must match directory name {label!r}"
        )
    if not isinstance(feature_id, str) or not FEATURE_ID_RE.fullmatch(feature_id):
        errors.append(f"{label}/feature.json: id must be lowercase kebab-case")

    for field in ("title", "summary", "created"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}/feature.json: {field} must be a non-empty string")

    status = manifest.get("status")
    if not isinstance(status, str) or status not in ALLOWED_STATUSES:
        errors.append(
            f"{label}/feature.json: status must be one of "
            + ", ".join(sorted(ALLOWED_STATUSES))
        )

    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, list) or any(
        not isinstance(item, str) or item not in ALLOWED_SURFACES
        for item in surfaces
    ):
        errors.append(
            f"{label}/feature.json: surfaces must use "
            + ", ".join(sorted(ALLOWED_SURFACES))
        )
        surfaces = []
    elif len(surfaces) != len(set(surfaces)):
        errors.append(f"{label}/feature.json: surfaces must not contain duplicates")

    depends_on = manifest.get("depends_on")
    if not isinstance(depends_on, list) or any(
        not isinstance(item, str) or not FEATURE_ID_RE.fullmatch(item)
        for item in depends_on
    ):
        errors.append(
            f"{label}/feature.json: depends_on must contain kebab-case feature ids"
        )
        depends_on = []
    elif len(depends_on) != len(set(depends_on)):
        errors.append(f"{label}/feature.json: depends_on must not contain duplicates")

    docs = manifest.get("docs")
    if not isinstance(docs, dict) or set(docs) != DOC_KEYS:
        errors.append(
            f"{label}/feature.json: docs must contain exactly "
            + ", ".join(sorted(DOC_KEYS))
        )
        docs = {}
    else:
        if docs != CANONICAL_DOCS:
            errors.append(
                f"{label}/feature.json: docs must use the canonical dossier filenames"
            )
        for key, value in docs.items():
            if not isinstance(value, str) or not _safe_relative(value):
                errors.append(
                    f"{label}/feature.json: docs.{key} must be a safe relative path"
                )
            elif _confined_file(directory, value) is None:
                errors.append(
                    f"{label}/feature.json: docs.{key} target is missing, "
                    f"outside the dossier, or a symlink: {value}"
                )

    published = manifest.get("published_explainer")
    if published is not None and (
        not isinstance(published, str) or not published.startswith("https://")
    ):
        errors.append(
            f"{label}/feature.json: published_explainer must be null or https://"
        )

    code_paths = manifest.get("code_paths")
    if not isinstance(code_paths, list):
        errors.append(f"{label}/feature.json: code_paths must be a list")
        code_paths = []
    for position, value in enumerate(code_paths):
        if not isinstance(value, str) or not _safe_relative(value):
            errors.append(
                f"{label}/feature.json: code_paths[{position}] must be repo-relative"
            )
        elif _confined_file(root, value) is None:
            errors.append(
                f"{label}/feature.json: code_paths[{position}] is missing, "
                f"outside the repo, or a symlink: {value}"
            )
    if _has_duplicate_strings(code_paths):
        errors.append(f"{label}/feature.json: code_paths must not contain duplicates")

    test_paths = manifest.get("test_paths")
    if not isinstance(test_paths, dict) or set(test_paths) != TEST_KEYS:
        errors.append(
            f"{label}/feature.json: test_paths must contain exactly "
            + ", ".join(sorted(TEST_KEYS))
        )
        test_paths = {key: [] for key in TEST_KEYS}
    else:
        for key, values in test_paths.items():
            if not isinstance(values, list):
                errors.append(
                    f"{label}/feature.json: test_paths.{key} must be a list"
                )
                continue
            for position, value in enumerate(values):
                if not isinstance(value, str) or not _safe_relative(value):
                    errors.append(
                        f"{label}/feature.json: test_paths.{key}[{position}] "
                        "must be repo-relative"
                    )
                elif _confined_file(root, value) is None:
                    errors.append(
                        f"{label}/feature.json: test_paths.{key}[{position}] "
                        f"is missing, outside the repo, or a symlink: {value}"
                    )
            if _has_duplicate_strings(values):
                errors.append(
                    f"{label}/feature.json: test_paths.{key} must not contain "
                    "duplicates"
                )

    declared_entries = _declared_content_entries(manifest)
    declared_paths = [path for _, path in declared_entries]
    if len(declared_paths) != len(set(declared_paths)):
        errors.append(
            f"{label}/feature.json: code_paths and test_paths must not overlap"
        )

    delivery = manifest.get("delivery")
    if not isinstance(delivery, list):
        errors.append(f"{label}/feature.json: delivery must be a list")
        delivery = []
    for position, item in enumerate(delivery):
        if not isinstance(item, dict):
            errors.append(
                f"{label}/feature.json: delivery[{position}] must be an object"
            )
            continue
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            errors.append(
                f"{label}/feature.json: delivery[{position}].name is required"
            )
        commit = item.get("commit")
        if not isinstance(commit, str) or (
            commit != "self" and not COMMIT_RE.fullmatch(commit)
        ):
            errors.append(
                f"{label}/feature.json: delivery[{position}].commit "
                "must be 'self' or a full commit SHA"
            )
        elif commit != "self" and not _commit_exists(root, commit):
            errors.append(
                f"{label}/feature.json: delivery[{position}].commit "
                "does not exist in this repository"
            )

    verification = manifest.get("verification")
    if not isinstance(verification, dict) or set(verification) != {
        "tested_revision",
        "content_sha256",
        "commands",
        "evidence",
    }:
        errors.append(
            f"{label}/feature.json: verification must contain tested_revision, "
            "content_sha256, commands, and evidence"
        )
        verification = {
            "tested_revision": "pending",
            "content_sha256": "pending",
            "commands": [],
            "evidence": [],
        }

    tested_revision = verification.get("tested_revision")
    if not isinstance(tested_revision, str):
        errors.append(
            f"{label}/feature.json: verification.tested_revision must be a string"
        )

    declared_content_digest = verification.get("content_sha256")
    if not isinstance(declared_content_digest, str):
        errors.append(
            f"{label}/feature.json: verification.content_sha256 must be a string"
        )

    commands = verification.get("commands")
    if not isinstance(commands, list):
        errors.append(
            f"{label}/feature.json: verification.commands must be a list"
        )
        commands = []
    for position, item in enumerate(commands):
        if not isinstance(item, dict) or set(item) != {"command", "result"}:
            errors.append(
                f"{label}/feature.json: verification.commands[{position}] must "
                "contain command and result"
            )
            continue
        for field in ("command", "result"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(
                    f"{label}/feature.json: verification.commands[{position}]."
                    f"{field} is required"
                )

    evidence = verification.get("evidence")
    if not isinstance(evidence, list):
        errors.append(
            f"{label}/feature.json: verification.evidence must be a list"
        )
        evidence = []
    evidence_ids: list[str] = []
    evidence_paths: list[str] = []
    evidence_assets: dict[str, str] = {}
    for position, item in enumerate(evidence):
        if not isinstance(item, dict):
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}] "
                "must be an object"
            )
            continue
        if set(item) != {"id", "kind", "path", "sha256", "description"}:
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}] must "
                "contain id, kind, path, sha256, and description"
            )
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not FEATURE_ID_RE.fullmatch(
            evidence_id
        ):
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}].id "
                "must be kebab-case"
            )
        else:
            evidence_ids.append(evidence_id)
        if not isinstance(item.get("kind"), str) or item.get("kind") not in {
            "image",
            "video",
        }:
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}].kind "
                "must be image or video"
            )
        path = item.get("path")
        if (
            not isinstance(path, str)
            or not _safe_relative(path)
            or not path.startswith("evidence/")
        ):
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}].path "
                "must be under evidence/"
            )
        else:
            evidence_paths.append(path)
            if isinstance(evidence_id, str) and FEATURE_ID_RE.fullmatch(evidence_id):
                evidence_assets[evidence_id] = path
            target = _confined_file(directory, path)
            if target is None:
                errors.append(
                    f"{label}/feature.json: verification.evidence[{position}] "
                    f"target is missing, outside the dossier, or a symlink: {path}"
                )
            else:
                size = target.stat().st_size
                if size == 0:
                    errors.append(
                        f"{label}/feature.json: verification.evidence[{position}] "
                        "must not be empty"
                    )
                if size > MAX_EVIDENCE_BYTES:
                    errors.append(
                        f"{label}/feature.json: verification.evidence[{position}] "
                        "exceeds the 25 MiB repository media limit"
                    )
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if item.get("sha256") != digest:
                    errors.append(
                        f"{label}/feature.json: verification.evidence[{position}] "
                        "sha256 does not match the file"
                    )
                suffix = target.suffix.lower()
                kind = item.get("kind")
                allowed = (
                    {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                    if kind == "image"
                    else {".mp4", ".webm"}
                )
                if suffix not in allowed:
                    errors.append(
                        f"{label}/feature.json: verification.evidence[{position}] "
                        f"extension {suffix or '<none>'} does not match {kind}"
                    )
                header = target.read_bytes()[:16]
                magic_ok = {
                    ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
                    ".jpg": header.startswith(b"\xff\xd8\xff"),
                    ".jpeg": header.startswith(b"\xff\xd8\xff"),
                    ".gif": header.startswith((b"GIF87a", b"GIF89a")),
                    ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
                    ".mp4": len(header) >= 8 and header[4:8] == b"ftyp",
                    ".webm": header.startswith(b"\x1aE\xdf\xa3"),
                }.get(suffix, False)
                if suffix in allowed and not magic_ok:
                    errors.append(
                        f"{label}/feature.json: verification.evidence[{position}] "
                        "file signature does not match its extension"
                    )
        if not isinstance(item.get("sha256"), str) or not SHA256_RE.fullmatch(
            item["sha256"]
        ):
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}].sha256 "
                "must be 64 lowercase hex characters"
            )
        if not isinstance(item.get("description"), str) or not item[
            "description"
        ].strip():
            errors.append(
                f"{label}/feature.json: verification.evidence[{position}]."
                "description is required"
            )

    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append(f"{label}/feature.json: evidence ids must be unique")
    if len(evidence_paths) != len(set(evidence_paths)):
        errors.append(f"{label}/feature.json: evidence paths must be unique")

    if status == "verified":
        if not surfaces:
            errors.append(
                f"{label}/feature.json: verified features need named surfaces"
            )
        if not code_paths:
            errors.append(
                f"{label}/feature.json: verified features need code_paths"
            )
        if not delivery:
            errors.append(
                f"{label}/feature.json: verified features need delivery commits"
            )
        elif len(delivery) != 1 or not isinstance(delivery[0], dict) or delivery[
            0
        ].get("commit") != "self":
            errors.append(
                f"{label}/feature.json: verified features need one 'self' "
                "delivery commit"
            )
        if not isinstance(tested_revision, str) or not COMMIT_RE.fullmatch(
            tested_revision
        ):
            errors.append(
                f"{label}/feature.json: verified features need a tested revision"
            )
        if not isinstance(declared_content_digest, str) or not SHA256_RE.fullmatch(
            declared_content_digest
        ):
            errors.append(
                f"{label}/feature.json: verified features need a content_sha256"
            )
        else:
            anchor_parent: str | None = None
            try:
                anchor_commit = _feature_anchor_commit(root, label)
                anchor_parent = _single_parent(root, anchor_commit)
                anchor_digest = revision_content_sha256(
                    root, manifest, anchor_commit
                )
                changed_paths = _feature_commit_changed_paths(
                    root, label, anchor_commit
                )
            except ValueError as exc:
                errors.append(f"{label}/feature.json: {exc}")
            else:
                if anchor_digest != declared_content_digest:
                    errors.append(
                        f"{label}/feature.json: verification.content_sha256 does "
                        "not match the feature commit"
                    )
                undeclared = sorted(changed_paths - set(declared_paths))
                if undeclared:
                    errors.append(
                        f"{label}/feature.json: feature commit has undeclared "
                        "changed paths: " + ", ".join(undeclared)
                    )
            if (
                isinstance(tested_revision, str)
                and COMMIT_RE.fullmatch(tested_revision)
                and _commit_exists(root, tested_revision)
            ):
                try:
                    tested_parent = _single_parent(root, tested_revision)
                    revision_digest = revision_content_sha256(
                        root, manifest, tested_revision
                    )
                except ValueError as exc:
                    errors.append(f"{label}/feature.json: {exc}")
                else:
                    if (
                        anchor_parent is not None
                        and tested_parent != anchor_parent
                    ):
                        errors.append(
                            f"{label}/feature.json: tested revision and feature "
                            "commit must have the same parent"
                        )
                    if revision_digest != declared_content_digest:
                        errors.append(
                            f"{label}/feature.json: tested revision content does "
                            "not match verification.content_sha256"
                        )
        if not commands:
            errors.append(
                f"{label}/feature.json: verified features need verification commands"
            )
        if not evidence:
            errors.append(
                f"{label}/feature.json: verified features need image or video evidence"
            )
        if not (test_paths.get("backend") or test_paths.get("frontend")):
            errors.append(
                f"{label}/feature.json: verified features need backend or "
                "frontend test paths"
            )
        if not (test_paths.get("integration") or test_paths.get("live")):
            errors.append(
                f"{label}/feature.json: verified features need integration or "
                "live test paths"
            )
        if "dashboard" in surfaces:
            if not test_paths.get("frontend"):
                errors.append(
                    f"{label}/feature.json: dashboard features need frontend tests"
                )
            if not test_paths.get("browser"):
                errors.append(
                    f"{label}/feature.json: dashboard features need browser test paths"
                )

    return status, depends_on, evidence_assets


def _validate_explainer(
    directory: Path,
    label: str,
    html: str,
    status: str | None,
    evidence: dict[str, str],
    errors: list[str],
) -> None:
    if not re.search(r"<!doctype\s+html", html, re.IGNORECASE):
        errors.append(f"{label}/explainer.html: missing HTML doctype")
    if not re.search(r"<title>.+?</title>", html, re.IGNORECASE | re.DOTALL):
        errors.append(f"{label}/explainer.html: missing non-empty title")
    if not re.search(
        rf"<html\b[^>]*\bdata-feature-id=['\"]{re.escape(label)}['\"]",
        html,
        re.IGNORECASE,
    ):
        errors.append(
            f"{label}/explainer.html: html must declare data-feature-id={label!r}"
        )

    csp_match = re.search(
        r"<meta\b(?=[^>]*http-equiv=['\"]Content-Security-Policy['\"])[^>]*"
        r"content=(['\"])(.*?)\1",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    directives = (
        {item.strip().lower() for item in csp_match.group(2).split(";")}
        if csp_match
        else set()
    )
    if not REQUIRED_CSP.issubset(directives):
        errors.append(
            f"{label}/explainer.html: missing restrictive self-contained CSP"
        )

    if not re.search(
        r"<(?:figure|section|div)\b[^>]*\bdata-feature-diagram\b",
        html,
        re.IGNORECASE,
    ):
        errors.append(
            f"{label}/explainer.html: missing data-feature-diagram marker"
        )
    if not re.search(
        r"<svg\b(?=[^>]*\brole=['\"]img['\"])(?=[^>]*"
        r"(?:aria-label|aria-labelledby)=)[^>]*>",
        html,
        re.IGNORECASE,
    ):
        errors.append(
            f"{label}/explainer.html: include an accessible inline SVG diagram"
        )

    asset_values: set[str] = set()
    for match in re.finditer(
        r"\b(?:src|poster)\s*=\s*(['\"])(.*?)\1", html, re.IGNORECASE | re.DOTALL
    ):
        value = match.group(2).strip()
        if value.startswith(("data:", "#")):
            continue
        if _safe_relative(value) and value.startswith("evidence/"):
            asset_values.add(value)
            if _confined_file(directory, value) is None:
                errors.append(
                    f"{label}/explainer.html: referenced evidence is missing "
                    f"or unsafe: {value}"
                )
        elif value:
            errors.append(
                f"{label}/explainer.html: external asset is not self-contained: {value}"
            )

    for evidence_id, evidence_path in evidence.items():
        if not re.search(
            rf"\bdata-feature-evidence=['\"]{re.escape(evidence_id)}['\"]",
            html,
            re.IGNORECASE,
        ):
            errors.append(
                f"{label}/explainer.html: missing evidence marker {evidence_id!r}"
            )
        if evidence_path not in asset_values:
            errors.append(
                f"{label}/explainer.html: evidence {evidence_id!r} must render "
                f"{evidence_path!r}"
            )

    if status == "verified" and not re.search(
        r"<(?:img|video)\b", html, re.IGNORECASE
    ):
        errors.append(
            f"{label}/explainer.html: verified features need image or video proof"
        )


def _validate_dependencies(
    dependencies: dict[str, list[str]], errors: list[str]
) -> None:
    known = set(dependencies)
    for feature_id, items in sorted(dependencies.items()):
        for dependency in items:
            if dependency == feature_id:
                errors.append(
                    f"{feature_id}/feature.json: feature cannot depend on itself"
                )
            elif dependency not in known:
                errors.append(
                    f"{feature_id}/feature.json: unknown dependency {dependency!r}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(feature_id: str, path: list[str]) -> None:
        if feature_id in visiting:
            cycle_start = path.index(feature_id)
            cycle = path[cycle_start:] + [feature_id]
            errors.append(
                "docs/features: dependency cycle: " + " -> ".join(cycle)
            )
            return
        if feature_id in visited:
            return
        visiting.add(feature_id)
        for dependency in dependencies.get(feature_id, []):
            if dependency in known:
                visit(dependency, path + [dependency])
        visiting.remove(feature_id)
        visited.add(feature_id)

    for feature_id in sorted(known):
        visit(feature_id, [feature_id])


def validate(root: Path) -> list[str]:
    root = root.resolve()
    features_root = root / "docs" / "features"
    errors: list[str] = []

    if _has_symlink_component(root, features_root):
        return ["docs/features: path and repo-relative parents must not be symlinks"]
    if not features_root.is_dir():
        return ["docs/features: directory is missing"]
    if features_root.is_symlink():
        return ["docs/features: directory must not be a symlink"]
    index_path = features_root / "README.md"
    template_root = features_root / "_template"
    if not index_path.is_file():
        errors.append("docs/features/README.md: feature index is missing")
        index = ""
    elif index_path.is_symlink():
        errors.append("docs/features/README.md: feature index must not be a symlink")
        index = ""
    else:
        index = _read_text(index_path, "docs/features/README.md", errors) or ""
        if index.count(INDEX_MARKER) != 1:
            errors.append(
                "docs/features/README.md: expected exactly one feature index marker"
            )
    if not template_root.is_dir():
        errors.append("docs/features/_template: template directory is missing")
    elif template_root.is_symlink():
        errors.append("docs/features/_template: template must not be a symlink")
    else:
        names = {path.name for path in template_root.iterdir() if path.is_file()}
        if names != REQUIRED_FILES:
            errors.append(
                "docs/features/_template: required files differ; expected "
                + ", ".join(sorted(REQUIRED_FILES))
            )
        for path in template_root.iterdir():
            if path.is_symlink():
                errors.append(
                    f"docs/features/_template/{path.name}: must not be a symlink"
                )

    directories = sorted(
        path
        for path in features_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    dependencies: dict[str, list[str]] = {}
    for directory in directories:
        label = directory.name
        if directory.is_symlink():
            errors.append(f"{label}: dossier directory must not be a symlink")
            continue
        if not FEATURE_ID_RE.fullmatch(label):
            errors.append(f"{label}: directory name must be lowercase kebab-case")

        names = {path.name for path in directory.iterdir() if path.is_file()}
        missing = sorted(REQUIRED_FILES - names)
        if missing:
            errors.append(f"{label}: missing required files: {', '.join(missing)}")
            continue
        symlinked = [
            name for name in sorted(REQUIRED_FILES) if (directory / name).is_symlink()
        ]
        for name in symlinked:
            errors.append(f"{label}/{name}: required file must not be a symlink")
        if symlinked:
            continue

        manifest_text = _read_text(
            directory / "feature.json", f"{label}/feature.json", errors
        )
        if manifest_text is None:
            continue
        try:
            manifest = _json_without_duplicates(manifest_text)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{label}/feature.json: invalid JSON: {exc}")
            continue

        status, depends_on, evidence_assets = _validate_manifest(
            directory, manifest, errors
        )
        dependencies[label] = depends_on
        title = manifest.get("title") if isinstance(manifest, dict) else None
        summary = manifest.get("summary") if isinstance(manifest, dict) else None
        if all(isinstance(value, str) for value in (title, status, summary)):
            expected_row = (
                f"| [{_table_cell(title)}]({label}/) | {_table_cell(status)} | "
                f"{_table_cell(summary)} |"
            )
            if expected_row not in index:
                errors.append(
                    f"docs/features/README.md: row for {label} must match its manifest"
                )

        overview = _read_text(
            directory / "README.md", f"{label}/README.md", errors
        ) or ""
        for heading in _missing_headings(overview, README_HEADINGS):
            errors.append(f"{label}/README.md: missing heading {heading}")

        spec = _read_text(directory / "spec.md", f"{label}/spec.md", errors) or ""
        for heading in _missing_headings(spec, SPEC_HEADINGS):
            errors.append(f"{label}/spec.md: missing heading {heading}")
        if "```mermaid" not in spec:
            errors.append(f"{label}/spec.md: include at least one Mermaid diagram")

        evidence = _read_text(
            directory / "evidence.md", f"{label}/evidence.md", errors
        ) or ""
        for heading in _missing_headings(evidence, EVIDENCE_HEADINGS):
            errors.append(f"{label}/evidence.md: missing heading {heading}")
        tested_revision = (
            manifest.get("verification", {}).get("tested_revision")
            if isinstance(manifest, dict)
            and isinstance(manifest.get("verification"), dict)
            else None
        )
        if status == "verified" and (
            not isinstance(tested_revision, str)
            or f"`{tested_revision}`" not in evidence
        ):
            errors.append(
                f"{label}/evidence.md: tested commit must match feature.json"
            )
        content_digest = (
            manifest.get("verification", {}).get("content_sha256")
            if isinstance(manifest, dict)
            and isinstance(manifest.get("verification"), dict)
            else None
        )
        if status == "verified" and (
            not isinstance(content_digest, str)
            or f"`{content_digest}`" not in evidence
        ):
            errors.append(
                f"{label}/evidence.md: content digest must match feature.json"
            )
        if "```" not in evidence:
            errors.append(f"{label}/evidence.md: include exact command/output evidence")

        html = _read_text(
            directory / "explainer.html", f"{label}/explainer.html", errors
        ) or ""
        if status == "verified":
            for path, text in (
                ("README.md", overview),
                ("spec.md", spec),
                ("evidence.md", evidence),
                ("explainer.html", html),
            ):
                if SCAFFOLD_SENTINEL in text:
                    errors.append(
                        f"{label}/{path}: verified dossier retains scaffold TODOs"
                    )
        _validate_explainer(
            directory, label, html, status, evidence_assets, errors
        )

    _validate_dependencies(dependencies, errors)
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate all repository-owned feature dossiers."
    )
    parser.add_argument(
        "--print-content-digest",
        metavar="FEATURE_ID",
        help="print the digest for one dossier's declared code and test paths",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_content_digest:
        feature_id = args.print_content_digest
        if not FEATURE_ID_RE.fullmatch(feature_id):
            print("error: feature id must be lowercase kebab-case", file=sys.stderr)
            return 1
        root = args.root.resolve()
        manifest_relative = f"docs/features/{feature_id}/feature.json"
        try:
            candidate = subprocess.run(
                ["git", "-C", str(root), "show", f"HEAD:{manifest_relative}"],
                capture_output=True,
                check=False,
            )
            if candidate.returncode != 0:
                raise ValueError(
                    "candidate HEAD must contain the feature manifest"
                )
            manifest = _json_without_duplicates(candidate.stdout.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("feature.json root must be an object")
            declared_paths = [
                path for _, path in _declared_content_entries(manifest)
            ]
            if _git_path_is_dirty(
                root, [manifest_relative, *declared_paths]
            ):
                raise ValueError(
                    "candidate manifest, code, and test paths must be clean at HEAD"
                )
            digest = revision_content_sha256(root, manifest, "HEAD")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(digest)
        return 0

    errors = validate(args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"feature docs invalid: {len(errors)} error(s)", file=sys.stderr)
        return 1

    features_root = args.root.resolve() / "docs" / "features"
    count = sum(
        1
        for path in features_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    print(f"feature docs valid: {count} dossier(s), template present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
