"""The CONTEXT.md contract (see AGENTS.md, "Context docs").

Every registered feature area keeps a CONTEXT.md beside its code with a fixed
four-section shape, so a coding agent can load one small file per area and
trust it. This suite enforces everything mechanical about that promise:
presence, section order, that declared key files exist, and that new crew
subpackages register themselves instead of silently shipping undocumented.

Truthfulness cannot be checked mechanically — that part of the contract is
"update it in the same commit that falsifies it", enforced by review.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One entry per feature area, relative to the repo root. Adding an area to
# the repository means adding it here WITH its CONTEXT.md (AGENTS.md rule).
REGISTERED_AREAS = [
    "crew",
    "crew/harness",
    "crew/server",
    "frontend",
    "tests",
]

REQUIRED_SECTIONS = [
    "## What this area does",
    "## Key files",
    "## Invariants and gotchas",
    "## When to update this file",
]

MAX_LINES = 120  # convention says ~80; this is the hard backstop


def _context_path(area):
    return os.path.join(ROOT, area, "CONTEXT.md")


def _read(area):
    with open(_context_path(area), encoding="utf-8") as f:
        return f.read()


class ContextDocPresence(unittest.TestCase):
    def test_every_registered_area_has_a_context_doc(self):
        missing = [a for a in REGISTERED_AREAS
                   if not os.path.isfile(_context_path(a))]
        self.assertEqual(missing, [],
                         "areas missing CONTEXT.md (see AGENTS.md)")

    def test_every_crew_subpackage_is_a_registered_area(self):
        """A new crew/<pkg> must register itself — the documentation process
        continues, or this fails the moment an undocumented area appears."""
        crew_dir = os.path.join(ROOT, "crew")
        subpackages = sorted(
            f"crew/{name}" for name in os.listdir(crew_dir)
            if os.path.isfile(os.path.join(crew_dir, name, "__init__.py")))
        unregistered = [p for p in subpackages if p not in REGISTERED_AREAS]
        self.assertEqual(
            unregistered, [],
            "new crew subpackage(s) without a registered CONTEXT.md — add "
            "the doc and register the area in tests/test_context_docs.py")


class ContextDocShape(unittest.TestCase):
    def test_sections_are_present_in_order(self):
        for area in REGISTERED_AREAS:
            with self.subTest(area=area):
                text = _read(area)
                self.assertRegex(
                    text.splitlines()[0], r"^# .+ — context$",
                    f"{area}/CONTEXT.md must open '# <area> — context'")
                positions = [text.find(s) for s in REQUIRED_SECTIONS]
                self.assertNotIn(-1, positions,
                                 f"{area}/CONTEXT.md is missing a required "
                                 f"section ({REQUIRED_SECTIONS})")
                self.assertEqual(positions, sorted(positions),
                                 f"{area}/CONTEXT.md sections out of order")

    def test_length_stays_loadable(self):
        for area in REGISTERED_AREAS:
            with self.subTest(area=area):
                lines = _read(area).splitlines()
                self.assertLessEqual(
                    len(lines), MAX_LINES,
                    f"{area}/CONTEXT.md exceeds {MAX_LINES} lines — split "
                    "the area or trim (the point is one SMALL file)")

    def test_summary_is_prose_sized(self):
        for area in REGISTERED_AREAS:
            with self.subTest(area=area):
                text = _read(area)
                summary = text.split("## What this area does", 1)[1]
                summary = summary.split("##", 1)[0]
                content = [l for l in summary.splitlines() if l.strip()]
                self.assertTrue(
                    1 <= len(content) <= 8,
                    f"{area}/CONTEXT.md summary must be 1-8 non-empty lines, "
                    f"got {len(content)}")


class ContextDocKeyFiles(unittest.TestCase):
    def test_every_key_file_exists(self):
        """Backticked paths under '## Key files' must resolve relative to the
        doc's own directory — a renamed file must take its context line with
        it, or this test names the lie."""
        for area in REGISTERED_AREAS:
            with self.subTest(area=area):
                text = _read(area)
                section = text.split("## Key files", 1)[1].split("##", 1)[0]
                paths = re.findall(r"`([^`\s]+)`", section)
                self.assertTrue(
                    paths, f"{area}/CONTEXT.md lists no key files")
                missing = [
                    p for p in paths
                    if not os.path.exists(os.path.join(ROOT, area, p))]
                self.assertEqual(
                    missing, [],
                    f"{area}/CONTEXT.md names key files that do not exist "
                    f"relative to {area}/: {missing}")


if __name__ == "__main__":
    unittest.main()
