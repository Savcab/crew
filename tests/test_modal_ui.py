"""Run dependency-free modal behavior contracts in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required for modal UI controller checks")
class ModalUiControllerTests(unittest.TestCase):
    def test_cap_and_identity_contracts(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "js" / "modal_contract.mjs"),
             str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(
            result.returncode, 0,
            f"node modal checks failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("16 passed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
