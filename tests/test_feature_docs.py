"""Contracts for single-HTML feature records and their repository assets."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RE = re.compile(
    r'(<script\b(?=[^>]*\bid=["\']feature-manifest["\'])'
    r'(?=[^>]*\btype=["\']application/json["\'])[^>]*>)(.*?)(</script>)',
    re.IGNORECASE | re.DOTALL,
)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
    "ASsJTYQAAAAASUVORK5CYII="
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


new_feature = _load("new_feature", ROOT / "scripts" / "new_feature.py")
validate_feature_docs = _load(
    "validate_feature_docs", ROOT / "scripts" / "validate_feature_docs.py"
)


def _manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    matches = list(MANIFEST_RE.finditer(text))
    if len(matches) != 1:
        raise AssertionError("expected one embedded feature manifest")
    return json.loads(matches[0].group(2))


def _json_for_html(value: dict) -> str:
    return (
        json.dumps(value, indent=2, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _write_manifest(path: Path, manifest: dict) -> None:
    text = path.read_text(encoding="utf-8")
    matches = list(MANIFEST_RE.finditer(text))
    if len(matches) != 1:
        raise AssertionError("expected one embedded feature manifest")
    match = matches[0]
    rendered = (
        text[: match.start(2)]
        + "\n"
        + _json_for_html(manifest)
        + "\n  "
        + text[match.end(2) :]
    )
    path.write_text(rendered, encoding="utf-8")


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise AssertionError(f"expected one occurrence of {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


class FeatureHtmlRepositoryTests(unittest.TestCase):
    def test_repository_feature_records_are_valid(self):
        self.assertEqual(validate_feature_docs.validate(ROOT), [])

    def test_feature_directories_contain_only_html_and_assets(self):
        features = ROOT / "docs" / "features"
        for directory in features.iterdir():
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            self.assertIn(
                {path.name for path in directory.iterdir()},
                ({"index.html"}, {"index.html", "assets"}),
                directory.name,
            )

    def test_feature_tree_contains_no_markdown_or_sidecar_json(self):
        features = ROOT / "docs" / "features"
        forbidden = [
            path
            for path in features.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".json"}
        ]
        self.assertEqual(forbidden, [])


class FeatureHtmlScaffoldTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="crew-feature-html-")
        self.root = Path(self._temp.name)
        features = self.root / "docs" / "features"
        features.mkdir(parents=True)
        shutil.copy2(
            ROOT / "docs" / "features" / "index.html",
            features / "index.html",
        )
        catalog = (features / "index.html").read_text(encoding="utf-8")
        catalog = re.sub(
            r'\s*<article class="feature-card".*?</article>\n',
            "\n",
            catalog,
            flags=re.DOTALL,
        )
        (features / "index.html").write_text(catalog, encoding="utf-8")
        shutil.copytree(
            ROOT / "docs" / "features" / "_template",
            features / "_template",
        )
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Feature HTML Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "config",
                "user.email",
                "feature-html@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", "docs/features"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "commit",
                "-qm",
                "Add single HTML feature framework",
            ],
            check=True,
        )

    def tearDown(self):
        self._temp.cleanup()

    def _create(
        self,
        feature_id: str = "sample-feature",
        title: str = "Sample feature",
        summary: str = "Give operators a concrete result they can verify.",
    ) -> Path:
        return new_feature.create_feature(
            self.root,
            feature_id,
            title,
            summary,
        )

    def _make_verified(self) -> tuple[Path, str, str]:
        destination = self._create()
        for relative in (
            "crew/sample.py",
            "tests/test_sample.py",
            "tests/sample_live.py",
            "tests/browser/sample.txt",
            "frontend/tests/sample.test.js",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")

        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest.update(
            {
                "surfaces": ["dashboard"],
                "code_paths": ["crew/sample.py"],
                "test_paths": {
                    "backend": ["tests/test_sample.py"],
                    "frontend": ["frontend/tests/sample.test.js"],
                    "integration": ["tests/sample_live.py"],
                    "live": [],
                    "browser": ["tests/browser/sample.txt"],
                },
                "delivery": [{"name": "Implement sample", "commit": "self"}],
            }
        )
        _write_manifest(html_path, manifest)

        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "commit",
                "-qm",
                "Add sample implementation",
            ],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        digest = validate_feature_docs.revision_content_sha256(
            self.root, manifest, commit
        )

        asset_path = destination / "assets" / "result.png"
        asset_path.write_bytes(PNG)
        manifest["status"] = "verified"
        manifest["verification"] = {
            "tested_revision": commit,
            "content_sha256": digest,
            "commands": [
                {"command": "python3 -m unittest", "result": "1 test passed"}
            ],
            "evidence": [
                {
                    "id": "sample-proof",
                    "kind": "image",
                    "path": "assets/result.png",
                    "sha256": hashlib.sha256(PNG).hexdigest(),
                    "description": "Actual fixture result.",
                }
            ],
        }
        _write_manifest(html_path, manifest)
        _replace_once(
            html_path,
            '<span data-feature-status>planned</span>',
            '<span data-feature-status>verified</span>',
        )
        _replace_once(
            html_path,
            '<span class="value" data-feature-surfaces>none</span>',
            '<span class="value" data-feature-surfaces>dashboard</span>',
        )
        html_path.write_text(
            html_path.read_text(encoding="utf-8").replace(
                "TODO(feature):", "Verified:"
            ),
            encoding="utf-8",
        )
        _replace_once(
            html_path,
            '<code data-tested-revision>pending</code>',
            f"<code data-tested-revision>{commit}</code>",
        )
        _replace_once(
            html_path,
            '<code data-content-sha256>pending</code>',
            f"<code data-content-sha256>{digest}</code>",
        )
        _replace_once(
            html_path,
            "<!-- feature-commands:append-before -->",
            "<pre><code>python3 -m unittest\n1 test passed</code></pre>\n"
            "<!-- feature-commands:append-before -->",
        )
        _replace_once(
            html_path,
            "<!-- feature-evidence:append-before -->",
            '<figure data-feature-evidence="sample-proof">\n'
            '  <img src="assets/result.png" alt="Actual sample result">\n'
            "  <figcaption>Actual fixture result.</figcaption>\n"
            "</figure>\n"
            "<!-- feature-evidence:append-before -->",
        )

        index_path = self.root / "docs" / "features" / "index.html"
        _replace_once(
            index_path,
            new_feature.render_index_entry(
                "sample-feature",
                "Sample feature",
                "planned",
                "Give operators a concrete result they can verify.",
            ),
            new_feature.render_index_entry(
                "sample-feature",
                "Sample feature",
                "verified",
                "Give operators a concrete result they can verify.",
            ),
        )

        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "commit",
                "--amend",
                "--no-edit",
                "-q",
            ],
            check=True,
        )
        return destination, commit, digest

    def test_scaffold_creates_one_html_file_assets_and_index_entry(self):
        destination = self._create()
        self.assertEqual(
            {path.name for path in destination.iterdir()},
            {"index.html", "assets"},
        )
        self.assertEqual(list((destination / "assets").iterdir()), [])
        manifest = _manifest(destination / "index.html")
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["id"], "sample-feature")
        self.assertEqual(manifest["status"], "planned")
        index = (self.root / "docs" / "features" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('data-feature-entry="sample-feature"', index)
        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_scaffold_escapes_html_and_embedded_json(self):
        destination = self._create(
            "safe-feature",
            'Alpha </script> "quoted"',
            "<b>Visible text stays text</b>",
        )
        html = (destination / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("</script> \"quoted\"", html)
        self.assertIn("Alpha &lt;/script&gt; &quot;quoted&quot;", html)
        self.assertIn("&lt;b&gt;Visible text stays text&lt;/b&gt;", html)
        manifest = _manifest(destination / "index.html")
        self.assertEqual(manifest["title"], 'Alpha </script> "quoted"')
        self.assertEqual(
            manifest["summary"], "<b>Visible text stays text</b>"
        )
        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_scaffold_rejects_invalid_input_and_existing_destination(self):
        for feature_id in ("Bad", "two--hyphens", "../escape", "space id"):
            with self.subTest(feature_id=feature_id):
                with self.assertRaises(ValueError):
                    self._create(feature_id)
        with self.assertRaises(ValueError):
            self._create("empty-title", " ", "Summary")
        self._create()
        with self.assertRaises(FileExistsError):
            self._create()

    def test_scaffold_rejects_symlinked_feature_root(self):
        alternate = self.root / "alternate"
        (alternate / "docs").mkdir(parents=True)
        (alternate / "docs" / "features").symlink_to(
            self.root / "docs" / "features",
            target_is_directory=True,
        )
        with self.assertRaises(ValueError):
            new_feature.create_feature(
                alternate,
                "unsafe-feature",
                "Unsafe feature",
                "Must not follow a symlinked feature root.",
            )

    def test_verified_single_html_record_is_valid(self):
        destination, commit, digest = self._make_verified()
        self.assertEqual(validate_feature_docs.validate(self.root), [])
        html = (destination / "index.html").read_text(encoding="utf-8")
        self.assertIn(commit, html)
        self.assertIn(digest, html)
        self.assertIn('src="assets/result.png"', html)

    def test_extra_feature_sidecar_is_rejected(self):
        destination = self._create()
        (destination / "notes.md").write_text("extra\n", encoding="utf-8")
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any("feature directory may contain only index.html and assets/" in e
                for e in errors),
            errors,
        )

    def test_missing_or_duplicate_manifest_is_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        html = html_path.read_text(encoding="utf-8")
        manifest_tag = MANIFEST_RE.search(html)
        assert manifest_tag is not None
        html_path.write_text(
            html[: manifest_tag.start()] + html[manifest_tag.end() :],
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("expected exactly one embedded manifest" in e for e in errors))

    def test_wrong_manifest_type_and_invalid_utf8_are_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        _replace_once(
            html_path,
            'type="application/json"',
            'type="text/javascript"',
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("script type must be 'application/json'" in e for e in errors))

        html_path.write_bytes(b"\xff")
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("cannot read UTF-8 text" in e for e in errors), errors)

        shutil.rmtree(destination)
        index_path = self.root / "docs" / "features" / "index.html"
        index_path.write_text(
            index_path.read_text(encoding="utf-8").replace(
                new_feature.render_index_entry(
                    "sample-feature",
                    "Sample feature",
                    "planned",
                    "Give operators a concrete result they can verify.",
                ),
                "",
            ),
            encoding="utf-8",
        )
        destination = self._create()
        html_path = destination / "index.html"
        html = html_path.read_text(encoding="utf-8")
        manifest_tag = MANIFEST_RE.search(html)
        assert manifest_tag is not None
        html_path.write_text(
            html[: manifest_tag.end()]
            + manifest_tag.group(0)
            + html[manifest_tag.end() :],
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("expected exactly one embedded manifest" in e for e in errors))

    def test_manifest_duplicate_key_and_unknown_field_are_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        html = html_path.read_text(encoding="utf-8")
        match = MANIFEST_RE.search(html)
        assert match is not None
        duplicate = match.group(2).replace(
            '"schema_version": 3,',
            '"schema_version": 3, "schema_version": 3,',
            1,
        )
        html_path.write_text(
            html[: match.start(2)] + duplicate + html[match.end(2) :],
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("duplicate key" in e for e in errors), errors)

        shutil.rmtree(destination)
        index_path = self.root / "docs" / "features" / "index.html"
        index_path.write_text(
            index_path.read_text(encoding="utf-8").replace(
                new_feature.render_index_entry(
                    "sample-feature",
                    "Sample feature",
                    "planned",
                    "Give operators a concrete result they can verify.",
                ),
                "",
            ),
            encoding="utf-8",
        )
        destination = self._create()
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["surprise"] = True
        _write_manifest(html_path, manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("unknown fields: surprise" in e for e in errors), errors)

    def test_external_page_asset_is_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        _replace_once(
            html_path,
            "<!-- feature-evidence:append-before -->",
            '<img src="https://example.com/proof.png" alt="external">\n'
            "<!-- feature-evidence:append-before -->",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("external resource is not allowed" in e for e in errors), errors)

    def test_external_link_is_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        _replace_once(
            html_path,
            "<!-- feature-evidence:append-before -->",
            '<a href="https://example.com/private-report">external report</a>\n'
            "<!-- feature-evidence:append-before -->",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("external resource is not allowed" in e for e in errors), errors)

    def test_duplicate_csp_directive_is_rejected(self):
        destination = self._create()
        html_path = destination / "index.html"
        _replace_once(
            html_path,
            "default-src 'none';",
            "img-src https:; default-src 'none';",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any("duplicate CSP directive 'img-src'" in error for error in errors),
            errors,
        )

    def test_catalog_and_template_shells_enforce_offline_safety(self):
        catalog_path = self.root / "docs" / "features" / "index.html"
        catalog = catalog_path.read_text(encoding="utf-8")
        catalog_path.write_text(
            catalog.replace(
                "</head>",
                '<script src="https://example.com/catalog.js"></script></head>',
                1,
            ),
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any(
                "docs/features/index.html: scripts are not allowed" in error
                for error in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "docs/features/index.html: external resource is not allowed" in error
                for error in errors
            ),
            errors,
        )

        catalog_path.write_text(catalog, encoding="utf-8")
        template_path = (
            self.root / "docs" / "features" / "_template" / "index.html"
        )
        template = template_path.read_text(encoding="utf-8")
        template_path.write_text(
            template.replace(
                "default-src 'none';",
                "img-src https:; default-src 'none';",
                1,
            ).replace(
                "</body>",
                '<a href="https://example.com/template">external</a></body>',
                1,
            ),
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any(
                "docs/features/_template/index.html: duplicate CSP directive "
                "'img-src'" in error
                for error in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "docs/features/_template/index.html: external resource is not "
                "allowed" in error
                for error in errors
            ),
            errors,
        )

    def test_evidence_hash_and_marker_are_enforced(self):
        destination, _, _ = self._make_verified()
        asset = destination / "assets" / "result.png"
        asset.write_bytes(PNG + b"changed")
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("sha256 does not match" in e for e in errors), errors)

        asset.write_bytes(PNG)
        html_path = destination / "index.html"
        _replace_once(
            html_path,
            'data-feature-evidence="sample-proof"',
            'data-feature-evidence="wrong-proof"',
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("missing evidence marker 'sample-proof'" in e for e in errors), errors)

    def test_unreferenced_or_symlinked_asset_is_rejected(self):
        destination = self._create()
        extra = destination / "assets" / "extra.png"
        extra.write_bytes(PNG)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("unreferenced asset" in e for e in errors), errors)

        extra.unlink()
        source = self.root / "outside.png"
        source.write_bytes(PNG)
        extra.symlink_to(source)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("asset must not be a symlink" in e for e in errors), errors)

    def test_index_entry_must_match_manifest(self):
        self._create()
        index = self.root / "docs" / "features" / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "Give operators a concrete result they can verify.",
                "Wrong summary",
                1,
            ),
            encoding="utf-8",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("index entry must match embedded manifest" in e for e in errors), errors)

    def test_unknown_dependency_and_cycle_are_rejected(self):
        first = self._create()
        first_html = first / "index.html"
        first_manifest = _manifest(first_html)
        first_manifest["depends_on"] = ["missing-feature"]
        _write_manifest(first_html, first_manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("unknown dependency 'missing-feature'" in e for e in errors), errors)

        first_manifest["depends_on"] = ["second-feature"]
        _write_manifest(first_html, first_manifest)
        second = self._create(
            "second-feature",
            "Second feature",
            "Depend on the first feature.",
        )
        second_html = second / "index.html"
        second_manifest = _manifest(second_html)
        second_manifest["depends_on"] = ["sample-feature"]
        _write_manifest(second_html, second_manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("dependency cycle" in e for e in errors), errors)

    def test_content_change_during_evidence_amend_is_rejected(self):
        _, _, _ = self._make_verified()
        (self.root / "crew" / "sample.py").write_text(
            "# changed after evidence\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "commit",
                "--amend",
                "--no-edit",
                "-q",
            ],
            check=True,
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any("content_sha256 does not match the feature commit" in e for e in errors),
            errors,
        )

    def test_unreachable_tested_candidate_is_allowed(self):
        destination, original, _ = self._make_verified()
        invented = "b" * 40
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["verification"]["tested_revision"] = invented
        _write_manifest(html_path, manifest)
        _replace_once(
            html_path,
            f"<code data-tested-revision>{original}</code>",
            f"<code data-tested-revision>{invented}</code>",
        )
        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_reachable_tested_candidate_from_different_parent_is_rejected(self):
        destination, original, _ = self._make_verified()
        anchor_parent = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD^"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        parent_tree = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", f"{anchor_parent}^{{tree}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        divergent_parent = subprocess.run(
            ["git", "-C", str(self.root), "commit-tree", parent_tree, "-p", anchor_parent],
            input="Create different candidate base\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidate_tree = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", f"{original}^{{tree}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        divergent_candidate = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "commit-tree",
                candidate_tree,
                "-p",
                divergent_parent,
            ],
            input="Create candidate on different base\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["verification"]["tested_revision"] = divergent_candidate
        _write_manifest(html_path, manifest)
        _replace_once(
            html_path,
            f"<code data-tested-revision>{original}</code>",
            f"<code data-tested-revision>{divergent_candidate}</code>",
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("must have the same parent" in e for e in errors), errors)

    def test_nonexistent_explicit_delivery_commit_is_rejected(self):
        destination, _, _ = self._make_verified()
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["delivery"][0]["commit"] = "b" * 40
        _write_manifest(html_path, manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("delivery[0].commit does not exist" in e for e in errors), errors)

    def test_undeclared_feature_path_and_code_test_overlap_are_rejected(self):
        destination, _, _ = self._make_verified()
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["code_paths"] = []
        _write_manifest(html_path, manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("undeclared changed paths: crew/sample.py" in e for e in errors), errors)

        manifest["code_paths"] = ["crew/sample.py"]
        manifest["test_paths"]["backend"].append("crew/sample.py")
        _write_manifest(html_path, manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("code_paths and test_paths must not overlap" in e for e in errors), errors)

    def test_malformed_field_types_report_errors_without_traceback(self):
        destination = self._create()
        asset = destination / "assets" / "result.png"
        asset.write_bytes(PNG)
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        manifest["status"] = []
        manifest["code_paths"] = [[]]
        manifest["test_paths"]["backend"] = [{}]
        manifest["verification"]["evidence"] = [
            {
                "id": "typed-proof",
                "kind": [],
                "path": "assets/result.png",
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "description": "Malformed type fixture.",
            }
        ]
        _write_manifest(html_path, manifest)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("status must be one of" in e for e in errors), errors)
        self.assertTrue(any("code_paths[0] must be repo-relative" in e for e in errors), errors)
        self.assertTrue(any("test_paths.backend[0] must be repo-relative" in e for e in errors), errors)
        self.assertTrue(any(".kind must be image or video" in e for e in errors), errors)

    def test_manifest_text_cannot_spoof_visible_summary(self):
        destination = self._create()
        html_path = destination / "index.html"
        manifest = _manifest(html_path)
        old_summary = manifest["summary"]
        new_summary = "This sentence exists only inside JSON."
        manifest["summary"] = new_summary
        _write_manifest(html_path, manifest)
        index_path = self.root / "docs" / "features" / "index.html"
        _replace_once(
            index_path,
            new_feature.render_index_entry(
                "sample-feature", "Sample feature", "planned", old_summary
            ),
            new_feature.render_index_entry(
                "sample-feature", "Sample feature", "planned", new_summary
            ),
        )
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("summary must be visible outside the manifest" in e for e in errors), errors)

    def test_print_content_digest_cli(self):
        destination = self._create()
        fixture = self.root / "crew" / "sample.py"
        fixture.parent.mkdir()
        fixture.write_text("# fixture\n", encoding="utf-8")
        manifest = _manifest(destination / "index.html")
        manifest["code_paths"] = ["crew/sample.py"]
        _write_manifest(destination / "index.html", manifest)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add sample code"],
            check=True,
        )
        expected = validate_feature_docs.revision_content_sha256(
            self.root, manifest, "HEAD"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "validate_feature_docs.py"),
                "--root",
                str(self.root),
                "--print-content-digest",
                "sample-feature",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected)

    def test_print_content_digest_refuses_dirty_declared_content(self):
        self._make_verified()
        (self.root / "crew" / "sample.py").write_text(
            "# uncommitted change\n", encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "validate_feature_docs.py"),
                "--root",
                str(self.root),
                "--print-content-digest",
                "sample-feature",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("declared content is dirty", result.stderr)

    def test_symlinked_feature_index_is_rejected(self):
        destination = self._create()
        outside = self.root / "outside.html"
        outside.write_text(
            (destination / "index.html").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (destination / "index.html").unlink()
        (destination / "index.html").symlink_to(outside)
        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(any("index.html: missing or symlinked" in e for e in errors), errors)

    def test_new_feature_cli(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "new_feature.py"),
                "cli-feature",
                "--title",
                "CLI feature",
                "--summary",
                "Create a single HTML record.",
                "--root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.root / "docs" / "features" / "cli-feature" / "index.html").is_file()
        )
        self.assertTrue(
            (self.root / "docs" / "features" / "cli-feature" / "assets").is_dir()
        )
        self.assertEqual(validate_feature_docs.validate(self.root), [])


if __name__ == "__main__":
    unittest.main()
