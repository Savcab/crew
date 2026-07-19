"""Run dependency-free graph layout behavior checks in Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required for graph UI controller checks")
class GraphUiControllerTests(unittest.TestCase):
    def test_pinned_collisions_do_not_animate_forever(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests" / "js" / "graph_layout.mjs"),
             str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(
            result.returncode, 0,
            "node graph checks failed\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("5 passed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
