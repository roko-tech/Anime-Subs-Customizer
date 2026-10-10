"""First-watch confirmation (ADR-006 §4): consent is per folder, configs from
earlier releases are grandfathered (no nag after an upgrade), and obviously
wrong folders (a whole drive, the user profile, Windows) are flagged."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import runtime  # noqa: E402


def _load_gui():
    loader = SourceFileLoader("anime_subs_customizer_first_watch", str(ROOT / "anime_subs_customizer.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gui = _load_gui()


class ConsentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        self.lib = str(Path(self.tmp.name) / "Anime")

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, data):
        self.cfg.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(runtime, "CONFIG_FILE_PATH", self.cfg):
            return runtime.AppConfig.load()

    def test_a_watched_folder_from_an_earlier_release_is_grandfathered(self):
        cfg = self._load({"folder": self.lib, "font": "Arial", "color": "00FFFF"})
        self.assertIn(runtime.folder_key(self.lib), cfg.consented_folders)
        self.assertEqual(cfg.schema_version, 1)

    def test_a_new_config_must_still_ask(self):
        cfg = self._load({"folder": self.lib, "font": "Arial", "color": "00FFFF",
                          "schema_version": 1})
        self.assertEqual(cfg.consented_folders, [])

    def test_an_old_config_that_never_watched_is_not_grandfathered(self):
        cfg = self._load({"folder": self.lib})
        self.assertEqual(cfg.consented_folders, [])

    def test_garbage_consent_list_is_ignored(self):
        cfg = self._load({"consented_folders": "C:\\", "schema_version": 1})
        self.assertEqual(cfg.consented_folders, [])

    def test_consent_survives_a_save_from_the_ui_snapshot(self):
        st = gui.app_state.__class__()
        st.config = runtime.AppConfig(consented_folders=["x"], favorite_fonts=["A"])
        snap = st.snapshot_config(self.lib)
        self.assertEqual(snap.consented_folders, ["x"])
        self.assertEqual(snap.favorite_fonts, ["A"])

    def test_folder_keys_ignore_case_and_slashes(self):
        self.assertEqual(runtime.folder_key("D:/Library/"), runtime.folder_key("d:\\library"))


class RiskyFolderTests(unittest.TestCase):
    def test_drive_root_and_profile_and_windows_are_risky(self):
        self.assertTrue(gui.GUI._risky_folder("C:\\"))
        self.assertTrue(gui.GUI._risky_folder(str(Path.home())))
        if os.environ.get("WINDIR"):
            self.assertTrue(gui.GUI._risky_folder(os.path.join(os.environ["WINDIR"], "Fonts")))

    def test_an_anime_folder_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "Anime" / "Some Show"
            lib.mkdir(parents=True)
            # The temp dir lives under LOCALAPPDATA on Windows; judge a folder
            # outside every system location instead.
            with patch.dict(os.environ, {"LOCALAPPDATA": "Z:\\nowhere", "APPDATA": "Z:\\nowhere2"}):
                self.assertFalse(gui.GUI._risky_folder(str(lib)))


class FolderPickerTests(unittest.TestCase):
    """Change… and the welcome button used an untitled picker, so the Arabic
    UI showed Windows' own English caption (flagged in the Arabic review)."""

    def _pick(self, current):
        st = gui.app_state
        saved = (st.operation_in_progress, st.monitoring_flag, st.monitored_folder)
        st.operation_in_progress, st.monitoring_flag, st.monitored_folder = False, False, current
        try:
            with patch.object(gui.filedialog, "askdirectory", return_value="") as ask:
                gui.GUI.select_folder()
        finally:
            st.operation_in_progress, st.monitoring_flag, st.monitored_folder = saved
        return ask.call_args.kwargs

    def test_the_picker_has_the_apps_own_title(self):
        self.assertEqual(self._pick(None)["title"], gui.t_raw("pick_folder_title"))

    def test_it_opens_at_the_current_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._pick(d)["initialdir"], d)


if __name__ == "__main__":
    unittest.main()
