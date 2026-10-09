"""Start-at-login shortcut: the right command for source and packaged runs, the
Windows "Startup apps" switch, and test-token isolation.

Pure helpers only — nothing here creates or reads a real shortcut.
"""
import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "anime_subs_customizer.pyw"


def _load_gui_module():
    loader = SourceFileLoader("anime_subs_customizer_autostart_tests", str(MODULE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gui_app = _load_gui_module()
TIM = gui_app.TrayIconManager


class AutostartCommandTests(unittest.TestCase):
    def test_packaged_build_starts_the_exe_itself(self):
        target, args, workdir = TIM._autostart_command(
            frozen=True, executable=r"C:\Apps\ASC\AnimeSubsCustomizer.exe",
            script=r"C:\Apps\ASC\AnimeSubsCustomizer.exe", minimized=True)
        self.assertEqual(target, r"C:\Apps\ASC\AnimeSubsCustomizer.exe")
        self.assertEqual(args, "--minimized")
        self.assertEqual(workdir, r"C:\Apps\ASC")

    def test_packaged_build_without_minimized_has_no_arguments(self):
        _, args, _ = TIM._autostart_command(
            frozen=True, executable=r"C:\A\app.exe", script=r"C:\A\app.exe",
            minimized=False)
        self.assertEqual(args, "")

    def test_source_run_starts_python_with_the_script(self):
        target, args, workdir = TIM._autostart_command(
            frozen=False, executable=r"C:\Py\pythonw.exe",
            script=r"D:\src\anime_subs_customizer.pyw", minimized=True)
        self.assertEqual(target, r"C:\Py\pythonw.exe")
        self.assertEqual(args, '"D:\\src\\anime_subs_customizer.pyw" --minimized')
        self.assertEqual(workdir, r"D:\src")


class ShortcutMatchTests(unittest.TestCase):
    def test_packaged_build_matches_on_target_only(self):
        self.assertTrue(TIM._shortcut_points_here(
            target=r"c:\apps\asc\animesubscustomizer.exe", args="--minimized",
            frozen=True, executable=r"C:\Apps\ASC\AnimeSubsCustomizer.exe",
            script=r"C:\Apps\ASC\AnimeSubsCustomizer.exe"))

    def test_packaged_build_rejects_a_python_shortcut(self):
        self.assertFalse(TIM._shortcut_points_here(
            target=r"C:\Py\pythonw.exe", args='"D:\\src\\x.pyw"',
            frozen=True, executable=r"C:\Apps\ASC\AnimeSubsCustomizer.exe",
            script=r"C:\Apps\ASC\AnimeSubsCustomizer.exe"))

    def test_source_run_needs_python_and_script(self):
        self.assertTrue(TIM._shortcut_points_here(
            target=r"C:\Py\pythonw.exe", args='"D:\\src\\x.pyw" --minimized',
            frozen=False, executable=r"C:\Py\pythonw.exe", script=r"D:\src\x.pyw"))
        self.assertFalse(TIM._shortcut_points_here(
            target=r"C:\Py\pythonw.exe", args='"D:\\other\\x.pyw"',
            frozen=False, executable=r"C:\Py\pythonw.exe", script=r"D:\src\x.pyw"))


class StartupApprovedTests(unittest.TestCase):
    """Task Manager / Settings > Startup apps store an on/off flag per shortcut
    in HKCU\\...\\StartupApproved\\StartupFolder: an odd first byte = disabled."""

    def test_missing_value_means_enabled(self):
        self.assertFalse(TIM._startup_entry_disabled(None))

    def test_enabled_flags(self):
        for first in (0x02, 0x06):
            self.assertFalse(TIM._startup_entry_disabled(bytes([first]) + b"\0" * 11))

    def test_disabled_flags(self):
        for first in (0x03, 0x07):
            self.assertTrue(TIM._startup_entry_disabled(bytes([first]) + b"\0" * 11))


class TokenIsolationTests(unittest.TestCase):
    def test_test_token_moves_the_startup_shortcut_into_the_data_folder(self):
        with patch.object(gui_app, "INSTANCE_TOKEN", "t1"), \
             patch.object(gui_app, "APPDATA_DIR", Path(r"C:\tmp\asc-instance-t1")):
            p = TIM._startup_shortcut_path()
        self.assertTrue(str(p).startswith(r"C:\tmp\asc-instance-t1"), p)
        self.assertTrue(str(p).endswith(".lnk"))


if __name__ == "__main__":
    unittest.main()
