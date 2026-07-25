#!/usr/bin/env python3
"""Validate Crew's single-HTML, repository-owned feature record contract."""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


FEATURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMIT_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INDEX_MARKER = "<!-- feature-index:append-before -->"
MANIFEST_ID = "feature-manifest"
MANIFEST_TYPE = "application/json"
SCAFFOLD_SENTINEL = "TODO(feature):"
MAX_EVIDENCE_BYTES = 25 * 1024 * 1024

ALLOWED_STATUSES = {"planned", "in-progress", "verified", "deprecated"}
ALLOWED_SURFACES = {"agent", "cli", "dashboard", "public-http", "storage"}
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
    "code_paths",
    "test_paths",
    "delivery",
    "verification",
}
REQUIRED_SECTIONS = {
    "overview",
    "user-flow",
    "architecture",
    "public-interface",
    "security",
    "rollout",
    "verification",
}
REQUIRED_CSP = {
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    "script-src 'none'",
    "img-src 'self' data:",
    "media-src 'self' data:",
    "font-src data:",
    "connect-src 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
}
FORBIDDEN_TAGS = {"base", "embed", "form", "iframe", "object"}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
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


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _has_duplicate_strings(values: list[Any]) -> bool:
    strings = [value for value in values if isinstance(value, str)]
    return len(strings) != len(set(strings))


def _visible_list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def render_index_entry(
    feature_id: str,
    title: str,
    status: str,
    summary: str,
) -> str:
    escaped_id = html.escape(feature_id, quote=True)
    escaped_title = html.escape(title)
    escaped_status = html.escape(status)
    escaped_summary = html.escape(summary)
    return (
        f'    <article class="feature-card" data-feature-entry="{escaped_id}" '
        f'data-feature-status="{escaped_status}">\n'
        f'      <span class="status">{escaped_status}</span>\n'
        f'      <h2><a href="{escaped_id}/index.html">{escaped_title}</a></h2>\n'
        f"      <p>{escaped_summary}</p>\n"
        "    </article>\n"
    )


class FeaturePageParser(HTMLParser):
    """Collect structural facts without letting manifest text spoof the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctype = False
        self.html_attrs: dict[str, str | None] = {}
        self.title_parts: list[str] = []
        self.csp_values: list[str] = []
        self.scripts: list[dict[str, Any]] = []
        self.sections: list[str] = []
        self.evidence_markers: list[str] = []
        self.resources: list[tuple[str, str, str]] = []
        self.forbidden: list[str] = []
        self.diagram_count = 0
        self.accessible_diagram_svg = 0
        self.visible_parts: list[str] = []
        self.status_parts: list[list[str]] = []
        self.tested_revision_parts: list[list[str]] = []
        self.content_digest_parts: list[list[str]] = []
        self.created_parts: list[list[str]] = []
        self.surfaces_parts: list[list[str]] = []
        self.dependencies_parts: list[list[str]] = []

        self._stack: list[str] = []
        self._current_script: dict[str, Any] | None = None
        self._style_depth = 0
        self._title_depth: int | None = None
        self._diagram_depths: list[int] = []
        self._capture_depths: dict[str, list[tuple[int, list[str]]]] = {
            "status": [],
            "tested": [],
            "digest": [],
            "created": [],
            "surfaces": [],
            "dependencies": [],
        }

    @staticmethod
    def _attrs(attrs) -> dict[str, str | None]:
        return {str(key).lower(): value for key, value in attrs}

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() == "doctype html":
            self.doctype = True

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        depth = len(self._stack) + 1

        if tag == "html" and not self.html_attrs:
            self.html_attrs = values
        if tag in FORBIDDEN_TAGS:
            self.forbidden.append(f"forbidden <{tag}> element")
        for key in values:
            if key.startswith("on"):
                self.forbidden.append(f"inline event handler {key!r}")
        if tag == "meta":
            http_equiv = (values.get("http-equiv") or "").lower()
            if http_equiv == "content-security-policy":
                self.csp_values.append(values.get("content") or "")
            if http_equiv == "refresh":
                self.forbidden.append("meta refresh")
        if tag == "script":
            script = {"attrs": values, "parts": []}
            self.scripts.append(script)
            self._current_script = script
        elif tag == "style":
            self._style_depth += 1
        elif tag == "title":
            self._title_depth = depth

        section = values.get("data-feature-section")
        if section is not None:
            self.sections.append(section)
        evidence = values.get("data-feature-evidence")
        if evidence is not None:
            self.evidence_markers.append(evidence)
        if "data-feature-diagram" in values:
            self.diagram_count += 1
            self._diagram_depths.append(depth)
        if tag == "svg" and self._diagram_depths:
            if (
                (values.get("role") or "").lower() == "img"
                and (values.get("aria-label") or values.get("aria-labelledby"))
            ):
                self.accessible_diagram_svg += 1

        for attribute in ("src", "poster"):
            value = values.get(attribute)
            if value:
                self.resources.append((tag, attribute, value.strip()))
        if tag == "link" and values.get("href"):
            self.resources.append((tag, "href", values["href"].strip()))
        if tag == "a" and values.get("href"):
            self.resources.append((tag, "href", values["href"].strip()))

        captures = {
            "data-feature-status": "status",
            "data-tested-revision": "tested",
            "data-content-sha256": "digest",
            "data-feature-created": "created",
            "data-feature-surfaces": "surfaces",
            "data-feature-dependencies": "dependencies",
        }
        for attribute, key in captures.items():
            if attribute in values:
                parts: list[str] = []
                self._capture_depths[key].append((depth, parts))
                capture_targets = {
                    "status": self.status_parts,
                    "tested": self.tested_revision_parts,
                    "digest": self.content_digest_parts,
                    "created": self.created_parts,
                    "surfaces": self.surfaces_parts,
                    "dependencies": self.dependencies_parts,
                }
                capture_targets[key].append(parts)

        if tag not in VOID_TAGS:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current_script is not None:
            self._current_script["parts"].append(data)
            return
        if self._style_depth:
            return
        if self._title_depth is not None:
            self.title_parts.append(data)
        self.visible_parts.append(data)
        for captures in self._capture_depths.values():
            for _, parts in captures:
                parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        depth = len(self._stack)
        if tag == "script":
            self._current_script = None
        elif tag == "style" and self._style_depth:
            self._style_depth -= 1
        if tag == "title" and self._title_depth == depth:
            self._title_depth = None
        if self._diagram_depths and self._diagram_depths[-1] == depth:
            self._diagram_depths.pop()
        for key, captures in self._capture_depths.items():
            self._capture_depths[key] = [
                (capture_depth, parts)
                for capture_depth, parts in captures
                if capture_depth != depth
            ]
        if self._stack:
            self._stack.pop()


class CatalogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): value for key, value in attrs}
        value = values.get("data-feature-entry")
        if value is not None:
            self.entries.append(value)


def _parse_html(
    text: str, label: str, errors: list[str]
) -> FeaturePageParser:
    parser = FeaturePageParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        errors.append(f"{label}: cannot parse HTML: {exc}")
        return parser

    if not parser.doctype:
        errors.append(f"{label}: missing HTML doctype")
    if not _normalize("".join(parser.title_parts)):
        errors.append(f"{label}: missing non-empty title")
    if parser.forbidden:
        for finding in sorted(set(parser.forbidden)):
            errors.append(f"{label}: {finding} is not allowed")
    return parser


def _parse_page(
    text: str, label: str, errors: list[str]
) -> tuple[FeaturePageParser, Any | None]:
    parser = _parse_html(text, label, errors)
    if len(parser.scripts) != 1:
        errors.append(f"{label}: expected exactly one embedded manifest script")
        return parser, None
    script = parser.scripts[0]
    attrs = script["attrs"]
    if attrs.get("id") != MANIFEST_ID:
        errors.append(f"{label}: embedded manifest script id must be {MANIFEST_ID!r}")
        return parser, None
    if (attrs.get("type") or "").lower() != MANIFEST_TYPE:
        errors.append(
            f"{label}: embedded manifest script type must be {MANIFEST_TYPE!r}"
        )
        return parser, None
    payload = "".join(script["parts"]).strip()
    if any(character in payload for character in "<>&"):
        errors.append(
            f"{label}: embedded manifest must escape literal '<', '>', and '&'"
        )
        return parser, None
    try:
        manifest = _json_without_duplicates(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{label}: invalid embedded manifest JSON: {exc}")
        return parser, None
    return parser, manifest


def _validate_csp(
    parser: FeaturePageParser, label: str, errors: list[str]
) -> None:
    if len(parser.csp_values) != 1:
        errors.append(f"{label}: expected exactly one CSP meta tag")
        return

    directives = [
        " ".join(item.lower().split())
        for item in parser.csp_values[0].split(";")
        if item.strip()
    ]
    names = [directive.split(None, 1)[0] for directive in directives]
    duplicate_names = sorted(
        name for name in set(names) if names.count(name) > 1
    )
    for name in duplicate_names:
        errors.append(f"{label}: duplicate CSP directive {name!r}")
    if (
        len(directives) != len(REQUIRED_CSP)
        or set(directives) != REQUIRED_CSP
    ):
        errors.append(f"{label}: CSP must exactly match the restrictive policy")


def _validate_static_page(
    text: str,
    label: str,
    base: Path,
    *,
    allow_manifest: bool,
    errors: list[str],
) -> None:
    parser = _parse_html(text, label, errors)
    _validate_csp(parser, label, errors)

    if allow_manifest:
        if len(parser.scripts) != 1:
            errors.append(f"{label}: expected exactly one manifest script")
        else:
            attrs = parser.scripts[0]["attrs"]
            if (
                set(attrs) != {"id", "type"}
                or attrs.get("id") != MANIFEST_ID
                or (attrs.get("type") or "").lower() != MANIFEST_TYPE
            ):
                errors.append(
                    f"{label}: only the embedded application/json manifest "
                    "script is allowed"
                )
    elif parser.scripts:
        errors.append(f"{label}: scripts are not allowed")

    for tag, attribute, value in parser.resources:
        if tag == "a" and attribute == "href":
            if value.startswith("#") or _confined_file(base, value) is not None:
                continue
        elif value.startswith("data:") and tag in {"img", "source", "video"}:
            continue
        errors.append(
            f"{label}: external resource is not allowed: "
            f"{tag}[{attribute}]={value!r}"
        )


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


def _added_commits(root: Path, path: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--format=%H",
            "--diff-filter=A",
            "--",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _feature_anchor_commit(root: Path, feature_id: str) -> str:
    legacy = _added_commits(
        root, f"docs/features/{feature_id}/feature.json"
    )
    if legacy:
        if len(legacy) != 1:
            raise ValueError(
                "delivery 'self' legacy anchor must resolve to one commit"
            )
        return legacy[0]
    current = _added_commits(root, f"docs/features/{feature_id}/index.html")
    if len(current) != 1:
        raise ValueError(
            "delivery 'self' must resolve to exactly one record-creating commit"
        )
    return current[0]


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
        and path not in {"docs/features/index.html", "docs/features/README.md"}
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


def _asset_inventory(
    directory: Path, label: str, errors: list[str]
) -> set[str]:
    assets = directory / "assets"
    if not assets.exists() and not assets.is_symlink():
        return set()
    if assets.is_symlink() or not assets.is_dir():
        errors.append(f"{label}/assets: must be a real directory, not a symlink")
        return set()
    found: set[str] = set()
    for path in assets.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            errors.append(f"{label}/{relative}: asset must not be a symlink")
        elif path.is_dir():
            continue
        elif path.is_file():
            found.add(relative)
        else:
            errors.append(f"{label}/{relative}: asset must be a regular file")
    return found


def _validate_manifest(
    root: Path,
    directory: Path,
    manifest: Any,
    parser: FeaturePageParser,
    page_text: str,
    errors: list[str],
) -> tuple[str | None, list[str], dict[str, str]]:
    label = directory.name
    page_label = f"{label}/index.html"
    if not isinstance(manifest, dict):
        errors.append(f"{page_label}: embedded manifest root must be an object")
        return None, [], {}

    unknown = sorted(set(manifest) - TOP_LEVEL_KEYS)
    missing = sorted(TOP_LEVEL_KEYS - set(manifest))
    if unknown:
        errors.append(f"{page_label}: unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{page_label}: missing fields: {', '.join(missing)}")
    if manifest.get("schema_version") != 3:
        errors.append(f"{page_label}: schema_version must be 3")

    feature_id = manifest.get("id")
    if feature_id != label:
        errors.append(f"{page_label}: id must match directory name {label!r}")
    if not isinstance(feature_id, str) or not FEATURE_ID_RE.fullmatch(feature_id):
        errors.append(f"{page_label}: id must be lowercase kebab-case")
    if parser.html_attrs.get("data-feature-id") != label:
        errors.append(f"{page_label}: page data-feature-id must match {label!r}")
    if parser.html_attrs.get("data-feature-schema") != "3":
        errors.append(f"{page_label}: page data-feature-schema must be '3'")

    for field in ("title", "summary", "created"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{page_label}: {field} must be a non-empty string")

    status = manifest.get("status")
    if not isinstance(status, str) or status not in ALLOWED_STATUSES:
        errors.append(
            f"{page_label}: status must be one of "
            + ", ".join(sorted(ALLOWED_STATUSES))
        )

    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, list) or any(
        not isinstance(item, str) or item not in ALLOWED_SURFACES
        for item in surfaces
    ):
        errors.append(
            f"{page_label}: surfaces must use "
            + ", ".join(sorted(ALLOWED_SURFACES))
        )
        surfaces = []
    elif len(surfaces) != len(set(surfaces)):
        errors.append(f"{page_label}: surfaces must not contain duplicates")

    depends_on = manifest.get("depends_on")
    if not isinstance(depends_on, list) or any(
        not isinstance(item, str) or not FEATURE_ID_RE.fullmatch(item)
        for item in depends_on
    ):
        errors.append(f"{page_label}: depends_on must contain kebab-case ids")
        depends_on = []
    elif len(depends_on) != len(set(depends_on)):
        errors.append(f"{page_label}: depends_on must not contain duplicates")

    code_paths = manifest.get("code_paths")
    if not isinstance(code_paths, list):
        errors.append(f"{page_label}: code_paths must be a list")
        code_paths = []
    for position, value in enumerate(code_paths):
        if not isinstance(value, str) or not _safe_relative(value):
            errors.append(
                f"{page_label}: code_paths[{position}] must be repo-relative"
            )
        elif _confined_file(root, value) is None:
            errors.append(
                f"{page_label}: code_paths[{position}] is missing or unsafe: {value}"
            )
    if _has_duplicate_strings(code_paths):
        errors.append(f"{page_label}: code_paths must not contain duplicates")

    test_paths = manifest.get("test_paths")
    if not isinstance(test_paths, dict) or set(test_paths) != TEST_KEYS:
        errors.append(
            f"{page_label}: test_paths must contain exactly "
            + ", ".join(sorted(TEST_KEYS))
        )
        test_paths = {key: [] for key in TEST_KEYS}
    else:
        for key, values in test_paths.items():
            if not isinstance(values, list):
                errors.append(f"{page_label}: test_paths.{key} must be a list")
                continue
            for position, value in enumerate(values):
                if not isinstance(value, str) or not _safe_relative(value):
                    errors.append(
                        f"{page_label}: test_paths.{key}[{position}] "
                        "must be repo-relative"
                    )
                elif _confined_file(root, value) is None:
                    errors.append(
                        f"{page_label}: test_paths.{key}[{position}] "
                        f"is missing or unsafe: {value}"
                    )
            if _has_duplicate_strings(values):
                errors.append(
                    f"{page_label}: test_paths.{key} must not contain duplicates"
                )

    declared_entries = _declared_content_entries(manifest)
    declared_paths = [path for _, path in declared_entries]
    if len(declared_paths) != len(set(declared_paths)):
        errors.append(f"{page_label}: code_paths and test_paths must not overlap")

    delivery = manifest.get("delivery")
    if not isinstance(delivery, list):
        errors.append(f"{page_label}: delivery must be a list")
        delivery = []
    for position, item in enumerate(delivery):
        if not isinstance(item, dict) or set(item) != {"name", "commit"}:
            errors.append(
                f"{page_label}: delivery[{position}] must contain name and commit"
            )
            continue
        if not isinstance(item["name"], str) or not item["name"].strip():
            errors.append(f"{page_label}: delivery[{position}].name is required")
        commit = item["commit"]
        if not isinstance(commit, str) or (
            commit != "self" and not COMMIT_RE.fullmatch(commit)
        ):
            errors.append(
                f"{page_label}: delivery[{position}].commit must be 'self' "
                "or a full commit SHA"
            )
        elif commit != "self" and not _commit_exists(root, commit):
            errors.append(
                f"{page_label}: delivery[{position}].commit does not exist"
            )

    verification = manifest.get("verification")
    expected_verification_keys = {
        "tested_revision",
        "content_sha256",
        "commands",
        "evidence",
    }
    if not isinstance(verification, dict) or set(verification) != expected_verification_keys:
        errors.append(
            f"{page_label}: verification must contain tested_revision, "
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
        errors.append(f"{page_label}: tested_revision must be a string")
    content_digest = verification.get("content_sha256")
    if not isinstance(content_digest, str):
        errors.append(f"{page_label}: content_sha256 must be a string")

    commands = verification.get("commands")
    if not isinstance(commands, list):
        errors.append(f"{page_label}: commands must be a list")
        commands = []
    for position, item in enumerate(commands):
        if not isinstance(item, dict) or set(item) != {"command", "result"}:
            errors.append(
                f"{page_label}: commands[{position}] must contain command and result"
            )
            continue
        for field in ("command", "result"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(
                    f"{page_label}: commands[{position}].{field} is required"
                )

    evidence = verification.get("evidence")
    if not isinstance(evidence, list):
        errors.append(f"{page_label}: evidence must be a list")
        evidence = []
    evidence_ids: list[str] = []
    evidence_paths: list[str] = []
    evidence_assets: dict[str, str] = {}
    for position, item in enumerate(evidence):
        if not isinstance(item, dict) or set(item) != {
            "id",
            "kind",
            "path",
            "sha256",
            "description",
        }:
            errors.append(
                f"{page_label}: evidence[{position}] must contain id, kind, "
                "path, sha256, and description"
            )
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not FEATURE_ID_RE.fullmatch(
            evidence_id
        ):
            errors.append(
                f"{page_label}: evidence[{position}].id must be kebab-case"
            )
        else:
            evidence_ids.append(evidence_id)
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in {"image", "video"}:
            errors.append(
                f"{page_label}: evidence[{position}].kind must be image or video"
            )
        path = item.get("path")
        if (
            not isinstance(path, str)
            or not _safe_relative(path)
            or not path.startswith("assets/")
        ):
            errors.append(
                f"{page_label}: evidence[{position}].path must be under assets/"
            )
        else:
            evidence_paths.append(path)
            if isinstance(evidence_id, str) and FEATURE_ID_RE.fullmatch(evidence_id):
                evidence_assets[evidence_id] = path
            target = _confined_file(directory, path)
            if target is None:
                errors.append(
                    f"{page_label}: evidence[{position}] target is missing or unsafe: "
                    f"{path}"
                )
            else:
                size = target.stat().st_size
                if size == 0:
                    errors.append(f"{page_label}: evidence[{position}] is empty")
                if size > MAX_EVIDENCE_BYTES:
                    errors.append(
                        f"{page_label}: evidence[{position}] exceeds 25 MiB"
                    )
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if item.get("sha256") != digest:
                    errors.append(
                        f"{page_label}: evidence[{position}] sha256 does not match"
                    )
                suffix = target.suffix.lower()
                allowed = (
                    {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                    if kind == "image"
                    else {".mp4", ".webm"}
                )
                if suffix not in allowed:
                    errors.append(
                        f"{page_label}: evidence[{position}] extension does not "
                        f"match {kind}"
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
                        f"{page_label}: evidence[{position}] file signature "
                        "does not match its extension"
                    )
        sha = item.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            errors.append(
                f"{page_label}: evidence[{position}].sha256 must be lowercase hex"
            )
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(
                f"{page_label}: evidence[{position}].description is required"
            )

    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append(f"{page_label}: evidence ids must be unique")
    if len(evidence_paths) != len(set(evidence_paths)):
        errors.append(f"{page_label}: evidence paths must be unique")

    assets = _asset_inventory(directory, label, errors)
    declared_assets = set(evidence_paths)
    for orphan in sorted(assets - declared_assets):
        errors.append(f"{label}/{orphan}: unreferenced asset")
    if declared_assets and not (directory / "assets").is_dir():
        errors.append(f"{label}/assets: directory is required for evidence")

    _validate_csp(parser, page_label, errors)

    section_counts = {name: parser.sections.count(name) for name in REQUIRED_SECTIONS}
    missing_sections = sorted(name for name, count in section_counts.items() if count == 0)
    duplicate_sections = sorted(name for name, count in section_counts.items() if count > 1)
    if missing_sections:
        errors.append(
            f"{page_label}: missing feature sections: {', '.join(missing_sections)}"
        )
    if duplicate_sections:
        errors.append(
            f"{page_label}: duplicate feature sections: {', '.join(duplicate_sections)}"
        )
    if parser.diagram_count != 1 or parser.accessible_diagram_svg != 1:
        errors.append(
            f"{page_label}: require one accessible inline SVG feature diagram"
        )

    asset_values: list[str] = []
    for tag, attribute, value in parser.resources:
        if value.startswith("#"):
            continue
        if value.startswith("data:") and tag in {"img", "source", "video"}:
            continue
        if _safe_relative(value) and value.startswith("assets/"):
            asset_values.append(value)
            if _confined_file(directory, value) is None:
                errors.append(
                    f"{page_label}: referenced asset is missing or unsafe: {value}"
                )
        else:
            errors.append(
                f"{page_label}: external resource is not allowed: "
                f"{tag}[{attribute}]={value!r}"
            )

    for evidence_id, evidence_path in evidence_assets.items():
        if parser.evidence_markers.count(evidence_id) != 1:
            errors.append(
                f"{page_label}: missing evidence marker {evidence_id!r}"
            )
        if evidence_path not in asset_values:
            errors.append(
                f"{page_label}: evidence {evidence_id!r} must render "
                f"{evidence_path!r}"
            )

    visible = _normalize(" ".join(parser.visible_parts))
    title = manifest.get("title")
    summary = manifest.get("summary")
    if isinstance(title, str) and _normalize(title) not in visible:
        errors.append(f"{page_label}: title must be visible outside the manifest")
    if isinstance(summary, str) and _normalize(summary) not in visible:
        errors.append(f"{page_label}: summary must be visible outside the manifest")

    def one_capture(parts: list[list[str]], field: str, expected: str) -> None:
        if len(parts) != 1 or _normalize(" ".join(parts[0])) != _normalize(expected):
            errors.append(
                f"{page_label}: visible {field} must match embedded manifest"
            )

    if isinstance(status, str):
        one_capture(parser.status_parts, "status", status)
    created = manifest.get("created")
    if isinstance(created, str):
        one_capture(parser.created_parts, "created date", created)
    if isinstance(surfaces, list):
        one_capture(parser.surfaces_parts, "surfaces", _visible_list(surfaces))
    if isinstance(depends_on, list):
        one_capture(
            parser.dependencies_parts,
            "dependencies",
            _visible_list(depends_on),
        )
    if isinstance(tested_revision, str):
        one_capture(
            parser.tested_revision_parts,
            "tested revision",
            tested_revision,
        )
    if isinstance(content_digest, str):
        one_capture(
            parser.content_digest_parts,
            "content digest",
            content_digest,
        )

    for position, item in enumerate(commands):
        if not isinstance(item, dict):
            continue
        for field in ("command", "result"):
            value = item.get(field)
            if isinstance(value, str) and _normalize(value) not in visible:
                errors.append(
                    f"{page_label}: commands[{position}].{field} must be visible"
                )

    if status == "verified":
        if SCAFFOLD_SENTINEL in page_text:
            errors.append(f"{page_label}: verified page retains scaffold TODOs")
        if not surfaces:
            errors.append(f"{page_label}: verified feature needs named surfaces")
        if not code_paths:
            errors.append(f"{page_label}: verified feature needs code_paths")
        if (
            len(delivery) != 1
            or not isinstance(delivery[0], dict)
            or delivery[0].get("commit") != "self"
        ):
            errors.append(
                f"{page_label}: verified feature needs one 'self' delivery commit"
            )
        if not isinstance(tested_revision, str) or not COMMIT_RE.fullmatch(
            tested_revision
        ):
            errors.append(f"{page_label}: verified feature needs tested revision")
        if not isinstance(content_digest, str) or not SHA256_RE.fullmatch(
            content_digest
        ):
            errors.append(f"{page_label}: verified feature needs content_sha256")
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
                errors.append(f"{page_label}: {exc}")
            else:
                if anchor_digest != content_digest:
                    errors.append(
                        f"{page_label}: content_sha256 does not match "
                        "the feature commit"
                    )
                undeclared = sorted(changed_paths - set(declared_paths))
                if undeclared:
                    errors.append(
                        f"{page_label}: feature commit has undeclared changed paths: "
                        + ", ".join(undeclared)
                    )
            if (
                isinstance(tested_revision, str)
                and COMMIT_RE.fullmatch(tested_revision)
                and _commit_exists(root, tested_revision)
            ):
                try:
                    tested_parent = _single_parent(root, tested_revision)
                    tested_digest = revision_content_sha256(
                        root, manifest, tested_revision
                    )
                except ValueError as exc:
                    errors.append(f"{page_label}: {exc}")
                else:
                    if (
                        anchor_parent is not None
                        and tested_parent != anchor_parent
                    ):
                        errors.append(
                            f"{page_label}: tested revision and feature commit "
                            "must have the same parent"
                        )
                    if tested_digest != content_digest:
                        errors.append(
                            f"{page_label}: tested revision content does not "
                            "match content_sha256"
                        )
        if not commands:
            errors.append(f"{page_label}: verified feature needs commands")
        if not evidence:
            errors.append(f"{page_label}: verified feature needs media evidence")
        if not (test_paths.get("backend") or test_paths.get("frontend")):
            errors.append(
                f"{page_label}: verified feature needs backend or frontend tests"
            )
        if not (test_paths.get("integration") or test_paths.get("live")):
            errors.append(
                f"{page_label}: verified feature needs integration or live tests"
            )
        if "dashboard" in surfaces:
            if not test_paths.get("frontend"):
                errors.append(
                    f"{page_label}: dashboard feature needs frontend tests"
                )
            if not test_paths.get("browser"):
                errors.append(
                    f"{page_label}: dashboard feature needs browser tests"
                )

    return status, depends_on, evidence_assets


def _validate_dependencies(
    dependencies: dict[str, list[str]], errors: list[str]
) -> None:
    known = set(dependencies)
    for feature_id, items in sorted(dependencies.items()):
        for dependency in items:
            if dependency == feature_id:
                errors.append(
                    f"{feature_id}/index.html: feature cannot depend on itself"
                )
            elif dependency not in known:
                errors.append(
                    f"{feature_id}/index.html: unknown dependency {dependency!r}"
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

    root_files = {
        path.name
        for path in features_root.iterdir()
        if path.is_file() or path.is_symlink()
    }
    if root_files != {"index.html"}:
        errors.append(
            "docs/features: root must contain only index.html and directories"
        )
    index_path = features_root / "index.html"
    if not index_path.is_file() or index_path.is_symlink():
        errors.append("docs/features/index.html: catalog is missing or a symlink")
        index = ""
    else:
        index = _read_text(index_path, "docs/features/index.html", errors) or ""
        if index.count(INDEX_MARKER) != 1:
            errors.append(
                "docs/features/index.html: expected exactly one append marker"
            )
        _validate_static_page(
            index,
            "docs/features/index.html",
            features_root,
            allow_manifest=False,
            errors=errors,
        )

    template_root = features_root / "_template"
    if not template_root.is_dir() or template_root.is_symlink():
        errors.append("docs/features/_template: missing or symlinked")
    else:
        entries = {path.name for path in template_root.iterdir()}
        if entries != {"index.html"}:
            errors.append(
                "docs/features/_template: must contain exactly index.html"
            )
        template_path = template_root / "index.html"
        if template_path.is_symlink():
            errors.append("docs/features/_template/index.html: must not be a symlink")
        elif template_path.is_file():
            template = _read_text(
                template_path, "docs/features/_template/index.html", errors
            )
            if template is not None:
                _validate_static_page(
                    template,
                    "docs/features/_template/index.html",
                    template_root,
                    allow_manifest=True,
                    errors=errors,
                )

    directories = sorted(
        path
        for path in features_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    dependencies: dict[str, list[str]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    for directory in directories:
        label = directory.name
        if directory.is_symlink():
            errors.append(f"{label}: feature directory must not be a symlink")
            continue
        if not FEATURE_ID_RE.fullmatch(label):
            errors.append(f"{label}: directory name must be lowercase kebab-case")

        entries = {path.name for path in directory.iterdir()}
        if not entries.issubset({"index.html", "assets"}) or "index.html" not in entries:
            errors.append(
                f"{label}: feature directory may contain only index.html and assets/"
            )
        page_path = directory / "index.html"
        if not page_path.is_file() or page_path.is_symlink():
            errors.append(f"{label}/index.html: missing or symlinked")
            continue
        page_text = _read_text(page_path, f"{label}/index.html", errors)
        if page_text is None:
            continue
        parser, manifest = _parse_page(
            page_text, f"{label}/index.html", errors
        )
        if manifest is None:
            continue
        status, depends_on, _ = _validate_manifest(
            root, directory, manifest, parser, page_text, errors
        )
        dependencies[label] = depends_on
        if isinstance(manifest, dict):
            manifests[label] = manifest
        title = manifest.get("title") if isinstance(manifest, dict) else None
        summary = manifest.get("summary") if isinstance(manifest, dict) else None
        if all(isinstance(value, str) for value in (title, status, summary)):
            expected = render_index_entry(label, title, status, summary)
            if expected not in index:
                errors.append(
                    f"{label}/index.html: index entry must match embedded manifest"
                )

    catalog = CatalogParser()
    try:
        catalog.feed(index)
        catalog.close()
    except Exception as exc:
        errors.append(f"docs/features/index.html: cannot parse HTML: {exc}")
    if len(catalog.entries) != len(set(catalog.entries)):
        errors.append("docs/features/index.html: duplicate feature entries")
    if set(catalog.entries) != set(manifests):
        errors.append(
            "docs/features/index.html: entries must exactly match feature directories"
        )

    _validate_dependencies(dependencies, errors)
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate all repository-owned single-HTML feature records."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--print-content-digest",
        metavar="FEATURE_ID",
        help="print the clean HEAD digest for one embedded feature manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    if args.print_content_digest:
        feature_id = args.print_content_digest
        if not FEATURE_ID_RE.fullmatch(feature_id):
            print("error: feature id must be lowercase kebab-case", file=sys.stderr)
            return 1
        page_relative = f"docs/features/{feature_id}/index.html"
        page_path = root / page_relative
        errors: list[str] = []
        text = _read_text(page_path, page_relative, errors)
        if text is None:
            print("error: " + "; ".join(errors), file=sys.stderr)
            return 1
        _, manifest = _parse_page(text, page_relative, errors)
        if errors or not isinstance(manifest, dict):
            print("error: " + "; ".join(errors), file=sys.stderr)
            return 1
        declared = [path for _, path in _declared_content_entries(manifest)]
        if not declared:
            print("error: no code or test paths are declared", file=sys.stderr)
            return 1
        try:
            if _git_path_is_dirty(root, [page_relative, *declared]):
                raise ValueError(
                    "feature page or declared content is dirty; commit the "
                    "candidate before computing its digest"
                )
            digest = revision_content_sha256(root, manifest, "HEAD")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(digest)
        return 0

    errors = validate(root)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    directories = [
        path
        for path in (root / "docs" / "features").iterdir()
        if path.is_dir() and not path.name.startswith("_")
    ]
    print(
        f"feature HTML valid: {len(directories)} record(s), template present"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
