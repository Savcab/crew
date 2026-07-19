"""Run dependency-free dashboard API authentication behavior in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required for API UI controller checks")
class ApiUiControllerTests(unittest.TestCase):
    def test_capability_hash_change_bootstraps_an_open_dashboard(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "js" / "api_auth.mjs"), str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(
            result.returncode, 0,
            f"node API checks failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("1 passed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
