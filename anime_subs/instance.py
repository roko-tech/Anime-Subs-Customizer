"""Single-instance guard and the quit signal used by the installer (Windows).

The running app owns a named mutex; the installer's AppMutex checks the same name
before replacing files. `--quit` sets a named event the running instance waits
on, then waits until that instance has exited (the OS drops the mutex only when
the process is gone, so "mutex gone" means the files are free). Kernel objects
reach the running copy whatever its data folder is, unlike a flag file, and the
event is serviced by a plain thread, so a modal dialog can't block it.

A test instance token (ASC_INSTANCE_TOKEN) suffixes both names.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable, Optional

from .runtime import IS_WINDOWS, INSTANCE_TOKEN, instance_object_name, logger

__all__ = ["MUTEX_BASE", "QUIT_EVENT_BASE", "SingleInstance", "mutex_name",
           "is_running", "request_quit"]

MUTEX_BASE = "ASS-Subs-Changer-Singleton"
QUIT_EVENT_BASE = "ASS-Subs-Changer-Quit"

_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE-returning calls need a pointer-sized restype; the ctypes default
    # (c_int) would truncate a 64-bit handle.
    _k32.CreateMutexW.restype = wt.HANDLE
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    _k32.OpenMutexW.restype = wt.HANDLE
    _k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _k32.CreateEventW.restype = wt.HANDLE
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    _k32.OpenEventW.restype = wt.HANDLE
    _k32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _k32.SetEvent.argtypes = [wt.HANDLE]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.WaitForSingleObject.restype = wt.DWORD
    _k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]


def mutex_name(token: Optional[str] = None) -> str:
    return instance_object_name(MUTEX_BASE, INSTANCE_TOKEN if token is None else token)


def _quit_event_name(token: Optional[str] = None) -> str:
    return instance_object_name(QUIT_EVENT_BASE, INSTANCE_TOKEN if token is None else token)


class SingleInstance:
    """The running app's claim on its name, plus its quit-event listener."""

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = INSTANCE_TOKEN if token is None else token
        self._mutex: Optional[int] = None
        self._quit_event: Optional[int] = None

    def acquire(self) -> bool:
        """Claim the instance mutex. False when another instance already holds
        it. Also True when the mutex can't be created at all, so a Windows
        quirk never stops the app from starting."""
        if not IS_WINDOWS:
            return True
        handle = _k32.CreateMutexW(None, False, mutex_name(self.token))
        err = ctypes.get_last_error()
        if not handle:
            logger.error(f"Single-instance mutex could not be created (error {err})")
            return True
        if err == _ERROR_ALREADY_EXISTS:
            _k32.CloseHandle(handle)
            return False
        self._mutex = handle
        return True

    def release(self) -> None:
        """Give up the mutex (before a self-restart, so the new process can
        claim it). At normal exit the OS releases it with the process, which
        is what tells a waiting installer the files are free."""
        if self._mutex:
            _k32.CloseHandle(self._mutex)
            self._mutex = None

    def listen_for_quit(self, on_quit: Callable[[], None]) -> None:
        """Run `on_quit` (on a background thread) when another process calls
        request_quit(). on_quit must marshal any UI work itself."""
        if not IS_WINDOWS or self._quit_event:
            return
        handle = _k32.CreateEventW(None, True, False, _quit_event_name(self.token))
        if not handle:
            logger.error(f"Quit event could not be created (error {ctypes.get_last_error()})")
            return
        self._quit_event = handle

        def _wait() -> None:
            if _k32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
                logger.info("Quit requested by another process (installer/uninstaller)")
                on_quit()

        threading.Thread(target=_wait, name="quit-listener", daemon=True).start()


def is_running(token: Optional[str] = None) -> bool:
    """True while some process holds the instance mutex."""
    if not IS_WINDOWS:
        return False
    handle = _k32.OpenMutexW(_SYNCHRONIZE, False, mutex_name(token))
    if handle:
        _k32.CloseHandle(handle)
        return True
    return False


def request_quit(token: Optional[str] = None, timeout: float = 15.0) -> int:
    """Ask the running instance to quit and wait for it to exit.

    Returns 0 when no instance is running (any more), 1 when one is still
    running after `timeout` seconds (or can't be signalled — e.g. a version
    from before --quit existed)."""
    if not is_running(token):
        return 0
    event = _k32.OpenEventW(_EVENT_MODIFY_STATE, False, _quit_event_name(token))
    if event:
        _k32.SetEvent(event)
        _k32.CloseHandle(event)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(token):
            return 0
        time.sleep(0.1)
    return 1
