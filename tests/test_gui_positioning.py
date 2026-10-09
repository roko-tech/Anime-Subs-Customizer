import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "anime_subs_customizer.pyw"


def _load_gui_module():
    loader = SourceFileLoader("anime_subs_customizer_gui_tests", str(MODULE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gui_app = _load_gui_module()


class CenterWindowTests(unittest.TestCase):
    def test_commits_requested_size_before_physical_centering(self):
        calls = []

        class FakeWindow:
            def geometry(self, value):
                calls.append(("geometry", value))

            def update_idletasks(self):
                calls.append(("update_idletasks",))

        with patch.object(
            gui_app.GUI,
            "_center_position",
            side_effect=lambda _win, width, height: calls.append(
                ("center_position", width, height)
            ),
        ):
            gui_app.GUI._center_window(FakeWindow(), 620, 470)

        self.assertEqual(
            calls,
            [
                ("geometry", "620x470"),
                ("update_idletasks",),
                ("center_position", 620, 470),
            ],
        )


class StudioDialogTests(unittest.TestCase):
    def test_stays_hidden_until_idle_centering_finishes(self):
        calls = []
        idle_callbacks = []

        class FakeDialog:
            def __init__(self):
                self._geometry = "200x200+0+0"
                self._state = "normal"

            def withdraw(self):
                calls.append(("withdraw",))
                self._state = "withdrawn"

            def title(self, value):
                calls.append(("title", value))

            def after(self, delay, callback):
                calls.append(("after", delay))

            def after_idle(self, callback):
                calls.append(("after_idle",))
                idle_callbacks.append(callback)

            def geometry(self, value=None):
                if value is not None:
                    calls.append(("geometry", value))
                    self._geometry = f"{value}+0+0"
                return self._geometry

            def update_idletasks(self):
                calls.append(("update_idletasks",))

            def transient(self, parent):
                calls.append(("transient", parent))

            def grab_set(self):
                calls.append(("grab_set",))

            def resizable(self, width, height):
                calls.append(("resizable", width, height))

            def winfo_exists(self):
                return True

            def state(self):
                return self._state

            def deiconify(self):
                calls.append(("deiconify",))
                self._state = "normal"

            def lift(self):
                calls.append(("lift",))

        dialog = FakeDialog()
        root = object()

        with (
            patch.object(gui_app.ctk, "CTkToplevel", return_value=dialog),
            patch.object(gui_app.app_state, "root", root),
            patch.object(
                gui_app.GUI,
                "_center_position",
                side_effect=lambda _win, width, height: calls.append(
                    ("center_position", width, height)
                ),
            ),
        ):
            result = gui_app.GUI._studio_dialog("Title", 620, 470)

            self.assertIs(result, dialog)
            self.assertEqual(dialog.state(), "withdrawn")
            self.assertEqual(len(idle_callbacks), 1)
            self.assertLess(
                calls.index(("withdraw",)),
                calls.index(("geometry", "620x470")),
            )
            self.assertNotIn(("deiconify",), calls)

            idle_callbacks[0]()

        self.assertEqual(dialog.state(), "normal")
        self.assertLess(
            calls.index(("center_position", 620, 470)),
            calls.index(("deiconify",)),
        )


class PreviewLangTests(unittest.TestCase):
    # A doubled @classmethod raises TypeError on Python 3.13+, which the
    # preview worker swallowed — Subtitle-line mode then never found a line.
    def test_arabic_only_font_previews_arabic_on_english_ui(self):
        with (
            patch.object(gui_app, "get_language", return_value="en"),
            patch.object(gui_app.FontSupport, "supports_text", return_value=True),
            patch.object(gui_app.FontSupport, "supports_latin", return_value=False),
        ):
            self.assertEqual(
                gui_app.PreviewBackground._preview_lang("Arabic Font"), "ar"
            )


class RtlLabelTests(unittest.TestCase):
    def tearDown(self):
        gui_app.set_language("en")

    def test_marks_tk_draws_from_a_fallback_font_end_up_on_the_right_side(self):
        # Tk lays fallback-font pieces out left to right in logical order, so
        # in Arabic a leading icon goes last and a trailing check goes first.
        gui_app.set_language("en")
        self.assertEqual(gui_app._with_mark("Shuffle", "⟳", lead=True), "⟳ Shuffle")
        self.assertEqual(gui_app._with_mark("eng", "✓", lead=False), "eng ✓")
        gui_app.set_language("ar")
        self.assertEqual(gui_app._with_mark("بدّل", "⟳", lead=True), "بدّل ⟳")
        self.assertEqual(gui_app._with_mark("eng", "✓", lead=False), "✓ eng")

    def test_issue_times_use_no_month_or_day_names(self):
        import time
        today = time.time()
        self.assertRegex(gui_app._issue_timestamp(today), r"^\d\d:\d\d$")
        self.assertRegex(gui_app._issue_timestamp(today - 3 * 86400),
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_checkbox_room_moves_to_the_text_side_in_rtl(self):
        gui_app.set_language("en")
        self.assertEqual(gui_app.CHECK_PAD(10), (10, 0))
        gui_app.set_language("ar")
        self.assertEqual(gui_app.CHECK_PAD(10), (7, 3))


if __name__ == "__main__":
    unittest.main()
