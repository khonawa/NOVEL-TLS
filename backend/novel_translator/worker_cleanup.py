"""Bound hung Playwright close calls without calling its API from another thread.

The driver process is owned by this worker. The private transport access is
isolated here because Playwright has no public close timeout. A Windows process
handle pins browser identity, so PID recycling cannot target another browser.
"""
from __future__ import annotations

import ctypes
import os
import threading


CLOSE_GRACE_SECONDS = 3.0


class BrowserCleanup:
    def __init__(self, playwright, cancel):
        self.cancel = cancel
        self.closing = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.handles = []
        self.driver = getattr(getattr(getattr(playwright, "_impl_obj", None), "_connection", None), "_transport", None)
        self.driver = getattr(self.driver, "_proc", None)
        self.kernel = None
        if os.name == "nt":
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.thread = threading.Thread(target=self._watch, name="browser-cleanup", daemon=True)
        self.thread.start()

    def track_browser(self, pid):
        if self.kernel is not None and type(pid) is int and pid > 0:
            handle = self.kernel.OpenProcess(0x0001, False, pid)
            if handle:
                with self.lock:
                    self.handles.append(handle)

    def _watch(self):
        try:
            while not self.done.wait(0.1):
                if self.closing.is_set() or self.cancel.is_set():
                    if not self.done.wait(CLOSE_GRACE_SECONDS):
                        self._terminate()
                    return
        finally:
            with self.lock:
                for handle in self.handles:
                    self.kernel.CloseHandle(handle)
                self.handles.clear()

    def _terminate(self):
        with self.lock:
            for handle in self.handles:
                self.kernel.TerminateProcess(handle, 1)
        if self.driver is not None:
            try:
                # asyncio Process delegates to the owned subprocess handle.
                self.driver.terminate()
            except (OSError, ProcessLookupError):
                pass

    def finish(self):
        self.done.set()
        self.thread.join(timeout=1)
