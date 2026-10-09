"""Compatibility façade for the historical flat `anime_subs_core` API.

The engine now lives in the ``anime_subs`` package (runtime / db / fonts / media
/ ass_editor / state / monitor). This module re-exports it so the GUI launcher
(`anime_subs_customizer.pyw`, which does ``from anime_subs_core import *``) and
the test suite keep importing a single namespace unchanged.

The top-level third-party/stdlib imports below are reproduced verbatim from the
old monolith so that ``from anime_subs_core import *`` still surfaces every name
the GUI relied on (tkinter, customtkinter, PIL, pystray, win32, …).
"""
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque, Counter
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import Optional, Set, Dict, List, Any, Callable, Tuple, Deque
from enum import Enum
from queue import Queue, Empty

try:
    import chardet
except ImportError:
    chardet = None

import tkinter as tk
import tkinter.font as tkFont
from tkinter import colorchooser, filedialog, messagebox

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import Icon, MenuItem
import customtkinter as ctk

if sys.platform.startswith("win"):
    import winshell
    from win32com.client import Dispatch
    import ctypes

# Expose the submodules (so tests can patch module-globals at their real home,
# e.g. anime_subs_core.db.DB_PATH) …
from anime_subs import (  # noqa: F401
    runtime, db, fonts, media, ass_editor, state, monitor, i18n, instance, updates,
)

# … and flatten the public API into this namespace for `import *`.
from anime_subs.runtime import *      # noqa: F401,F403
from anime_subs.db import *           # noqa: F401,F403
from anime_subs.fonts import *        # noqa: F401,F403
from anime_subs.media import *        # noqa: F401,F403
from anime_subs.ass_editor import *   # noqa: F401,F403
from anime_subs.state import *        # noqa: F401,F403
from anime_subs.monitor import *      # noqa: F401,F403
from anime_subs.i18n import *         # noqa: F401,F403
from anime_subs.instance import *     # noqa: F401,F403

# Underscore name the GUI imports explicitly (not surfaced by `import *`).
from anime_subs.fonts import _next_font_index  # noqa: F401
