"""Data-folder resolution, the test-only instance token, and import side effects.

These cover the rules that decide where an installed (frozen) build keeps its
settings, and that a test run can never share data or a single-instance mutex
with the owner's real running copy.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import runtime  # noqa: E402


class InstanceTokenTests(unittest.TestCase):
    def test_absent_token_is_empty(self):
        self.assertEqual(runtime._read_instance_token({}, ["app.exe"]), "")

    def test_token_is_sanitized_and_bounded(self):
        tok = runtime._read_instance_token(
            {"ASC_INSTANCE_TOKEN": " ab c/..\\d:e*" + "x" * 80}, ["app.exe"])
        self.assertTrue(tok.startswith("abcde"))
        self.assertLessEqual(len(tok), 40)
        self.assertRegex(tok, r"^[A-Za-z0-9_-]+$")

    def test_self_test_gets_its_own_token(self):
        tok = runtime._read_instance_token({}, ["app.exe", "--self-test"])
        self.assertTrue(tok.startswith("selftest-"))

    def test_explicit_token_wins_over_self_test(self):
        tok = runtime._read_instance_token(
            {"ASC_INSTANCE_TOKEN": "ci1"}, ["app.exe", "--self-test"])
        self.assertEqual(tok, "ci1")

    def test_object_names_are_suffixed_only_with_a_token(self):
        self.assertEqual(runtime.instance_object_name("Base", ""), "Base")
        self.assertEqual(runtime.instance_object_name("Base", "t1"), "Base-t1")


class DataDirCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "app"
        self.root.mkdir()
        self.local = str(Path(self.tmp.name) / "local")
        self.temp = str(Path(self.tmp.name) / "temp")

    def tearDown(self):
        self.tmp.cleanup()

    def _cands(self, frozen, token=""):
        return runtime._data_dir_candidates(
            frozen=frozen, root=self.root, token=token,
            local=self.local, temp=self.temp)

    def test_source_run_keeps_data_beside_the_app(self):
        c = self._cands(frozen=False)
        self.assertEqual(c[0], self.root)
        self.assertEqual(c[1], Path(self.local) / runtime.APP_NAME)

    def test_installed_build_uses_local_appdata_not_the_install_dir(self):
        c = self._cands(frozen=True)
        self.assertNotIn(self.root, c)
        self.assertEqual(c[0], Path(self.local) / runtime.APP_NAME)

    def test_portable_marker_keeps_data_beside_the_exe(self):
        (self.root / runtime.PORTABLE_MARKER_NAME).write_text("", encoding="utf-8")
        c = self._cands(frozen=True)
        self.assertEqual(c[0], self.root)

    def test_token_redirects_everything_to_a_private_temp_dir(self):
        for frozen in (False, True):
            c = self._cands(frozen=frozen, token="abc")
            self.assertEqual(c, [Path(self.temp) / "asc-instance-abc"])


class IconPathTests(unittest.TestCase):
    def test_shipped_icon_is_used_not_a_generated_one(self):
        # A packaged build keeps data in LOCALAPPDATA; the icon must still be the
        # shipped one (beside the app, or in the bundle), never the placeholder.
        self.assertEqual(runtime.ICON_PATH, ROOT / "icon.ico")
        self.assertTrue(runtime.ICON_PATH.exists())


class LegacyMigrationTests(unittest.TestCase):
    def test_missing_appdata_never_migrates_a_relative_folder(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as cwd:
            rel = Path(cwd) / runtime.APP_NAME
            rel.mkdir()
            (rel / "config.json").write_text("{}", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "APPDATA"}
            old_cwd = os.getcwd()
            os.chdir(cwd)
            try:
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(runtime, "INSTANCE_TOKEN", ""):
                    runtime.migrate_legacy_appdata()
            finally:
                os.chdir(old_cwd)
            self.assertTrue((rel / "config.json").exists())


class ImportSideEffectTests(unittest.TestCase):
    """Importing the engine must not open the log or touch legacy data — a test,
    a --quit call, or a self-test would otherwise write into the user's folder."""

    def test_import_does_not_create_the_log_until_configured(self):
        token = "pytest-" + uuid.uuid4().hex[:8]
        env = dict(os.environ, ASC_INSTANCE_TOKEN=token, PYTHONIOENCODING="utf-8")
        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "from anime_subs import runtime as r\n"
            "print(r.APPDATA_DIR)\n"
            "print(r.LOG_PATH.exists())\n"
            "r.configure_logging(); r.logger.info('hello')\n"
            "print(r.LOG_PATH.exists())\n" % ROOT
        )
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        data_dir = Path(lines[0])
        try:
            self.assertEqual(data_dir.name, f"asc-instance-{token}")
            self.assertEqual(lines[1], "False", "monitor.log created at import time")
            self.assertEqual(lines[2], "True", "configure_logging() did not open the log")
        finally:
            for p in sorted(data_dir.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
            try:
                data_dir.rmdir()
            except OSError:
                pass

    def test_configure_logging_tolerates_missing_stderr(self):
        token = "pytest-" + uuid.uuid4().hex[:8]
        env = dict(os.environ, ASC_INSTANCE_TOKEN=token, PYTHONIOENCODING="utf-8")
        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "sys.stderr = None\n"
            "from anime_subs import runtime as r\n"
            "r.configure_logging(); r.logger.warning('no console here')\n"
            "import logging\n"
            "print(sum(isinstance(h, logging.StreamHandler) and not "
            "isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers))\n"
            % ROOT
        )
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
