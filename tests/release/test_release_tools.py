"""Exercise secret redaction and history-free export against disposable repos."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import audit_release
import export_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.addCleanup(patch.stopall)
        patch.object(audit_release, "ROOT", self.root).start()
        patch.object(export_release, "ROOT", self.root).start()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def export(self, destination):
        with patch.object(sys, "argv", ["export_release.py", str(destination)]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return export_release.main()

    def test_export_committed_sources_without_history_or_private_material(self):
        self.write("README.md", "Example project\n")
        self.write("frontend/package.json", "{}\n")
        script = self.write("scripts/run.sh", "#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.write("private.pdf", "Private attachment\n")
        self.write("docs/archive/notes.md", "Private notes\n")
        self.write("frontend/.env.local", "PRIVATE_VALUE=local\n")
        self.commit()
        destination = Path(self.temp.name) / "export"
        self.assertEqual(self.export(destination), 0)
        self.assertFalse((destination / ".git").exists())
        self.assertFalse((destination / "private.pdf").exists())
        self.assertFalse((destination / "docs/archive").exists())
        self.assertFalse((destination / "frontend/.env.local").exists())
        self.assertEqual((destination / "README.md").read_text(), "Example project\n")
        self.assertTrue((destination / "scripts/run.sh").stat().st_mode & 0o111)
        manifest = json.loads((destination / "RELEASE_MANIFEST.json").read_text())
        self.assertEqual(set(manifest), {"README.md", "frontend/package.json", "scripts/run.sh"})

    def test_export_refuses_secret_before_writing_any_files(self):
        token = "sk-" + "Ab9Qw7Er5Ty3Ui1Op8As6Df4Gh2Jk0Lz"
        self.write("README.md", token)
        self.commit()
        destination = Path(self.temp.name) / "export"
        with self.assertRaises(SystemExit):
            self.export(destination)
        self.assertFalse(destination.exists())
        findings = audit_release.audit(False)["findings"]
        self.assertTrue(findings)
        self.assertNotIn(token, json.dumps(findings))

    def test_history_finds_deleted_secret_and_export_only_uses_head(self):
        token = "sk-" + "Ab9Qw7Er5Ty3Ui1Op8As6Df4Gh2Jk0Lz"
        self.write("README.md", token)
        self.commit()
        self.write("README.md", "Sanitized\n")
        self.commit()
        self.assertFalse(audit_release.audit(False)["findings"])
        self.assertTrue(audit_release.audit(True)["findings"])
        destination = Path(self.temp.name) / "export"
        self.export(destination)
        self.assertNotIn(token, (destination / "README.md").read_text())

    def test_export_refuses_dirty_tree_and_existing_destination(self):
        self.write("README.md", "Example\n")
        self.commit()
        self.write("README.md", "Changed\n")
        with self.assertRaises(SystemExit):
            self.export(Path(self.temp.name) / "export")
        self.commit()
        with self.assertRaises(SystemExit):
            self.export(self.root)

    def test_export_refuses_symlink_escape(self):
        (self.root / "README.md").symlink_to("../outside")
        self.commit()
        with self.assertRaises(SystemExit):
            self.export(Path(self.temp.name) / "export")

    def test_export_path_filter_rejects_traversal_and_artifacts(self):
        for name in ("docker/../../outside", "/README.md", "frontend/test-results/screenshot.png", "frontend/.npm-cache/package.json"):
            self.assertFalse(export_release.public_path(name))
        self.assertTrue(export_release.public_path("tests/fixtures/test_payload.json"))

    def test_scanner_distinguishes_expressions_placeholders_and_literals(self):
        self.assertFalse(audit_release.scan(b'formData.password = formData.password.replace(/x/, "")', "Login.vue"))
        self.assertFalse(audit_release.scan(b'LLM_API_KEY=your_api_key_placeholder', ".env.example"))
        value = "q8Az5Ws2Ed9Rf6Tg3Yh0Uj7Ik4Ol1Pm"
        self.assertTrue(audit_release.scan(("LLM_API_KEY=" + value).encode(), ".env"))
        self.assertFalse(audit_release.scan(b'"CRITICAL RULES:\\n"', "prompt.py"))


if __name__ == "__main__":
    unittest.main()
