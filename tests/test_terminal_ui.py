"""Run dependency-free terminal/dock controller behavior checks in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required for terminal UI controller checks")
class TerminalUiControllerTests(unittest.TestCase):
    def test_terminal_transport_and_dock_controller(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "js" / "terminal_transport.mjs"),
             str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(
            result.returncode, 0,
            f"node controller checks failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("24 passed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
