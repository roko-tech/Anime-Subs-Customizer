"""Read-only library check: would the current settings restyle anything?

`--check-styled <folder>` runs this before an upgrade or a data move. It makes the
exact decision the monitor makes for every .ass file — already styled, left alone
because the font can't render it, or would be rewritten — without writing
anything, so a build whose styling math differs (missing shaper, unresolved font,
other size table) shows up as "would restyle" instead of as a restyle wave.
"""
from pathlib import Path
from typing import Any, Dict, List

from .runtime import APP_VERSION, AppConfig, is_frozen
from .ass_editor import FileProcessor
from .fonts import FontSupport

__all__ = ["check_styled"]


def check_styled(folder: str, cfg: AppConfig, limit: int = 200) -> Dict[str, Any]:
    """Classify every .ass under `folder` for `cfg`; lists are capped at `limit`
    names each (counts are always complete)."""
    font = (cfg.font or "").strip()
    nudge = cfg.font_size_nudges.get(font, 1.0)
    color = cfg.color or ""
    counts = {"total": 0, "already_styled": 0, "no_dialogue": 0, "font_skip": 0,
              "would_restyle": 0, "error": 0}
    lists: Dict[str, List[str]] = {"would_restyle": [], "error": []}

    def note(kind: str, path: Path) -> None:
        counts[kind] += 1
        if kind in lists and len(lists[kind]) < limit:
            lists[kind].append(str(path))

    for path in FileProcessor.list_ass_files(folder):
        counts["total"] += 1
        try:
            if FileProcessor.is_already_styled(
                    path, font, color, bold=cfg.font_bold, italic=cfg.font_italic,
                    underline=cfg.font_underline, size=cfg.font_size, nudge=nudge):
                note("already_styled", path)
                continue
            computed = FileProcessor.compute_changes(
                path, font, color, bold=cfg.font_bold, italic=cfg.font_italic,
                underline=cfg.font_underline, size=cfg.font_size, nudge=nudge)
            if computed is None:
                note("error", path)
            elif FileProcessor.dialogue_signature(computed[1], computed[2])["taggable"] == 0:
                # Signs/songs only: Watch and Apply leave these alone too.
                note("no_dialogue", path)
            elif FileProcessor._font_cant_render_file(
                    font, computed[1], path, skip_styles=computed[2]):
                note("font_skip", path)
            else:
                note("would_restyle", path)
        except Exception:  # noqa: BLE001 — a check must report, not crash
            note("error", path)

    return {
        "version": APP_VERSION,
        "frozen": is_frozen(),
        "folder": folder,
        "font": font,
        "color": cfg.color,
        "size": cfg.font_size,
        "nudge": nudge,
        "size_ratio_arabic": FontSupport.size_ratio(font, "arabic") if font else None,
        "size_ratio_latin": FontSupport.size_ratio(font, "latin") if font else None,
        "counts": counts,
        **lists,
    }
