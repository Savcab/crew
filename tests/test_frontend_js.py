"""Drive the frontend vitest contract suites (ports of the old tests/js/*.mjs
node checks: api auth, graph layout, terminal transport + dock, modal/identity)."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NPM = shutil.which("npm")
FRONTEND = ROOT / "frontend"


@unittest.skipUnless(NPM and (FRONTEND / "node_modules").is_dir(),
                     "npm + frontend/node_modules are required for the "
                     "frontend contract checks (run `npm install` in frontend/)")
class FrontendJsContractTests(unittest.TestCase):
    def test_vitest_contract_suites_pass(self):
        result = subprocess.run(
            [NPM, "--prefix", str(FRONTEND), "run", "test"],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        combined = result.stdout + result.stderr
        self.assertEqual(
            result.returncode, 0,
            f"frontend vitest suites failed\n{combined}")
        # Every suite must have run and passed (count grows as suites are added).
        match = re.search(r"Test Files\s+(\d+) passed", combined)
        self.assertTrue(match, f"no passing vitest files reported\n{combined}")
        self.assertGreaterEqual(int(match.group(1)), 4, combined)
        self.assertNotRegex(combined, r"Test Files\s+\d+ failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
