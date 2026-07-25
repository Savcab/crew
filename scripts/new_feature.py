#!/usr/bin/env python3
"""Create one repository-owned feature dossier from the canonical template."""

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
    "{{FEATURE_TITLE}}",
    "{{FEATURE_SUMMARY}}",
    "{{CREATED_DATE}}",
}


def _table_cell(value: str) -> str:
    value = html.escape(" ".join(value.split()))
    for character in ("\\", "|", "[", "]", "(", ")"):
        value = value.replace(character, "\\" + character)
    return value


def _markdown_text(value: str) -> str:
    value = html.escape(value)
    for character in ("\\", "[", "]"):
        value = value.replace(character, "\\" + character)
    return value


def _has_symlink_component(root: Path, target: Path) -> bool:
    cursor = root
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


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
    destination = features_root / feature_id
    index_path = features_root / "README.md"

    if _has_symlink_component(root, features_root):
        raise ValueError("docs/features and its repo-relative parents must not be symlinks")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"feature dossier already exists: {destination}")
    if not template_root.is_dir():
        raise FileNotFoundError(f"feature template is missing: {template_root}")
    if features_root.is_symlink() or template_root.is_symlink():
        raise ValueError("feature root and template must not be symlinks")
    if not index_path.is_file():
        raise FileNotFoundError(f"feature index is missing: {index_path}")
    if index_path.is_symlink():
        raise ValueError("feature index must not be a symlink")

    templates = sorted(path for path in template_root.iterdir() if path.is_file())
    expected = {"feature.json", "README.md", "spec.md", "evidence.md", "explainer.html"}
    names = {path.name for path in templates}
    if names != expected:
        raise ValueError(
            "feature template files do not match the required dossier: "
            f"expected {sorted(expected)}, found {sorted(names)}"
        )

    index = index_path.read_text(encoding="utf-8")
    if index.count(INDEX_MARKER) != 1:
        raise ValueError("feature index must contain exactly one append marker")

    replacements = {
        "{{FEATURE_ID}}": feature_id,
        "{{FEATURE_TITLE}}": title,
        "{{FEATURE_SUMMARY}}": summary,
        "{{CREATED_DATE}}": dt.date.today().isoformat(),
    }
    rendered: dict[str, str] = {}
    for template in templates:
        if template.is_symlink():
            raise ValueError(f"feature template must not be a symlink: {template.name}")
        content = template.read_text(encoding="utf-8")
        for token, raw_value in replacements.items():
            value = raw_value
            if template.suffix == ".json":
                value = json.dumps(raw_value, ensure_ascii=False)[1:-1]
            elif template.suffix == ".html":
                value = html.escape(raw_value)
            elif template.suffix == ".md":
                value = _markdown_text(raw_value)
            content = content.replace(token, value)
        remaining = sorted(token for token in TOKEN_NAMES if token in content)
        if remaining:
            raise ValueError(
                f"unreplaced template tokens in {template.name}: {', '.join(remaining)}"
            )
        rendered[template.name] = content

    row = (
        f"| [{_table_cell(title)}]({feature_id}/) | planned | "
        f"{_table_cell(summary)} |\n"
    )
    updated_index = index.replace(INDEX_MARKER, row + INDEX_MARKER)

    destination.mkdir()
    for name, content in rendered.items():
        (destination / name).write_text(content, encoding="utf-8")
    index_path.write_text(updated_index, encoding="utf-8")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a docs/features dossier and register it in the index."
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
    print(f"created {relative.as_posix()}")
    print("next: complete the spec and evidence, then run:")
    print("  python3 scripts/validate_feature_docs.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
