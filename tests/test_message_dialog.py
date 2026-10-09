"""GUI.message defaults: show_info/show_warning/show_error pass no buttons and
must get a single OK button, not a TypeError (which the crash handler would
turn into a "Something went wrong" window instead of the message)."""
import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_gui():
    loader = SourceFileLoader("anime_subs_customizer_message", str(ROOT / "anime_subs_customizer.pyw"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gui = _load_gui()


class MessageDefaultsTests(unittest.TestCase):
    def setUp(self):
        # No Tk window: message() takes the native fallback, which is patched.
        patcher = patch.object(gui.app_state, "root", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_show_helpers_without_buttons(self):
        for helper, kind in ((gui.GUI.show_info, "info"),
                             (gui.GUI.show_warning, "warning"),
                             (gui.GUI.show_error, "error")):
            with self.subTest(kind=kind), \
                    patch.object(gui.GUI, "_native_message") as native:
                helper("Title", "Body")
                native.assert_called_once_with("Title", "Body", kind)

    def test_default_button_is_ok(self):
        with patch.object(gui.GUI, "_native_message"):
            self.assertEqual(gui.GUI.message("Title", "Body"), "ok")


if __name__ == "__main__":
    unittest.main()
