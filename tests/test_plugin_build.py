"""The installer must never copy the workspace's local catalog into a plugin."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PluginBuildTests(unittest.TestCase):
    def test_allowlist_excludes_local_data_and_matches_source_hashes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "marked"
            subprocess.run(
                [sys.executable, "-B", str(root / "scripts/build_plugin.py"),
                 "--output", str(output)],
                cwd=root, check=True, capture_output=True, text=True,
            )
            manifest = json.loads((output / "BUILD_MANIFEST.json").read_text())
            actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
            self.assertEqual(actual, set(manifest["files"]) | {"BUILD_MANIFEST.json"})
            self.assertFalse(any(path.startswith((".marked/", ".git/", "tests/")) for path in actual))
            self.assertFalse(any(path.endswith(".sqlite3") for path in actual))
            for relative, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256((output / relative).read_bytes()).hexdigest(), expected)
                self.assertEqual((output / relative).read_bytes(), (root / relative).read_bytes())
            refused = subprocess.run(
                [sys.executable, "-B", str(root / "scripts/build_plugin.py"),
                 "--output", str(output)],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("pass --replace", refused.stderr)
            replaced = subprocess.run(
                [sys.executable, "-B", str(root / "scripts/build_plugin.py"),
                 "--output", str(output), "--replace"],
                cwd=root, check=True, capture_output=True, text=True,
            )
            self.assertTrue(json.loads(replaced.stdout)["replaced"])
            self.assertEqual((output / "README.md").read_bytes(), (root / "README.md").read_bytes())

    def test_repo_marketplace_package_matches_allowlisted_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        marketplace = json.loads((root / ".agents/plugins/marketplace.json").read_text())
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["source"]["source"], "local")
        self.assertEqual(entry["source"]["path"], "./plugins/marked")
        package = root / entry["source"]["path"]
        manifest = json.loads((package / "BUILD_MANIFEST.json").read_text())
        actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
        self.assertEqual(actual, set(manifest["files"]) | {"BUILD_MANIFEST.json"})
        self.assertFalse(any(path.startswith((".marked/", ".git/", "tests/")) for path in actual))
        self.assertFalse(any(path.endswith((".sqlite3", ".db")) for path in actual))
        for relative, expected in manifest["files"].items():
            raw = (package / relative).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
            self.assertEqual(raw, (root / relative).read_bytes())


if __name__ == "__main__":
    unittest.main()
