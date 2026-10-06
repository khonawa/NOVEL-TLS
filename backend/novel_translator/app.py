from __future__ import annotations

import logging
import os
import platform
import queue
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # tkinterdnd2 not installed / not bundled: drag-and-drop disabled
    DND_FILES = None
    TkinterDnD = None

# Drag-and-drop needs the root window to be a TkinterDnD drag-source/target, not
# a plain CTk. Subclassing CTk first and mixing in DnDWrapper (then calling
# TkinterDnD._require in __init__) is the documented customtkinter pattern.
# When tkinterdnd2 is unavailable we fall back to a plain CTk so the app still
# runs with the Browse button alone.
_AppBase = ctk.CTk if TkinterDnD is None else type(
    "_AppBase", (ctk.CTk, TkinterDnD.DnDWrapper), {}
)

from .constants import (
    APP_NAME,
    CHUNK_PRESETS,
    COLORS,
    DEFAULT_CHUNK_PRESET,
    REQUIRED_SWEEPS_MAX,
    REQUIRED_SWEEPS_MIN,
    SCROLL_INTERVAL_MAX,
    SCROLL_INTERVAL_MIN,
    SETTINGS_SAVE_DEBOUNCE_MS,
    UiEvent,
    resource_path,
    sanitize_filename,
)
from .pipeline import PipelineContext, QueueHandler, TranslationRunner
from .settings import format_duration, load_settings, save_settings


class TranslatorApp(_AppBase):
    def __init__(self) -> None:
        super().__init__()
        # Load the tkdnd package into this interpreter (no-op base when
        # tkinterdnd2 is missing, since _AppBase is then just CTk).
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                # OLE2 init can fail in odd environments; DnD just won't work.
                self.TkdndVersion = None
        settings = load_settings()
        self.title(APP_NAME)
        # Chrome-like flexibility: fully resizable in both directions, with a
        # small floor so the layout never collapses, and no upper bound so it
        # can be snapped to a half-screen or maximized like a browser window.
        self.resizable(True, True)
        saved_geometry = str(settings["geometry"])
        size_match = re.match(r"(\d+)x(\d+)", saved_geometry)
        if size_match:
            width, height = map(int, size_match.groups())
            target_geometry = f"{max(640, width)}x{max(520, height)}"
        else:
            target_geometry = "1120x820"
        self.geometry(target_geometry)
        self.minsize(640, 520)
        # CTk re-applies its own default geometry/minsize from a deferred
        # after(0) DPI callback, clobbering what we set above. Re-assert our
        # values once the event loop starts so the restored size sticks.
        self.after_idle(lambda g=target_geometry: (self.geometry(g), self.minsize(640, 520)))
        self.configure(fg_color=COLORS["window"])
        ctk.set_appearance_mode("dark")
        icon_path = resource_path("assets/novel_translator.ico")
        if icon_path.is_file():
            self.iconbitmap(str(icon_path))

        self.input_var = ctk.StringVar()
        self.output_var = ctk.StringVar()
        self.status_var = ctk.StringVar(value="Ready")
        self.timing_var = ctk.StringVar(value="Elapsed 0:00 | ETA --")
        self.summary_var = ctk.StringVar(value="No chunks prepared")
        self.stats_var = ctk.StringVar(value="")
        # Live-tunable settings are read by the worker thread while the GUI
        # writes them, so guard both with a lock (#2 thread-safety).
        self._settings_lock = threading.Lock()
        self.scroll_interval = float(settings["scroll_interval"])
        self.required_sweeps = int(settings["required_sweeps"])
        self.chunk_preset = str(settings["chunk_preset"])
        self.input_dir = str(settings["input_dir"])
        self.output_dir = str(settings["output_dir"])
        self.chunk_statuses: list[str] = []
        # Render cache for _render_chunk_statuses (#24): the row count and the
        # per-row statuses last drawn, so updates patch only changed lines.
        self._rendered_chunk_count: int | None = None
        self._rendered_statuses: list[str] = []
        self.run_started_at = 0.0
        self.run_finished_at = 0.0
        self.completed_count = 0
        self.total_count = 0
        self.failed_count = 0
        # F3 metrics: rolling window of (chars, seconds) per completed chunk,
        # plus live per-chunk translation percentage from the drop-rate events.
        self.chunk_durations: deque[tuple[float, float]] = deque(maxlen=10)
        self.total_translated_chars = 0
        self.current_chunk_index = 0
        self.current_chunk_translated_pct = 0.0
        self.current_chunk_failed = False
        self.last_input_path: Path | None = None
        self.last_output_path: Path | None = None
        # True while the output box still holds the auto-derived
        # "<input>.english.epub" path. Picking a custom output flips this to
        # False so a later input change won't clobber the user's choice; while
        # it's True, changing the input re-derives the output so the previous
        # novel's name never carries into the next job.
        self._output_auto = True
        self.events: queue.Queue[UiEvent] = queue.Queue()
        self.cancel_event = threading.Event()
        # Pause/resume: set = running, cleared = paused. Starts set (F2).
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.worker: threading.Thread | None = None
        # Debounce handle for settings persistence (#20); see _schedule_save_settings.
        self._settings_save_after: str | None = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)
        self.after(1000, self._refresh_timing)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=26, pady=(22, 16), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        brand_mark = ctk.CTkFrame(
            header, width=58, height=58, corner_radius=12,
            fg_color=COLORS["surface_alt"], border_width=1, border_color=COLORS["border"],
        )
        brand_mark.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        brand_mark.grid_propagate(False)
        ctk.CTkLabel(
            brand_mark, text="NT", text_color=COLORS["accent"],
            font=ctk.CTkFont(family="Georgia", size=20, weight="bold"),
        ).place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(
            header, text="Novel Translator", text_color=COLORS["text"],
            font=ctk.CTkFont(family="Georgia", size=26, weight="bold"),
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            header, text="CHINESE  >  ENGLISH EPUB", text_color=COLORS["accent"],
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
        ).grid(row=1, column=1, pady=(1, 0), sticky="nw")
        self.status_badge = ctk.CTkLabel(
            header, textvariable=self.status_var, width=104, height=32, corner_radius=16,
            fg_color="#174D3B", text_color=COLORS["warm"],
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        self.status_badge.grid(row=0, column=2, rowspan=2, padx=(16, 0), sticky="e")

        file_frame = ctk.CTkFrame(
            self, fg_color=COLORS["surface"], border_width=1,
            border_color=COLORS["accent_hover"], corner_radius=8,
        )
        file_frame.grid(row=1, column=0, padx=26, pady=(0, 14), sticky="ew")
        file_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            file_frame, text="File", text_color=COLORS["text"],
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=15, pady=(13, 7), sticky="w")
        ctk.CTkLabel(file_frame, text="Input File (.txt)", text_color=COLORS["text"]).grid(
            row=1, column=0, padx=(15, 14), pady=5, sticky="w"
        )
        self.input_entry = ctk.CTkEntry(
            file_frame, textvariable=self.input_var, placeholder_text="Chinese .txt input file",
            height=34, corner_radius=6, fg_color=COLORS["input"], border_color=COLORS["border"],
        )
        self.input_entry.grid(row=1, column=1, padx=(0, 8), pady=5, sticky="ew")
        ctk.CTkButton(
            file_frame, text="Browse", width=82, height=34, corner_radius=6,
            fg_color=COLORS["surface_alt"], hover_color=COLORS["border"], command=self._choose_input
        ).grid(row=1, column=2, padx=(0, 15))
        ctk.CTkLabel(file_frame, text="Output File (.epub)", text_color=COLORS["text"]).grid(
            row=2, column=0, padx=(15, 14), pady=(5, 14), sticky="w"
        )
        ctk.CTkEntry(
            file_frame, textvariable=self.output_var, placeholder_text="English .epub output file",
            height=34, corner_radius=6, fg_color=COLORS["input"], border_color=COLORS["border"],
        ).grid(row=2, column=1, padx=(0, 8), pady=(5, 14), sticky="ew")
        ctk.CTkButton(
            file_frame, text="Browse", width=82, height=34, corner_radius=6,
            fg_color=COLORS["surface_alt"], hover_color=COLORS["border"], command=self._choose_output
        ).grid(row=2, column=2, padx=(0, 15), pady=(5, 14))

        settings_frame = ctk.CTkFrame(
            self, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"], corner_radius=8
        )
        settings_frame.grid(row=2, column=0, padx=26, pady=(0, 14), sticky="ew")
        settings_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            settings_frame, text="Translation Settings", text_color=COLORS["text"],
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=5, padx=15, pady=(13, 8), sticky="w")

        speed_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        speed_frame.grid(row=1, column=0, padx=(15, 22), pady=(0, 14), sticky="ew")
        speed_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(speed_frame, text="Scroll Speed", text_color=COLORS["text"]).grid(row=0, column=0, sticky="w")
        self.speed_value = ctk.CTkLabel(
            speed_frame, text=f"{self.scroll_interval:.2f} sec", text_color=COLORS["warm"],
        )
        self.speed_value.grid(row=0, column=1, sticky="e")
        self.speed_control = ctk.CTkSlider(
            speed_frame, from_=SCROLL_INTERVAL_MIN, to=SCROLL_INTERVAL_MAX,
            number_of_steps=99, command=self._set_speed,
            height=16, button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent"], fg_color=COLORS["border"],
        )
        self.speed_control.set(self.scroll_interval)
        self.speed_control.grid(row=1, column=0, columnspan=2, pady=(8, 0), sticky="ew")

        ctk.CTkLabel(settings_frame, text="Sweeps", text_color=COLORS["text"]).grid(row=1, column=1, padx=(0, 8), sticky="nw")
        ctk.CTkButton(
            settings_frame, text="-", width=30, height=30, corner_radius=5,
            fg_color=COLORS["surface_alt"], hover_color=COLORS["border"],
            command=lambda: self._change_sweeps(-1),
        ).grid(row=1, column=2, pady=(27, 0))
        self.sweeps_value = ctk.CTkLabel(
            settings_frame, text=str(self.required_sweeps), width=36, height=30,
            fg_color=COLORS["input"], text_color=COLORS["text"],
        )
        self.sweeps_value.grid(row=1, column=3, pady=(27, 0))
        ctk.CTkButton(
            settings_frame, text="+", width=30, height=30, corner_radius=5,
            fg_color=COLORS["surface_alt"], hover_color=COLORS["border"],
            command=lambda: self._change_sweeps(1),
        ).grid(row=1, column=4, padx=(0, 18), pady=(27, 0))

        chunk_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        chunk_frame.grid(row=1, column=5, padx=(0, 15), pady=(0, 14), sticky="n")
        ctk.CTkLabel(chunk_frame, text="Chunk Size", text_color=COLORS["text"]).grid(row=0, column=0, sticky="w")
        self.chunk_menu = ctk.CTkOptionMenu(
            chunk_frame, values=list(CHUNK_PRESETS), command=self._set_chunk_preset,
            width=165, height=32, corner_radius=6, fg_color=COLORS["surface_alt"],
            button_color=COLORS["surface_alt"], button_hover_color=COLORS["border"],
        )
        if self.chunk_preset not in CHUNK_PRESETS:
            self.chunk_preset = DEFAULT_CHUNK_PRESET
        self.chunk_menu.set(self.chunk_preset)
        self.chunk_menu.grid(row=1, column=0, pady=(8, 0))

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=3, column=0, padx=26, pady=(0, 14), sticky="ew")
        action_frame.grid_columnconfigure(4, weight=1)
        self.start_button = ctk.CTkButton(
            action_frame, text="Start Translation", width=174, height=40, corner_radius=7,
            text_color=COLORS["window"], font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self._start,
        )
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ctk.CTkButton(
            action_frame, text="Cancel", width=92, height=40, corner_radius=7,
            fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
            command=self._cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1)
        self.pause_button = ctk.CTkButton(
            action_frame, text="Pause", width=92, height=40, corner_radius=7,
            fg_color=COLORS["input"], border_width=1, border_color=COLORS["border"],
            hover_color=COLORS["surface_alt"], command=self._toggle_pause, state="disabled",
        )
        self.pause_button.grid(row=0, column=2, padx=(8, 0))
        self.retry_button = ctk.CTkButton(
            action_frame, text="Retry Failed", width=110, height=40, corner_radius=7,
            fg_color=COLORS["input"], border_width=1, border_color=COLORS["border"],
            hover_color=COLORS["surface_alt"], command=self._retry_failed, state="disabled",
        )
        self.retry_button.grid(row=0, column=3, padx=(8, 0))
        self.diagnostics_button = ctk.CTkButton(
            action_frame, text="Export Diagnostics", width=145, height=40, corner_radius=7,
            fg_color=COLORS["input"], border_width=1, border_color=COLORS["border"],
            hover_color=COLORS["surface_alt"], command=self._export_diagnostics,
        )
        self.diagnostics_button.grid(row=0, column=5, padx=(8, 0), sticky="e")
        self.open_book_button = ctk.CTkButton(
            action_frame, text="Open Book", width=104, height=40, corner_radius=7,
            fg_color=COLORS["input"], border_width=1, border_color=COLORS["accent"],
            hover_color=COLORS["surface_alt"], command=self._open_book, state="disabled",
        )
        self.open_book_button.grid(row=0, column=6, padx=(8, 0), sticky="e")
        self.open_folder_button = ctk.CTkButton(
            action_frame, text="Open Folder", width=110, height=40, corner_radius=7,
            fg_color=COLORS["input"], border_width=1, border_color=COLORS["accent"],
            hover_color=COLORS["surface_alt"], command=self._open_folder, state="disabled",
        )
        self.open_folder_button.grid(row=0, column=7, padx=(8, 0), sticky="e")

        progress_frame = ctk.CTkFrame(
            self, fg_color=COLORS["surface"], border_width=1,
            border_color=COLORS["border"], corner_radius=8,
        )
        progress_frame.grid(row=4, column=0, padx=26, pady=(0, 14), sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            progress_frame, text="Progress", text_color=COLORS["text"],
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=15, pady=(12, 5), sticky="w")
        self.progress = ctk.CTkProgressBar(
            progress_frame, height=10, corner_radius=5,
            progress_color=COLORS["accent"], fg_color=COLORS["border"],
        )
        self.progress.grid(row=1, column=0, columnspan=2, padx=15, pady=(4, 5), sticky="ew")
        self.progress.set(0)
        progress_info = ctk.CTkFrame(progress_frame, fg_color="transparent")
        progress_info.grid(row=2, column=0, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        progress_info.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(progress_info, textvariable=self.summary_var, text_color=COLORS["accent"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(progress_info, textvariable=self.timing_var, text_color=COLORS["muted"]).grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(progress_info, textvariable=self.stats_var, text_color=COLORS["muted"]).grid(row=1, column=0, columnspan=2, pady=(2, 0), sticky="w")

        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.grid(row=5, column=0, padx=26, pady=(0, 24), sticky="nsew")
        content_frame.grid_columnconfigure(1, weight=1)
        content_frame.grid_rowconfigure(0, weight=1)
        self.chunk_list = ctk.CTkTextbox(
            content_frame, width=220, state="disabled", corner_radius=8,
            border_width=1, border_color=COLORS["border"], fg_color=COLORS["surface"],
            text_color=COLORS["muted"], font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.chunk_list.insert("1.0", "CHUNKS\n\nNo chunks prepared")
        self.chunk_list.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        self.log = ctk.CTkTextbox(
            content_frame, state="disabled", corner_radius=8, border_width=1,
            border_color=COLORS["border"], fg_color="#07100C", text_color=COLORS["warm"],
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log.insert("1.0", "ACTIVITY LOG\n\nReady for a Chinese TXT novel.")
        self.log.grid(row=0, column=1, sticky="nsew")
        self.log._textbox.tag_configure("warning", foreground="#FF7B72")
        self._setup_drag_and_drop()

    def _setup_drag_and_drop(self) -> None:
        """Register .txt drag-and-drop on the window and input entry (F4).

        No-ops when tkinterdnd2 is unavailable (e.g. missing from a PyInstaller
        build), so the app still works with the Browse button alone.
        """
        if DND_FILES is None or TkinterDnD is None:
            return
        # tkdnd was loaded in __init__ via TkinterDnD._require; if that failed
        # (TkdndVersion is None) there's nothing to register against.
        if not getattr(self, "TkdndVersion", None):
            return
        try:
            for widget in (self, self.input_entry):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            self.input_var.set("Drop a .txt novel here or use Browse")
        except Exception:
            pass  # Drag-and-drop is a convenience; never break the app over it.

    def _on_drop(self, event: object) -> None:
        raw = getattr(event, "data", "") or ""
        try:
            paths = [Path(p) for p in self.tk.splitlist(raw) if p]
        except Exception:
            paths = [Path(raw.strip('{}"'))] if raw.strip() else []
        txt_files = [p for p in paths if p.suffix.lower() == ".txt"]
        if txt_files:
            self._set_input_path(txt_files[0])
            self.input_entry.configure(border_color=COLORS["border"])
        else:
            self._flash_input_invalid()

    def _flash_input_invalid(self) -> None:
        self.input_entry.configure(border_color=COLORS["danger_hover"])
        self.after(900, lambda: self.input_entry.configure(border_color=COLORS["border"]))

    def _set_input_path(self, path: Path) -> None:
        self.input_var.set(str(path))
        self.input_dir = str(path.parent)
        # Re-derive the default output whenever the user hasn't explicitly chosen
        # one, so the previous novel's title doesn't carry into the next job.
        if self._output_auto or not self.output_var.get():
            # Sanitize the derived name: novel titles can contain characters
            # Windows forbids in filenames (":", "?", ...), which would make
            # the output/checkpoint path invalid and crash file operations.
            safe_stem = sanitize_filename(path.stem)
            derived = path.with_name(f"{safe_stem}.english.epub")
            self.output_var.set(str(derived))
            self._output_auto = True
        self._disarm_retry_if_stale()
        self._schedule_save_settings()

    def _disarm_retry_if_stale(self) -> None:
        """Disarm Retry if the path fields no longer match the snapshot (#3)."""
        if self.last_input_path is None or self.last_output_path is None:
            return
        if (
            Path(self.input_var.get().strip()) != self.last_input_path
            or Path(self.output_var.get().strip()) != self.last_output_path
        ):
            self.retry_button.configure(state="disabled")

    def _choose_input(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select novel", initialdir=self.input_dir or None, filetypes=[("Text files", "*.txt")]
        )
        if selected:
            self._set_input_path(Path(selected))

    def _set_speed(self, value: float) -> None:
        with self._settings_lock:
            self.scroll_interval = round(float(value), 2)
            current = self.scroll_interval
        self.speed_value.configure(text=f"{current:.2f} sec")
        self._schedule_save_settings()

    def _change_sweeps(self, amount: int) -> None:
        with self._settings_lock:
            self.required_sweeps = max(
                REQUIRED_SWEEPS_MIN, min(REQUIRED_SWEEPS_MAX, self.required_sweeps + amount)
            )
            current = self.required_sweeps
        self.sweeps_value.configure(text=str(current))
        self._schedule_save_settings()

    def _get_scroll_interval(self) -> float:
        with self._settings_lock:
            return self.scroll_interval

    def _get_required_sweeps(self) -> int:
        with self._settings_lock:
            return self.required_sweeps

    def _set_chunk_preset(self, value: str) -> None:
        self.chunk_preset = value
        self._schedule_save_settings()

    def _choose_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Save translated EPUB", initialdir=self.output_dir or None,
            defaultextension=".epub", filetypes=[("EPUB books", "*.epub")]
        )
        if selected:
            self.output_var.set(selected)
            self.output_dir = str(Path(selected).parent)
            # The user chose an explicit output: stop auto-deriving it from the
            # input so their custom name is respected on later input changes.
            self._output_auto = False
            self._disarm_retry_if_stale()
            self._schedule_save_settings()

    def _start(self) -> None:
        input_path = Path(self.input_var.get().strip())
        output_path = Path(self.output_var.get().strip())
        if not input_path.is_file() or input_path.suffix.lower() != ".txt":
            messagebox.showerror(APP_NAME, "Select a valid .txt input file.")
            return
        if output_path.suffix.lower() != ".epub":
            messagebox.showerror(APP_NAME, "Choose an output path ending in .epub.")
            return
        if input_path.resolve() == output_path.resolve():
            messagebox.showerror(APP_NAME, "The output EPUB must be different from the input file.")
            return
        # Self-heal: whatever path is in the box (typed, browsed, or restored
        # from settings), strip characters Windows forbids so the run can't
        # fail with WinError 267. Reflect the cleaned name back so the user
        # sees the actual file that will be written.
        safe_output = output_path.with_name(
            f"{sanitize_filename(output_path.stem)}{output_path.suffix}"
        )
        if safe_output != output_path:
            output_path = safe_output
            self.output_var.set(str(output_path))
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            probe = output_path.parent / f".{output_path.name}.write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"The output folder is not writable:\n{exc}")
            return
        self.cancel_event.clear()
        self.pause_event.set()
        self.pause_button.configure(text="Pause")
        self.progress.set(0)
        self.run_started_at = time.monotonic()
        self.run_finished_at = 0.0
        self.chunk_durations.clear()
        self.total_translated_chars = 0
        self.current_chunk_index = 0
        self.current_chunk_translated_pct = 0.0
        self.current_chunk_failed = False
        # Force a clean rebuild of the chunk list on the first status event of
        # this run (#24), so a prior run's rows can't linger.
        self._rendered_chunk_count = None
        self._rendered_statuses = []
        self.stats_var.set("")
        # Snapshot the validated paths for this run; Retry Failed re-uses them
        # and stays armed only while the visible fields still match (#3).
        self.last_input_path = input_path
        self.last_output_path = output_path
        # Keep the visible output field in sync with the (possibly sanitized)
        # path so Retry/Open-book target the real file.
        self.output_var.set(str(output_path))
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.pause_button.configure(state="normal")
        self.retry_button.configure(state="disabled")
        self.open_book_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")
        self.status_badge.configure(fg_color=COLORS["warm"], text_color=COLORS["window"])
        self.worker = threading.Thread(
            target=self._translate_worker,
            args=(input_path, output_path), daemon=True
        )
        self.worker.start()

    def _retry_failed(self) -> None:
        if self.last_input_path is None or self.last_output_path is None:
            return
        # Guard against silently retrying a different file than the one that
        # produced the failed run (#3). _start() re-reads the (matching) fields.
        if (
            Path(self.input_var.get().strip()) != self.last_input_path
            or Path(self.output_var.get().strip()) != self.last_output_path
        ):
            self.retry_button.configure(state="disabled")
            return
        self._start()

    def _open_book(self) -> None:
        if self.last_output_path is not None and self.last_output_path.is_file():
            # os.startfile is Windows-only, which is fine: the app already
            # requires Windows for pywinauto UI automation.
            os.startfile(str(self.last_output_path))  # noqa: S606

    def _open_folder(self) -> None:
        if self.last_output_path is not None and self.last_output_path.is_file():
            # /select highlights the file itself inside Explorer.
            subprocess.run(["explorer", "/select,", str(self.last_output_path)], check=False)

    def _export_diagnostics(self) -> None:
        destination = filedialog.asksaveasfilename(
            title="Export diagnostics", initialdir=self.output_dir or None,
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
        )
        if not destination:
            return
        lines = [
            f"Application: {APP_NAME}",
            f"Python: {platform.python_version()}",
            f"OS: {platform.platform()}",
            f"Architecture: {platform.machine()}",
            f"Input file selected: {bool(self.input_var.get().strip())}",
            f"Output file selected: {bool(self.output_var.get().strip())}",
            f"Chunk preset: {self.chunk_preset}",
            f"Scroll interval: {self.scroll_interval:.2f} seconds",
            f"Required sweeps: {self.required_sweeps}",
            f"Chunk statuses: {', '.join(self.chunk_statuses) or 'none'}",
            "",
            "Log:",
            self.log.get("1.0", "end").strip(),
        ]
        Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
        messagebox.showinfo(APP_NAME, f"Diagnostics saved to:\n{destination}")

    def _toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text="Resume")
            self.status_var.set("Paused")
        else:
            self.pause_event.set()
            self.pause_button.configure(text="Pause")
            if self.current_chunk_index:
                self.status_var.set(f"Translating {self.current_chunk_index}")

    def _cancel(self) -> None:
        # Resume first so a worker blocked in _pace() can observe the cancel.
        self.pause_event.set()
        self.cancel_event.set()
        self.status_var.set("Cancelling...")
        self.cancel_button.configure(state="disabled")
        self.pause_button.configure(state="disabled", text="Pause")

    def _emit(self, kind: str, value: object) -> None:
        self.events.put(UiEvent(kind, value))

    def _render_chunk_statuses(self) -> None:
        """Update the chunk list in place (#24).

        The first render (or any run whose chunk count changes) builds the rows
        once; subsequent status updates only rewrite the single line whose status
        actually changed, instead of deleting and reinserting the whole widget.
        """
        markers = {
            "Complete": "[x]",
            "Translating": "[~]",
            "Pending": "[ ]",
            "Failed": "[!]",
        }
        # Statuses occupy lines 3..N (after the "CHUNKS" header + blank line).
        first_row = 3
        self.chunk_list.configure(state="normal")
        try:
            # Rebuild fully when the row count changes (new run / re-render).
            if getattr(self, "_rendered_chunk_count", None) != len(self.chunk_statuses):
                self.chunk_list.delete("1.0", "end")
                self.chunk_list.insert("end", "CHUNKS\n\n")
                for index, status in enumerate(self.chunk_statuses, start=1):
                    marker = markers.get(status, "[ ]")
                    self.chunk_list.insert("end", f"{marker}  Chunk {index:<5}{status}\n")
                self._rendered_chunk_count = len(self.chunk_statuses)
                self._rendered_statuses = list(self.chunk_statuses)
                return

            # Same number of rows: patch only the lines that changed.
            previous = getattr(self, "_rendered_statuses", [])
            for offset, (old, new) in enumerate(zip(previous, self.chunk_statuses)):
                if old == new:
                    continue
                line = first_row + offset
                marker = markers.get(new, "[ ]")
                self.chunk_list.delete(f"{line}.0", f"{line}.end")
                self.chunk_list.insert(f"{line}.0", f"{marker}  Chunk {offset + 1:<5}{new}")
            self._rendered_statuses = list(self.chunk_statuses)
        finally:
            self.chunk_list.configure(state="disabled")

    def _average_rate(self) -> float | None:
        """Rolling chars/sec over the most recent completed chunks (None if no data)."""
        chars = sum(entry[0] for entry in self.chunk_durations)
        seconds = sum(entry[1] for entry in self.chunk_durations)
        return chars / seconds if seconds > 0 else None

    def _update_stats(self) -> None:
        """Compose the F3 stats line: avg rate, live per-chunk %, remaining, rolling ETA."""
        if not self.run_started_at or not self.total_count:
            return
        parts: list[str] = []
        rate = self._average_rate()
        if rate is not None:
            parts.append(f"Avg {rate:,.0f} char/s")
        if (
            1 <= self.current_chunk_index <= self.total_count
            and self.current_chunk_index <= len(self.chunk_statuses)
            and self.chunk_statuses[self.current_chunk_index - 1] == "Translating"
        ):
            parts.append(
                f"Chunk {self.current_chunk_index}: {self.current_chunk_translated_pct:.0f}% translated"
            )
        remaining_chars = None
        if rate is not None and self.completed_count < self.total_count:
            if self.total_translated_chars > 0 and self.completed_count > 0:
                avg_chunk_chars = self.total_translated_chars / self.completed_count
                remaining_chunks = self.total_count - self.completed_count
                remaining_chars = avg_chunk_chars * remaining_chunks
                if self.current_chunk_translated_pct > 0:
                    remaining_chars -= avg_chunk_chars * (self.current_chunk_translated_pct / 100)
                remaining_chars = max(0.0, remaining_chars)
            # Only show "Remaining" once we actually have a char-based estimate;
            # before the first chunk's char count lands, remaining_chars is None.
            if remaining_chars is not None:
                parts.append(f"Remaining ~{int(remaining_chars):,} chars")
        eta = (remaining_chars / rate) if (rate is not None and remaining_chars is not None and rate > 0) else None
        parts.append(f"ETA {format_duration(eta)}")
        self.stats_var.set("  |  ".join(parts))

    def _refresh_timing(self) -> None:
        if self.run_started_at and self.total_count:
            elapsed = (self.run_finished_at or time.monotonic()) - self.run_started_at
            eta = elapsed / self.completed_count * (self.total_count - self.completed_count) if self.completed_count else None
            self.timing_var.set(
                f"Elapsed {format_duration(elapsed)} | ETA {format_duration(eta)}"
            )
            self._update_stats()
        self.after(1000, self._refresh_timing)

    def _schedule_save_settings(self) -> None:
        """Debounce settings persistence (#20).

        Re-schedules a save a short time after the most recent control change,
        so dragging the speed slider doesn't rewrite settings.json on every tick
        but a crash soon after still persists the latest values.
        """
        if self._settings_save_after is not None:
            try:
                self.after_cancel(self._settings_save_after)
            except Exception:
                pass
        self._settings_save_after = self.after(SETTINGS_SAVE_DEBOUNCE_MS, self._save_settings_now)

    def _save_settings_now(self) -> None:
        self._settings_save_after = None
        self._save_settings()

    def _save_settings(self) -> None:
        geometry = self.geometry().split("+")[0]
        with self._settings_lock:
            scroll_interval = self.scroll_interval
            required_sweeps = self.required_sweeps
        save_settings({
            "scroll_interval": scroll_interval,
            "required_sweeps": required_sweeps,
            "chunk_preset": self.chunk_preset,
            "input_dir": self.input_dir,
            "output_dir": self.output_dir,
            "geometry": geometry,
        })

    def _on_close(self) -> None:
        # Flush any pending debounced save immediately before tearing down (#20).
        if self._settings_save_after is not None:
            try:
                self.after_cancel(self._settings_save_after)
            except Exception:
                pass
            self._settings_save_after = None
        self._save_settings()
        # Resume first so a worker blocked in _pace() can observe the cancel.
        self.pause_event.set()
        self.cancel_event.set()
        self.destroy()

    def _request_validation_decision(self, chunk_index: int, reason: str) -> str:
        response: dict[str, str] = {}
        ready = threading.Event()
        self._emit("validation", (chunk_index, reason, response, ready))
        while not ready.wait(0.1):
            if self.cancel_event.is_set():
                raise InterruptedError("Translation cancelled.")
            # While paused, keep waiting for the decision but don't time out;
            # the dialog stays up and the run simply holds (F2).
        return response["decision"]

    def _translate_worker(self, input_path: Path, output_path: Path) -> None:
        """Thin GUI-thread entry point: route pipeline logs to the GUI, then run.

        All of the real orchestration lives in ``pipeline.TranslationRunner``
        (#11). This worker only wires the queue-based log handler up for the
        duration of the run and supplies the thread-safe callables the runner
        needs, so the pipeline stays display-independent and testable.
        """
        handler = QueueHandler(self.events)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("novel_translator.pipeline")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            ctx = PipelineContext(
                events=self.events,
                cancel_event=self.cancel_event,
                pause_event=self.pause_event,
                get_scroll_interval=self._get_scroll_interval,
                get_required_sweeps=self._get_required_sweeps,
                request_validation=self._request_validation_decision,
                chunk_limit=CHUNK_PRESETS[self.chunk_preset],
            )
            TranslationRunner(ctx).run(input_path, output_path)
        finally:
            logger.removeHandler(handler)

    def _process_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event.kind == "log":
                    self.log.configure(state="normal")
                    message = str(event.value)
                    tag = "warning" if message.startswith("Automatic Chrome menu selection failed") else None
                    self.log._textbox.insert("end", f"{message}\n", tag)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif event.kind == "progress":
                    self.progress.set(float(event.value))
                elif event.kind == "chunk_statuses":
                    self.chunk_statuses = list(event.value)
                    self._render_chunk_statuses()
                elif event.kind == "run_info":
                    complete, total, failed = event.value
                    self.completed_count = complete
                    self.total_count = total
                    self.failed_count = failed
                    self.summary_var.set(f"{complete}/{total} complete | {failed} failed")
                    elapsed = time.monotonic() - self.run_started_at if self.run_started_at else 0
                    if self.run_finished_at:
                        elapsed = self.run_finished_at - self.run_started_at
                    eta = elapsed / complete * (total - complete) if complete else None
                    self.timing_var.set(
                        f"Elapsed {format_duration(elapsed)} | ETA {format_duration(eta)}"
                    )
                elif event.kind == "status":
                    self.status_var.set(str(event.value))
                elif event.kind == "current_chunk":
                    index, translated_pct = event.value
                    self.current_chunk_index = index
                    self.current_chunk_translated_pct = translated_pct
                    self.current_chunk_failed = False
                elif event.kind == "drop_rate":
                    index, translated_pct = event.value
                    if index == self.current_chunk_index:
                        self.current_chunk_translated_pct = translated_pct * 100
                elif event.kind == "stats":
                    chars, duration, index, translated_pct, failed = event.value
                    self.current_chunk_index = index
                    self.current_chunk_translated_pct = translated_pct
                    self.current_chunk_failed = failed
                    if chars > 0 and duration > 0:
                        self.chunk_durations.append((chars, duration))
                        self.total_translated_chars += chars
                    self._update_stats()
                elif event.kind == "validation":
                    chunk_index, reason, response, ready = event.value
                    choice = messagebox.askyesnocancel(
                        APP_NAME,
                        f"Chunk {chunk_index} failed validation:\n\n{reason}\n\n"
                        "Yes = Retry\nNo = Accept anyway\nCancel = Skip chunk",
                    )
                    response["decision"] = "retry" if choice is True else "accept" if choice is False else "skip"
                    ready.set()
                elif event.kind == "done":
                    self._finish("Complete")
                    self.open_book_button.configure(state="normal")
                    self.open_folder_button.configure(state="normal")
                    messagebox.showinfo(APP_NAME, f"EPUB saved to:\n{event.value}")
                elif event.kind == "incomplete":
                    self._finish("Incomplete")
                    self.retry_button.configure(state="normal")
                    messagebox.showwarning(
                        APP_NAME,
                        f"{event.value} chunk(s) could not be completed. Progress was saved. "
                        "Select Retry failed to process only those chunks.",
                    )
                elif event.kind == "error":
                    cancelled = self.cancel_event.is_set()
                    self._finish("Cancelled" if cancelled else "Failed")
                    if not cancelled:
                        messagebox.showerror(APP_NAME, str(event.value))
        except queue.Empty:
            pass
        self.after(100, self._process_events)

    def _finish(self, status: str) -> None:
        if self.run_started_at and not self.run_finished_at:
            self.run_finished_at = time.monotonic()
        self.status_var.set(status)
        badge_color = COLORS["accent"] if status == "Complete" else COLORS["danger"] if status in {"Failed", "Incomplete"} else COLORS["surface_alt"]
        self.status_badge.configure(fg_color=badge_color, text_color=COLORS["text"])
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.pause_event.set()
        self.pause_button.configure(state="disabled", text="Pause")
        if status == "Complete":
            self.retry_button.configure(state="disabled")
        self._save_settings()
