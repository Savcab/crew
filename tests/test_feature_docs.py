"""Contracts for feature dossier scaffolding and validation."""

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


class FeatureDocsRepositoryTests(unittest.TestCase):
    def test_repository_feature_docs_are_valid(self):
        self.assertEqual(validate_feature_docs.validate(ROOT), [])


class FeatureScaffoldTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="crew-feature-docs-")
        self.root = Path(self._temp.name)
        shutil.copytree(
            ROOT / "docs" / "features", self.root / "docs" / "features"
        )
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Feature Docs Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "config",
                "user.email",
                "feature-docs@example.invalid",
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
                "Add feature dossier framework",
            ],
            check=True,
        )

    def tearDown(self):
        self._temp.cleanup()

    def _create(self, feature_id="sample-feature"):
        return new_feature.create_feature(
            self.root,
            feature_id,
            "Sample feature",
            "Give operators a concrete result they can verify.",
        )

    def _make_verified(self):
        destination = self._create()
        for relative in (
            "crew/sample.py",
            "tests/test_sample.py",
            "tests/sample_live.py",
            "tests/browser/sample.md",
            "frontend/tests/sample.test.js",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")

        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "surfaces": ["dashboard"],
                "code_paths": ["crew/sample.py"],
                "test_paths": {
                    "backend": ["tests/test_sample.py"],
                    "frontend": ["frontend/tests/sample.test.js"],
                    "integration": ["tests/sample_live.py"],
                    "live": [],
                    "browser": ["tests/browser/sample.md"],
                },
                "delivery": [{"name": "Implement sample", "commit": "self"}],
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

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
        content_digest = validate_feature_docs.revision_content_sha256(
            self.root, manifest, commit
        )

        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
            "ASsJTYQAAAAASUVORK5CYII="
        )
        image_path = destination / "evidence" / "result.png"
        image_path.parent.mkdir()
        image_path.write_bytes(image)

        manifest["status"] = "verified"
        manifest["verification"] = {
            "tested_revision": commit,
            "content_sha256": content_digest,
            "commands": [
                {"command": "python3 -m unittest", "result": "1 test passed"}
            ],
            "evidence": [
                {
                    "id": "sample-proof",
                    "kind": "image",
                    "path": "evidence/result.png",
                    "sha256": hashlib.sha256(image).hexdigest(),
                    "description": "Actual fixture result.",
                }
            ],
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        evidence_path = destination / "evidence.md"
        evidence = (
            evidence_path.read_text(encoding="utf-8")
            .replace("`pending`", f"`{commit}`", 1)
            .replace("`pending`", f"`{content_digest}`", 1)
            .replace("TODO(feature):", "Verified:")
        )
        evidence_path.write_text(evidence, encoding="utf-8")

        for name in ("README.md", "spec.md"):
            path = destination / name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "TODO(feature):", "Verified:"
                ),
                encoding="utf-8",
            )

        html_path = destination / "explainer.html"
        html = (
            html_path.read_text(encoding="utf-8")
            .replace("TODO(feature):", "Verified:")
            .replace(
                '<figure class="panel proof">',
                '<figure class="panel proof" data-feature-evidence="sample-proof">\n'
                '      <img alt="Actual sample result" '
                'src="evidence/result.png">',
            )
        )
        html_path.write_text(html, encoding="utf-8")

        index_path = self.root / "docs" / "features" / "README.md"
        index = index_path.read_text(encoding="utf-8").replace(
            "| [Sample feature](sample-feature/) | planned | "
            "Give operators a concrete result they can verify. |",
            "| [Sample feature](sample-feature/) | verified | "
            "Give operators a concrete result they can verify. |",
        )
        index_path.write_text(index, encoding="utf-8")

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
        return destination

    def test_scaffold_creates_complete_planned_dossier_and_index_entry(self):
        destination = self._create()

        self.assertEqual(
            {path.name for path in destination.iterdir()},
            validate_feature_docs.REQUIRED_FILES,
        )
        manifest = json.loads(
            (destination / "feature.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["id"], "sample-feature")
        self.assertEqual(manifest["status"], "planned")
        index = (self.root / "docs" / "features" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[Sample feature](sample-feature/)", index)
        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_scaffold_refuses_invalid_or_duplicate_ids_without_extra_index_rows(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            self._create("Not Valid")

        self._create()
        with self.assertRaises(FileExistsError):
            self._create()
        index = (self.root / "docs" / "features" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(index.count("[Sample feature](sample-feature/)"), 1)

    def test_scaffold_escapes_json_and_html_values(self):
        destination = new_feature.create_feature(
            self.root,
            "quoted-feature",
            'Quoted "feature" <safe>',
            'Show "quoted" input without creating executable HTML.',
        )

        manifest = json.loads(
            (destination / "feature.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["title"], 'Quoted "feature" <safe>')
        html = (destination / "explainer.html").read_text(encoding="utf-8")
        self.assertIn("Quoted &quot;feature&quot; &lt;safe&gt;", html)
        self.assertNotIn('Quoted "feature" <safe>', html)
        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_generator_refuses_symlinked_index_without_writing_outside(self):
        index_path = self.root / "docs" / "features" / "README.md"
        outside = self.root / "outside-index.md"
        original = index_path.read_text(encoding="utf-8")
        outside.write_text(original, encoding="utf-8")
        index_path.unlink()
        index_path.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "index must not be a symlink"):
            self._create()

        self.assertEqual(outside.read_text(encoding="utf-8"), original)

    def test_tools_reject_symlinked_docs_parent(self):
        alternate_root = self.root / "alternate-root"
        outside_root = self.root / "outside-root"
        alternate_root.mkdir()
        shutil.copytree(ROOT / "docs", outside_root / "docs")
        (alternate_root / "docs").symlink_to(outside_root / "docs")

        with self.assertRaisesRegex(ValueError, "repo-relative parents"):
            new_feature.create_feature(
                alternate_root,
                "escaped-feature",
                "Escaped feature",
                "This must never be written outside the selected repository.",
            )
        self.assertEqual(
            validate_feature_docs.validate(alternate_root),
            ["docs/features: path and repo-relative parents must not be symlinks"],
        )
        self.assertFalse(
            (outside_root / "docs" / "features" / "escaped-feature").exists()
        )

    def test_validator_rejects_required_file_symlink(self):
        destination = self._create()
        outside = self.root / "outside-readme.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        readme = destination / "README.md"
        readme.unlink()
        readme.symlink_to(outside)

        errors = validate_feature_docs.validate(self.root)

        self.assertIn(
            "sample-feature/README.md: required file must not be a symlink",
            errors,
        )

    def test_validator_reports_invalid_utf8_without_traceback(self):
        destination = self._create()
        (destination / "spec.md").write_bytes(b"\xff")

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any("sample-feature/spec.md: cannot read UTF-8 text" in error for error in errors)
        )

    def test_validator_rejects_nonexistent_explicit_delivery_commit(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original = manifest["verification"]["tested_revision"]
        invented = "b" * 40
        manifest["verification"]["tested_revision"] = invented
        manifest["delivery"][0]["commit"] = invented
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        evidence_path = destination / "evidence.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8").replace(original, invented),
            encoding="utf-8",
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any("delivery[0].commit does not exist" in error for error in errors)
        )
        self.assertFalse(
            any("tested revision does not exist" in error for error in errors)
        )

    def test_validator_rejects_duplicate_manifest_keys(self):
        destination = self._create()
        manifest_path = destination / "feature.json"
        manifest = manifest_path.read_text(encoding="utf-8").replace(
            '  "id": "sample-feature",',
            '  "id": "sample-feature",\n  "id": "sample-feature",',
        )
        manifest_path.write_text(manifest, encoding="utf-8")

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(any("duplicate key 'id'" in error for error in errors))

    def test_validator_reports_unhashable_field_types_without_traceback(self):
        destination = self._create()
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
            "ASsJTYQAAAAASUVORK5CYII="
        )
        image_path = destination / "evidence" / "result.png"
        image_path.parent.mkdir()
        image_path.write_bytes(image)
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = []
        manifest["code_paths"] = [[]]
        manifest["test_paths"]["backend"] = [{}]
        manifest["verification"]["evidence"] = [
            {
                "id": "typed-proof",
                "kind": [],
                "path": "evidence/result.png",
                "sha256": hashlib.sha256(image).hexdigest(),
                "description": "Malformed type fixture.",
            }
        ]
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(any("status must be one of" in error for error in errors))
        self.assertTrue(any("code_paths[0] must be repo-relative" in error for error in errors))
        self.assertTrue(any("test_paths.backend[0] must be repo-relative" in error for error in errors))
        self.assertTrue(any(".kind must be image or video" in error for error in errors))

    def test_complete_verified_dossier_passes(self):
        self._make_verified()

        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_verified_dossier_allows_unreachable_tested_candidate(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original = manifest["verification"]["tested_revision"]
        candidate = "b" * 40
        manifest["verification"]["tested_revision"] = candidate
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        evidence_path = destination / "evidence.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8").replace(original, candidate),
            encoding="utf-8",
        )

        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_validator_rejects_reachable_candidate_from_different_parent(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original = manifest["verification"]["tested_revision"]
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
            [
                "git",
                "-C",
                str(self.root),
                "commit-tree",
                parent_tree,
                "-p",
                anchor_parent,
            ],
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
        manifest["verification"]["tested_revision"] = divergent_candidate
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        evidence_path = destination / "evidence.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8").replace(
                original, divergent_candidate
            ),
            encoding="utf-8",
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any(
                "tested revision and feature commit must have the same parent"
                in error
                for error in errors
            )
        )

    def test_stacked_descendant_may_change_declared_content(self):
        self._make_verified()
        (self.root / "crew" / "sample.py").write_text(
            "# changed after evidence\n", encoding="utf-8"
        )

        self.assertEqual(validate_feature_docs.validate(self.root), [])

    def test_validator_rejects_digest_that_does_not_match_feature_commit(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["verification"]["content_sha256"] = "a" * 64
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        evidence_path = destination / "evidence.md"
        evidence_path.write_text(
            re.sub(
                r"`[0-9a-f]{64}`",
                f"`{'a' * 64}`",
                evidence_path.read_text(encoding="utf-8"),
                count=1,
            ),
            encoding="utf-8",
        )

        errors = validate_feature_docs.validate(self.root)
        self.assertTrue(
            any(
                "verification.content_sha256 does not match the feature commit"
                in error
                for error in errors
            )
        )

    def test_validator_rejects_undeclared_feature_commit_path(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["code_paths"] = []
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any(
                "feature commit has undeclared changed paths: crew/sample.py" in error
                for error in errors
            )
        )

    def test_validator_rejects_overlapping_code_and_test_paths(self):
        destination = self._make_verified()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["test_paths"]["backend"].append("crew/sample.py")
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any("code_paths and test_paths must not overlap" in error for error in errors)
        )

    def test_command_line_prints_declared_content_digest(self):
        destination = self._make_verified()
        manifest = json.loads(
            (destination / "feature.json").read_text(encoding="utf-8")
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

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(), manifest["verification"]["content_sha256"]
        )

    def test_command_line_refuses_dirty_declared_content(self):
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
        self.assertIn("must be clean at HEAD", result.stderr)

    def test_verified_status_requires_delivery_tests_commands_media_and_commit(self):
        destination = self._create()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "verified"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(
            any("verified features need delivery commits" in error for error in errors)
        )
        self.assertTrue(
            any("verified features need image or video evidence" in error for error in errors)
        )
        self.assertTrue(
            any("verified features need a tested revision" in error for error in errors)
        )
        self.assertTrue(
            any("verified features need a content_sha256" in error for error in errors)
        )
        self.assertTrue(
            any("verified features need image or video proof" in error for error in errors)
        )
        self.assertTrue(
            any(
                "verified features need backend or frontend test paths" in error
                for error in errors
            )
        )
        self.assertTrue(
            any(
                "verified features need integration or live test paths" in error
                for error in errors
            )
        )
        self.assertTrue(
            any("row for sample-feature must match its manifest" in error for error in errors)
        )
        self.assertTrue(
            any("verified dossier retains scaffold TODOs" in error for error in errors)
        )

    def test_command_line_generator_reports_invalid_input(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "new_feature.py"),
                "Bad ID",
                "--title",
                "Bad",
                "--summary",
                "Invalid identifier should fail.",
                "--root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("lowercase", result.stderr)

    def test_validator_rejects_unknown_dependency(self):
        destination = self._create()
        manifest_path = destination / "feature.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["depends_on"] = ["missing-feature"]
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        errors = validate_feature_docs.validate(self.root)

        self.assertIn(
            "sample-feature/feature.json: unknown dependency 'missing-feature'",
            errors,
        )

    def test_validator_rejects_dependency_cycle(self):
        first = self._create()
        second = new_feature.create_feature(
            self.root,
            "second-feature",
            "Second feature",
            "Depend on the first feature for a visible operator result.",
        )
        for path, dependency in (
            (first / "feature.json", "second-feature"),
            (second / "feature.json", "sample-feature"),
        ):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["depends_on"] = [dependency]
            path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        errors = validate_feature_docs.validate(self.root)

        self.assertTrue(any("dependency cycle" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
