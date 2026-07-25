#!/usr/bin/env python3
"""Create one repository-owned, single-HTML feature record."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from pathlib import Path
import re
import sys


FEATURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INDEX_MARKER = "<!-- feature-index:append-before -->"
TOKEN_NAMES = {
    "{{FEATURE_ID}}",
    "{{FEATURE_TITLE_HTML}}",
    "{{FEATURE_SUMMARY_HTML}}",
    "{{FEATURE_TITLE_JSON}}",
    "{{FEATURE_SUMMARY_JSON}}",
    "{{CREATED_DATE}}",
}


def _json_text(value: str) -> str:
    """Return JSON string contents safe inside an HTML raw-text script."""
    return (
        json.dumps(value, ensure_ascii=False)[1:-1]
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _has_symlink_component(root: Path, target: Path) -> bool:
    cursor = root
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def render_index_entry(
    feature_id: str,
    title: str,
    status: str,
    summary: str,
) -> str:
    """Render the canonical catalog card used by the scaffold and validator."""
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


def create_feature(root: Path, feature_id: str, title: str, summary: str) -> Path:
    root = root.resolve()
    feature_id = feature_id.strip()
    title = " ".join(title.split())
    summary = " ".join(summary.split())

    if not FEATURE_ID_RE.fullmatch(feature_id):
        raise ValueError(
            "feature id must use lowercase letters, digits, and single hyphens"
        )
    if not title:
        raise ValueError("title must not be empty")
    if not summary:
        raise ValueError("summary must not be empty")

    features_root = root / "docs" / "features"
    template_root = features_root / "_template"
    template_path = template_root / "index.html"
    destination = features_root / feature_id
    index_path = features_root / "index.html"

    if _has_symlink_component(root, features_root):
        raise ValueError(
            "docs/features and its repo-relative parents must not be symlinks"
        )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"feature record already exists: {destination}")
    if not template_root.is_dir() or not template_path.is_file():
        raise FileNotFoundError(f"feature template is missing: {template_path}")
    if (
        features_root.is_symlink()
        or template_root.is_symlink()
        or template_path.is_symlink()
    ):
        raise ValueError("feature root and template must not be symlinks")
    template_entries = {path.name for path in template_root.iterdir()}
    if template_entries != {"index.html"}:
        raise ValueError(
            "feature template must contain exactly one index.html file"
        )
    if not index_path.is_file():
        raise FileNotFoundError(f"feature index is missing: {index_path}")
    if index_path.is_symlink():
        raise ValueError("feature index must not be a symlink")

    index = index_path.read_text(encoding="utf-8")
    if index.count(INDEX_MARKER) != 1:
        raise ValueError("feature index must contain exactly one append marker")

    rendered = template_path.read_text(encoding="utf-8")
    replacements = {
        "{{FEATURE_ID}}": feature_id,
        "{{FEATURE_TITLE_HTML}}": html.escape(title),
        "{{FEATURE_SUMMARY_HTML}}": html.escape(summary),
        "{{FEATURE_TITLE_JSON}}": _json_text(title),
        "{{FEATURE_SUMMARY_JSON}}": _json_text(summary),
        "{{CREATED_DATE}}": dt.date.today().isoformat(),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    remaining = sorted(token for token in TOKEN_NAMES if token in rendered)
    if remaining:
        raise ValueError(
            "unreplaced template tokens: " + ", ".join(remaining)
        )

    entry = render_index_entry(feature_id, title, "planned", summary)
    updated_index = index.replace(INDEX_MARKER, entry + INDEX_MARKER)

    destination.mkdir()
    (destination / "assets").mkdir()
    (destination / "index.html").write_text(rendered, encoding="utf-8")
    index_path.write_text(updated_index, encoding="utf-8")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one single-HTML feature record and catalog entry."
    )
    parser.add_argument("feature_id", help="lowercase kebab-case feature id")
    parser.add_argument("--title", required=True, help="human-readable feature title")
    parser.add_argument(
        "--summary", required=True, help="one-sentence user-visible outcome"
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
    try:
        destination = create_feature(
            args.root, args.feature_id, args.title, args.summary
        )
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    relative = destination.relative_to(args.root.resolve())
    print(f"created {relative.as_posix()}/index.html and assets/")
    print("next: complete the page and embedded manifest, then run:")
    print("  python3 scripts/validate_feature_docs.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
