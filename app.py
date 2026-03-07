import json
import os
import re
import subprocess
import tempfile
import threading
import tkinter as tk
import ctypes
import sys
import zlib
import webbrowser
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from ctypes import wintypes
from urllib import error as urlerror
from urllib import request as urlrequest

from PIL import Image, ImageOps, ImageTk

from app_helpers import CatalogHelper, ImageStateHelper, MetadataParser, PreviewMixin, ReviewHelper, StateStore, UiDispatchMixin

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
DEFAULT_THUMB_SIZE = 180
DEFAULT_COLUMNS = 4
THUMB_RENDER_BATCH = 28
THUMB_CACHE_MAX = 900
THUMB_SIZE_DEBOUNCE_MS = 180
THUMB_LAYOUT_UPDATE_EVERY_BATCHES = 3
STATE_SAVE_DEBOUNCE_MS = 420
METADATA_WARMUP_BATCH = 64
METADATA_PARSE_REV = 3
APP_VERSION = "3.0.0"
REVIEW_STATUSES = ("All", "Unreviewed", "Reject")
UPDATE_REPO_OWNER = "ixxeg"
UPDATE_REPO_NAME = "PromptLens"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/releases"
METADATA_EMPTY_TEXT = "Select an image to inspect metadata\n\nUse single click for details and double-click for full preview."
PREVIEW_EMPTY_TEXT = "Select an image to preview it here."
PREVIEW_REVIEW_EMPTY_TEXT = "Enable Review Mode and select an image to preview it here."
NO_MATCH_TEXT = "Nothing matched this filter\n\nTry a broader search, remove a tag filter, or disable favorites-only mode."
NO_IMAGE_SELECTED_TITLE = "No image selected"

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def get_app_dir() -> Path:
    # For PyInstaller onefile/onedir, keep state near the executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


STATE_FILE = get_app_dir() / ".image_catalog_state.json"
PALETTE = {
    "bg": "#edf3f8",
    "panel": "#e6edf4",
    "surface": "#ffffff",
    "surface_alt": "#f5f8fb",
    "surface_1": "#ffffff",
    "surface_2": "#f7fbff",
    "surface_3": "#ebf2f8",
    "gradient_top": "#f7fbff",
    "gradient_mid": "#eff5fb",
    "gradient_bottom": "#e5edf6",
    "text": "#1f2a37",
    "muted": "#68768a",
    "accent": "#2563eb",
    "accent_hover": "#3b82f6",
    "accent_active": "#1d4ed8",
    "accent_muted": "#bfdbfe",
    "border": "#d7e1eb",
    "focus_ring": "#60a5fa",
    "danger": "#cf5a73",
    "chip_bg": "#dbeafe",
    "chip_fg": "#1e40af",
    "chip_soft_bg": "#eff6ff",
    "chip_soft_fg": "#31527b",
    "glass_prompt": "#f3f8ff",
    "glass_negative": "#fff2f4",
    "glass_main": "#f6fbff",
    "glass_misc": "#f4f8fc",
    "shadow": "#dce6f0",
    "thumb_shadow": "#dde7f1",
    "thumb_bg": "#fbfdff",
    "thumb_hover_bg": "#f3f8ff",
    "thumb_selected_bg": "#eef5ff",
    "status_info": "#3b82f6",
    "status_busy": "#2563eb",
    "status_ok": "#6f9f8a",
    "status_warn": "#b08f65",
    "status_error": "#cf5a73",
    "skeleton_base": "#e5edf6",
    "skeleton_shine": "#f7fbff",
}
SUBFOLDER_DOT_COLORS = [
    "#8a63d2",
    "#9f60d6",
    "#b064cf",
    "#c96ab9",
    "#d776a3",
    "#de878f",
    "#d89576",
    "#cda05f",
    "#b9ad5a",
    "#9ab764",
    "#7ec074",
    "#66bf8a",
    "#5db9a0",
    "#5fb0b8",
    "#669fd0",
    "#788fd9",
    "#8c84da",
    "#a17cda",
    "#b471d5",
    "#7a95d4",
    "#67a9d1",
    "#69b7c3",
    "#79bfaa",
    "#8ec297",
]


def enable_high_dpi() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return


class PreviewWindow(PreviewMixin, tk.Toplevel):
    def __init__(self, parent: tk.Tk, image_path: Path) -> None:
        super().__init__(parent)
        self.geometry("1200x820")
        self._init_preview_state(image_path)
        self._build_ui()
        self.load_path(image_path)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(7, weight=1)
        ttk.Button(toolbar, text="-", width=3, command=self.zoom_out).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(toolbar, text="+", width=3, command=self.zoom_in).grid(row=0, column=1, padx=(0, 12))
        ttk.Button(toolbar, text="100%", command=self.reset_zoom).grid(row=0, column=2, padx=(0, 12))
        ttk.Button(toolbar, text="Fit", command=self.fit_to_window).grid(row=0, column=3, padx=(0, 12))
        ttk.Label(toolbar, textvariable=self.zoom_var).grid(row=0, column=4, padx=(0, 12))
        ttk.Label(toolbar, textvariable=self.path_var).grid(row=0, column=7, sticky="e")

        content = ttk.Frame(self)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(content, background="#101010", highlightthickness=0)
        self.v_scroll = ttk.Scrollbar(content, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(content, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._loading_item = self.canvas.create_text(0, 0, text="Loading preview...", fill="#f2f2f2", font=("Segoe UI Semibold", 12), state="hidden")
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind("<plus>", lambda _e: self.zoom_in())
        self.bind("<minus>", lambda _e: self.zoom_out())
        self.bind("<Control-0>", lambda _e: self.reset_zoom())
        self.bind("<F>", lambda _e: self.fit_to_window())
        self.canvas.focus_set()

    def _on_preview_path_changed(self, image_path: Path) -> None:
        self.title(f"Preview: {image_path.name}")

    def _on_preview_load_failed(self, exc: Exception) -> None:
        self._hide_loading()
        messagebox.showerror("Preview error", f"Cannot open image:\n{exc}")
        self.destroy()

    def destroy(self) -> None:
        self._destroy_preview_resources()
        super().destroy()


class InlinePreviewPane(PreviewMixin, tk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=PALETTE["surface_1"])
        self._init_preview_state()
        self._build_ui()
        self.clear(PREVIEW_REVIEW_EMPTY_TEXT)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = tk.Frame(self, bg=PALETTE["surface_1"], padx=10, pady=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(5, weight=1)
        ttk.Button(toolbar, text="-", width=3, command=self.zoom_out).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(toolbar, text="+", width=3, command=self.zoom_in).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(toolbar, text="100%", command=self.reset_zoom).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(toolbar, text="Fit", command=self.fit_to_window).grid(row=0, column=3, padx=(0, 10))
        ttk.Label(toolbar, textvariable=self.zoom_var, style="Muted.TLabel").grid(row=0, column=4, sticky="w", padx=(0, 12))
        tk.Label(toolbar, textvariable=self.path_var, bg=PALETTE["surface_1"], fg=PALETTE["muted"], font=("Segoe UI", 9), anchor="e").grid(row=0, column=5, sticky="ew")
        content = tk.Frame(self, bg=PALETTE["surface_3"])
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(content, background="#101010", highlightthickness=0)
        self.v_scroll = ttk.Scrollbar(content, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(content, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._loading_item = self.canvas.create_text(0, 0, text="Loading preview...", fill="#f2f2f2", font=("Segoe UI Semibold", 12), state="hidden")
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def destroy(self) -> None:
        self._destroy_preview_resources()
        super().destroy()


class ImageMetadataViewer(UiDispatchMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._init_ui_dispatcher()
        self.title("PromptLens")
        self.geometry("1400x820")
        self.configure(background=PALETTE["bg"])
        self.style = ttk.Style(self)
        self._setup_styles()
        self._build_main_menu()

        self.selected_dirs: list[Path] = []
        self.all_image_paths: list[Path] = []
        self.image_paths: list[Path] = []
        self.thumbnail_refs: list[ImageTk.PhotoImage] = []
        self.thumbnail_cache: OrderedDict[tuple[str, int, int, int], ImageTk.PhotoImage] = OrderedDict()
        self.metadata_cache: dict[str, dict[str, str]] = {}
        self.metadata_cache_sig: dict[str, tuple[int, int, int]] = {}
        self.search_index_cache: dict[str, tuple[tuple[int, int, int, str], str, str]] = {}
        self.image_state: dict[str, dict[str, object]] = {}
        self.file_mtime_cache: dict[str, float] = {}
        self._cache_lock = threading.Lock()
        self._thumb_prepare_lock = threading.Lock()
        self.current_image_path: Path | None = None
        self.preview_window: PreviewWindow | None = None
        self.folders_panel_visible = True
        self.image_root_map: dict[str, Path] = {}
        self._scan_roots_snapshot: list[Path] = []
        self._scan_token = 0
        self._update_check_in_progress = False
        self._filter_token = 0
        self._filter_worker_thread: threading.Thread | None = None
        self._thumb_decode_token = 0
        self._thumb_decode_thread: threading.Thread | None = None
        self._thumb_prepared_images: dict[int, tuple[tuple[str, int, int, int], Image.Image]] = {}

        self.thumb_size_var = tk.DoubleVar(value=float(DEFAULT_THUMB_SIZE))
        self.columns_var = tk.StringVar(value=str(DEFAULT_COLUMNS))
        self.status_var = tk.StringVar(value="Add one or multiple folders")
        self.search_var = tk.StringVar(value="")
        self.tag_filter_var = tk.StringVar(value="")
        self.favorites_only_var = tk.BooleanVar(value=False)
        self.review_filter_var = tk.StringVar(value="All")
        self.review_mode_var = tk.BooleanVar(value=False)
        self.sort_var = tk.StringVar(value="Newest")
        self._thumb_render_after_id: str | None = None
        self._thumb_batch_after_id: str | None = None
        self._thumb_frame_configure_after_id: str | None = None
        self._thumb_render_token = 0
        self._thumb_render_preserve_view = False
        self._thumb_render_view: tuple[float, float] = (0.0, 0.0)
        self._thumb_rendering = False
        self._thumb_batch_counter = 0
        self._thumb_last_applied_size = int(round(self.thumb_size_var.get()))
        self._thumb_last_applied_columns = self._safe_columns()
        self._column_render_after_id: str | None = None
        self._filter_after_id: str | None = None
        self._save_state_after_id: str | None = None
        self._wheel_y_delta_accum = 0
        self._wheel_x_delta_accum = 0
        self._wheel_y_after_id: str | None = None
        self._wheel_x_after_id: str | None = None
        self._metadata_warmup_token = 0
        self._metadata_warmup_thread: threading.Thread | None = None
        self._meta_fade_after_id: str | None = None
        self._button_anim_after_ids: dict[int, str] = {}
        self._button_anim_tokens: dict[int, int] = {}
        self._button_target_colors: dict[int, tuple[str, str]] = {}
        self._metadata_tag_targets: dict[str, str] = {
            "file_title": PALETTE["accent_active"],
            "section": PALETTE["accent"],
            "section_negative": PALETTE["danger"],
            "separator": "#9fb3c8",
            "field_name": "#436180",
            "field_value": PALETTE["text"],
        }
        self._meta_start_color = "#a8b7c9"
        self.thumb_cells_by_path: dict[str, int] = {}
        self.thumb_inner_by_path: dict[str, int] = {}
        self.thumb_widget_by_path: dict[str, tuple[int, int | None]] = {}
        self._hover_path_key: str | None = None
        self._folder_tip_win: tk.Toplevel | None = None
        self._folder_tip_label: tk.Label | None = None
        self.summary_chip_frames: dict[str, tk.Frame] = {}
        self._summary_flash_after_ids: dict[str, str] = {}
        self._summary_chip_hovered: dict[str, bool] = {}
        self._summary_chip_hover_border = self._mix_hex(PALETTE["accent_muted"], PALETTE["accent"], 0.75)
        self.gallery_status_var = tk.StringVar(value="")
        self.review_status_var = tk.StringVar(value="Unreviewed")
        self.right_panel_title_var = tk.StringVar(value="Inspector")
        self._pending_focus_path: Path | None = None
        self._delete_rejected_count = -1
        self._delete_rejected_visible = False
        self._delete_rejected_slot_width = 0
        self._delete_rejected_in_progress = False
        self._state_store = StateStore(STATE_FILE)

        self._load_state()
        self._thumb_last_applied_size = max(100, min(320, int(round(self.thumb_size_var.get()))))
        self._thumb_last_applied_columns = self._safe_columns()
        self._build_ui()
        self._refresh_folder_rows()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.selected_dirs:
            self._set_status(f"Restored folders: {len(self.selected_dirs)}. Auto scan started...", "info")
            self.after(120, self.scan_images)

    def _setup_styles(self) -> None:
        self.style.theme_use("clam")

        self.style.configure(".", background=PALETTE["bg"], foreground=PALETTE["text"])
        self.style.configure("Root.TFrame", background=PALETTE["bg"])
        self.style.configure("Panel.TFrame", background=PALETTE["panel"])
        self.style.configure("Surface.TFrame", background=PALETTE["surface_1"])
        self.style.configure(
            "TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["text"],
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "TCheckbutton",
            background=PALETTE["bg"],
            foreground=PALETTE["text"],
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "Title.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["text"],
            font=("Segoe UI Semibold", 16),
        )
        self.style.configure(
            "Muted.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["muted"],
            font=("Segoe UI", 9),
        )
        self.style.configure(
            "Panel.TLabel",
            background=PALETTE["panel"],
            foreground=PALETTE["text"],
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "Accent.TButton",
            background=PALETTE["accent"],
            foreground="#ffffff",
            bordercolor=PALETTE["accent"],
            padding=(16, 9),
            relief="flat",
            font=("Segoe UI Semibold", 10),
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", PALETTE["accent_hover"]), ("pressed", PALETTE["accent_active"])],
            foreground=[("disabled", "#eee7f8")],
        )
        self.style.configure(
            "Soft.TButton",
            background=PALETTE["surface_1"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            padding=(14, 8),
            relief="flat",
            font=("Segoe UI", 10),
        )
        self.style.map(
            "Soft.TButton",
            background=[("active", PALETTE["surface_2"]), ("pressed", "#e4daf7")],
            bordercolor=[("active", PALETTE["accent"])],
        )
        self.style.configure(
            "MiniSoft.TButton",
            background=PALETTE["surface_1"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            padding=(10, 5),
            relief="flat",
            font=("Segoe UI", 9),
        )
        self.style.map(
            "MiniSoft.TButton",
            background=[("active", PALETTE["surface_2"])],
            bordercolor=[("active", PALETTE["accent"])],
        )
        self.style.configure(
            "MiniAccent.TButton",
            background=PALETTE["accent"],
            foreground="#ffffff",
            bordercolor=PALETTE["accent"],
            padding=(10, 5),
            relief="flat",
            font=("Segoe UI Semibold", 9),
        )
        self.style.map(
            "MiniAccent.TButton",
            background=[("active", PALETTE["accent_hover"]), ("pressed", PALETTE["accent_active"])],
        )
        self.style.configure(
            "MiniDanger.TButton",
            background="#fff8f8",
            foreground=PALETTE["danger"],
            bordercolor="#f3d4da",
            padding=(8, 3),
            relief="flat",
            font=("Segoe UI Semibold", 8),
        )
        self.style.map(
            "MiniDanger.TButton",
            background=[("active", "#fdf0f2"), ("pressed", "#f9e5e9")],
            bordercolor=[("active", PALETTE["danger"])],
        )
        self.style.configure(
            "TScrollbar",
            background=PALETTE["panel"],
            troughcolor=PALETTE["surface_alt"],
            bordercolor=PALETTE["border"],
            arrowcolor=PALETTE["text"],
        )
        self.style.configure(
            "TSpinbox",
            fieldbackground=PALETTE["surface"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            arrowsize=18,
            padding=6,
            font=("Segoe UI Semibold", 12),
        )
        self.style.configure(
            "TScale",
            background=PALETTE["bg"],
            troughcolor="#ddcff3",
            sliderthickness=18,
        )
        self.style.configure(
            "TEntry",
            fieldbackground=PALETTE["surface_1"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            padding=6,
            insertcolor=PALETTE["text"],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=PALETTE["surface_1"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            arrowcolor=PALETTE["text"],
            padding=4,
        )
        self.style.configure(
            "TPanedwindow",
            background=PALETTE["bg"],
            sashrelief="flat",
            sashthickness=8,
        )
        self.style.configure(
            "MiniSoft.TButton",
            background=PALETTE["surface_1"],
            foreground=PALETTE["text"],
            bordercolor=PALETTE["border"],
            padding=(8, 3),
            relief="flat",
            font=("Segoe UI", 9),
        )
        self.style.map(
            "MiniSoft.TButton",
            background=[("active", PALETTE["surface_2"]), ("pressed", "#e4daf7")],
            bordercolor=[("active", PALETTE["accent"])],
        )
        self.style.configure(
            "ChipTitle.TLabel",
            background=PALETTE["chip_soft_bg"],
            foreground=PALETTE["muted"],
            font=("Segoe UI Semibold", 8),
        )
        self.style.configure(
            "ChipValue.TLabel",
            background=PALETTE["chip_soft_bg"],
            foreground=PALETTE["chip_soft_fg"],
            font=("Segoe UI Semibold", 11),
        )

    def _build_main_menu(self) -> None:
        menubar = tk.Menu(self)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Check for updates", command=self.check_for_updates)
        app_menu.add_separator()
        app_menu.add_command(label="Open Releases page", command=self._open_releases_page)
        menubar.add_cascade(label="App", menu=app_menu)
        self.config(menu=menubar)

    def check_for_updates(self) -> None:
        if self._update_check_in_progress:
            self._set_status("Update check already in progress...", "info")
            return
        self._update_check_in_progress = True
        self._set_status("Checking latest release...", "busy")
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self) -> None:
        try:
            req = urlrequest.Request(
                GITHUB_LATEST_RELEASE_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"PromptLens/{APP_VERSION}",
                },
            )
            with urlrequest.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected response from GitHub API.")
            tag_name = str(payload.get("tag_name", "")).strip()
            html_url = str(payload.get("html_url", "")).strip() or GITHUB_RELEASES_PAGE
            assets = payload.get("assets", [])
            if not isinstance(assets, list):
                assets = []

            latest = tag_name.lstrip("vV")
            if not latest:
                raise RuntimeError("Could not detect latest version tag.")
            if not self._is_newer_version(latest, APP_VERSION):
                self._post_to_ui(lambda: self._on_update_check_finished("You already have the latest version.", "ok"))
                return

            asset_url, asset_name = self._choose_release_asset(assets)
            if not asset_url:
                self._post_to_ui(
                    lambda: self._on_update_check_no_asset(latest, html_url),
                )
                return

            self._post_to_ui(
                lambda: self._on_update_available(latest, asset_url, asset_name, html_url),
            )
        except (urlerror.URLError, TimeoutError) as exc:
            self._post_to_ui(lambda: self._on_update_check_finished(f"Update check failed: {exc}", "error"))
        except Exception as exc:  # noqa: BLE001
            self._post_to_ui(lambda: self._on_update_check_finished(f"Update check failed: {exc}", "error"))

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        clean = value.strip().lstrip("vV")
        parts: list[int] = []
        for part in clean.split("."):
            digits = "".join(ch for ch in part if ch.isdigit())
            if digits:
                parts.append(int(digits))
            else:
                parts.append(0)
        return tuple(parts)

    def _is_newer_version(self, candidate: str, current: str) -> bool:
        return self._version_tuple(candidate) > self._version_tuple(current)

    def _choose_release_asset(self, assets: list[object]) -> tuple[str, str]:
        entries: list[tuple[str, str]] = []
        for item in assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("browser_download_url", "")).strip()
            if name and url:
                entries.append((name, url))
        if not entries:
            return "", ""

        current_name = Path(sys.executable).name.lower() if getattr(sys, "frozen", False) else ""
        if current_name:
            for name, url in entries:
                if name.lower() == current_name:
                    return url, name
        for name, url in entries:
            if name.lower().endswith(".exe"):
                return url, name
        for name, url in entries:
            if name.lower().endswith(".zip"):
                return url, name
        return entries[0][1], entries[0][0]

    def _on_update_check_no_asset(self, latest: str, release_url: str) -> None:
        self._update_check_in_progress = False
        self._set_status(f"Latest version {latest} found, but no downloadable asset was detected.", "warn")
        if messagebox.askyesno(
            "Update",
            f"New version {latest} is available, but no .exe asset was found.\nOpen Releases page?",
        ):
            webbrowser.open(release_url)

    def _on_update_available(self, latest: str, asset_url: str, asset_name: str, release_url: str) -> None:
        self._update_check_in_progress = False
        self._set_status(f"Update available: {latest}", "info")
        if not getattr(sys, "frozen", False):
            if messagebox.askyesno(
                "Update available",
                f"Version {latest} is available.\n\nRunning from source mode now.\nOpen Releases page?",
            ):
                webbrowser.open(release_url)
            return

        if asset_name.lower().endswith(".zip"):
            if messagebox.askyesno(
                "Update available",
                f"Version {latest} is available.\nRelease asset is a ZIP package.\nOpen Releases page?",
            ):
                webbrowser.open(release_url)
            return

        should_update = messagebox.askyesno(
            "Update available",
            f"Version {latest} is available.\n\nInstall update now?\n(The app will restart.)",
        )
        if not should_update:
            return

        self._update_check_in_progress = True
        self._set_status(f"Downloading update {latest}...", "busy")
        threading.Thread(
            target=self._download_update_worker,
            args=(asset_url, asset_name),
            daemon=True,
        ).start()

    def _download_update_worker(self, asset_url: str, asset_name: str) -> None:
        app_dir = get_app_dir()
        safe_name = Path(asset_name).name
        download_path = app_dir / f".update_{safe_name}.download"
        try:
            req = urlrequest.Request(
                asset_url,
                headers={"User-Agent": f"PromptLens/{APP_VERSION}"},
            )
            with urlrequest.urlopen(req, timeout=60) as resp, open(download_path, "wb") as out:
                while True:
                    chunk = resp.read(512 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            self._post_to_ui(lambda: self._apply_downloaded_update(download_path))
        except Exception as exc:  # noqa: BLE001
            try:
                if download_path.exists():
                    download_path.unlink()
            except Exception:
                pass
            self._post_to_ui(lambda: self._on_update_check_finished(f"Download failed: {exc}", "error"))

    @staticmethod
    def _escape_cmd_value(value: Path | str) -> str:
        escaped = str(value).replace("^", "^^").replace("%", "%%").replace("&", "^&").replace("|", "^|")
        escaped = escaped.replace("<", "^<").replace(">", "^>").replace("(", "^(").replace(")", "^)")
        return escaped.replace("!", "^^!")

    def _apply_downloaded_update(self, downloaded_file: Path) -> None:
        self._update_check_in_progress = False
        if not getattr(sys, "frozen", False):
            self._set_status("Update downloaded, but auto-install works only in EXE mode.", "warn")
            return
        target_exe = Path(sys.executable).resolve()
        if downloaded_file.suffix.lower() != ".download":
            self._set_status("Downloaded update has unexpected format.", "error")
            return
        new_exe = downloaded_file.with_suffix("")
        try:
            if new_exe.exists():
                new_exe.unlink()
            downloaded_file.rename(new_exe)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Cannot prepare update file: {exc}", "error")
            return

        script_path = Path(tempfile.gettempdir()) / f"promptlens_updater_{os.getpid()}.cmd"
        script_text = (
            "@echo off\r\n"
            "setlocal\r\n"
            f"set \"TARGET={self._escape_cmd_value(target_exe)}\"\r\n"
            f"set \"NEWFILE={self._escape_cmd_value(new_exe)}\"\r\n"
            ":retry\r\n"
            "move /Y \"%NEWFILE%\" \"%TARGET%\" >nul 2>nul\r\n"
            "if errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto retry\r\n"
            ")\r\n"
            "start \"\" \"%TARGET%\"\r\n"
            "del \"%~f0\"\r\n"
        )
        try:
            script_path.write_text(script_text, encoding="utf-8")
            subprocess.Popen(
                ["cmd", "/c", str(script_path)],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
            self._set_status("Installing update and restarting...", "busy")
            self.after(200, self._on_close)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Cannot run updater: {exc}", "error")

    def _on_update_check_finished(self, text: str, kind: str) -> None:
        self._update_check_in_progress = False
        self._set_status(text, kind)
        if kind == "error":
            messagebox.showerror("Update", text)
        elif kind == "ok":
            messagebox.showinfo("Update", text)

    @staticmethod
    def _open_releases_page() -> None:
        webbrowser.open(GITHUB_RELEASES_PAGE)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(14, 12, 14, 10), style="Root.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        header_shell = tk.Frame(
            top,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=14,
            pady=14,
        )
        header_shell.grid(row=0, column=0, sticky="ew")
        header_shell.columnconfigure(0, weight=1)
        header_shell.columnconfigure(1, weight=0)

        utility_card = tk.Frame(
            header_shell,
            bg=PALETTE["surface_1"],
            highlightthickness=0,
            highlightbackground=PALETTE["surface_1"],
            bd=0,
            padx=0,
            pady=0,
        )
        utility_card.grid(row=0, column=1, sticky="e")
        utility_card.columnconfigure(1, weight=1)

        actions_row = tk.Frame(header_shell, bg=PALETTE["surface_1"])
        actions_row.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        actions_row.columnconfigure(0, weight=1)

        controls_left = tk.Frame(actions_row, bg=PALETTE["surface_1"])
        controls_left.grid(row=0, column=0, sticky="w")
        controls_right = tk.Frame(actions_row, bg=PALETTE["surface_1"])
        controls_right.grid(row=0, column=1, sticky="e")

        self.add_btn = ttk.Button(controls_left, text="[+] Add folder", command=self.add_folder, style="Soft.TButton")
        self.add_btn.pack(side="left", padx=(0, 8))
        self.clear_btn = ttk.Button(controls_left, text="[-] Clear folders", command=self.clear_folders, style="Soft.TButton")
        self.clear_btn.pack(side="left", padx=(0, 8))
        self.folders_toggle_btn = ttk.Button(controls_left, text="[F] Hide folders", command=self._toggle_folders_panel, style="Soft.TButton")
        self.folders_toggle_btn.pack(side="left", padx=(0, 10))
        self.scan_btn = ttk.Button(controls_left, text="[S] Scan", command=self.scan_images, style="Accent.TButton")
        self.scan_btn.pack(side="left")
        self._enable_button_hover_animation(self.add_btn, is_accent=False)
        self._enable_button_hover_animation(self.clear_btn, is_accent=False)
        self._enable_button_hover_animation(self.folders_toggle_btn, is_accent=False)
        self._enable_button_hover_animation(self.scan_btn, is_accent=True)

        tk.Label(
            utility_card,
            text="Thumb size",
            bg=PALETTE["surface_1"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.thumb_size_scale = ttk.Scale(
            utility_card,
            from_=100,
            to=320,
            variable=self.thumb_size_var,
            command=self.on_thumb_size_changed,
        )
        self.thumb_size_scale.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.thumb_size_scale.bind("<ButtonRelease-1>", lambda _e: self._apply_thumb_size_change(force=True))

        tk.Label(
            utility_card,
            text="Columns",
            bg=PALETTE["surface_1"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        ).grid(row=0, column=2, sticky="e", padx=(4, 6))
        self.columns_spin = ttk.Spinbox(
            utility_card,
            from_=2,
            to=12,
            textvariable=self.columns_var,
            width=5,
            command=self.on_layout_changed,
            font=("Segoe UI Semibold", 12),
        )
        self.columns_spin.grid(row=0, column=3, sticky="e")
        self.columns_spin.bind("<Return>", lambda _e: self._apply_layout_change())
        self.columns_spin.bind("<FocusOut>", lambda _e: self._apply_layout_change())

        filter_row = tk.Frame(
            header_shell,
            bg=PALETTE["surface_2"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=10,
            pady=10,
        )
        filter_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        filter_row.columnconfigure(1, weight=1)

        tk.Label(filter_row, text="Search", bg=PALETTE["surface_2"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.search_entry = ttk.Entry(filter_row, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(10, 14), ipady=5)
        self.search_entry.bind("<KeyRelease>", lambda _e: self._schedule_apply_filters())

        tk.Label(filter_row, text="Prompt tag", bg=PALETTE["surface_2"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.tag_entry = ttk.Entry(filter_row, textvariable=self.tag_filter_var, width=18)
        self.tag_entry.grid(row=0, column=3, sticky="w", padx=(0, 12), ipady=5)
        self.tag_entry.bind("<KeyRelease>", lambda _e: self._schedule_apply_filters())

        self.favorites_only_chk = tk.Checkbutton(
            filter_row,
            text="Favorites only",
            variable=self.favorites_only_var,
            command=self.apply_filters,
            bg=PALETTE["surface_2"],
            fg=PALETTE["text"],
            activebackground=PALETTE["surface_2"],
            activeforeground=PALETTE["text"],
            selectcolor=PALETTE["surface_1"],
            font=("Segoe UI", 10),
            bd=0,
            highlightthickness=0,
        )
        self.favorites_only_chk.grid(row=0, column=4, sticky="w", padx=(0, 10))

        tk.Label(filter_row, text="Review", bg=PALETTE["surface_2"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=5, sticky="e", padx=(0, 6))
        self.review_combo = ttk.Combobox(
            filter_row,
            textvariable=self.review_filter_var,
            values=REVIEW_STATUSES,
            state="readonly",
            width=11,
        )
        self.review_combo.grid(row=0, column=6, sticky="w", padx=(0, 10))
        self.review_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())

        tk.Label(filter_row, text="Sort", bg=PALETTE["surface_2"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=7, sticky="e", padx=(0, 6))
        self.sort_combo = ttk.Combobox(
            filter_row,
            textvariable=self.sort_var,
            values=("Newest", "Oldest"),
            state="readonly",
            width=10,
        )
        self.sort_combo.grid(row=0, column=8, sticky="w", padx=(0, 10))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())

        self.clear_filter_btn = ttk.Button(filter_row, text="[C] Clear filters", style="Soft.TButton", command=self._clear_filters)
        self.clear_filter_btn.grid(row=0, column=9, sticky="w")
        self._enable_button_hover_animation(self.clear_filter_btn, is_accent=False)

        self.folders_panel = tk.Frame(
            header_shell,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=6,
            pady=4,
        )
        self.folders_panel.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.folders_panel.columnconfigure(0, weight=1)

        self.folders_container = tk.Frame(self.folders_panel, bg=PALETTE["surface_1"], bd=0)
        self.folders_container.grid(row=0, column=0, sticky="ew")
        self._set_status(self.status_var.get(), "info")
        self._apply_folders_panel_visibility()

        body = tk.PanedWindow(
            self,
            orient="horizontal",
            opaqueresize=False,
            sashwidth=8,
            sashrelief="flat",
            bd=0,
            bg=PALETTE["bg"],
        )
        body.grid(row=1, column=0, sticky="nsew")

        left = tk.Frame(body, bg=PALETTE["panel"], padx=10, pady=8)
        right = tk.Frame(body, bg=PALETTE["panel"], padx=8, pady=8)
        body.add(left, minsize=520, stretch="always")
        body.add(right, minsize=380)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        gallery_shell = tk.Frame(
            left,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=10,
            pady=10,
        )
        gallery_shell.grid(row=0, column=0, sticky="nsew")
        gallery_shell.columnconfigure(0, weight=1)
        gallery_shell.rowconfigure(2, weight=1)
        gallery_head = tk.Frame(gallery_shell, bg=PALETTE["surface_1"])
        gallery_head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        gallery_head.columnconfigure(0, weight=1)
        gallery_head.columnconfigure(1, weight=0)
        gallery_head.columnconfigure(2, weight=0)
        tk.Label(
            gallery_head,
            text="Gallery",
            bg=PALETTE["surface_1"],
            fg=PALETTE["text"],
            font=("Segoe UI Semibold", 14),
        ).grid(row=0, column=0, sticky="w")
        gallery_actions = tk.Frame(gallery_head, bg=PALETTE["surface_1"])
        gallery_actions.grid(row=0, column=1, sticky="e", padx=(12, 12))
        gallery_actions.columnconfigure(0, weight=0)
        self.review_main_row = tk.Frame(gallery_actions, bg=PALETTE["surface_1"])
        self.review_main_row.grid(row=0, column=0, sticky="w")
        self.review_mode_btn = ttk.Button(
            self.review_main_row,
            text="[V] Review mode",
            style="MiniSoft.TButton",
            command=self.toggle_review_mode,
        )
        self.review_mode_btn.pack(side="left", padx=(0, 8))
        self.review_controls = tk.Frame(self.review_main_row, bg=PALETTE["surface_1"])
        self.favorite_btn = ttk.Button(
            self.review_controls,
            text="[F] Favorite",
            style="MiniSoft.TButton",
            command=self.toggle_current_favorite,
        )
        self.favorite_btn.pack(side="left", padx=(0, 6))
        self.reject_btn = ttk.Button(
            self.review_controls,
            text="[R] Reject",
            style="MiniSoft.TButton",
            command=lambda: self.set_current_review_status("reject"),
        )
        self.reject_btn.pack(side="left", padx=(0, 6))
        self.reset_review_btn = ttk.Button(
            self.review_controls,
            text="[U] Reset",
            style="MiniSoft.TButton",
            command=lambda: self.set_current_review_status("unreviewed"),
        )
        self.reset_review_btn.pack(side="left", padx=(0, 8))
        self.review_status_label = tk.Label(
            self.review_controls,
            textvariable=self.review_status_var,
            bg=PALETTE["chip_soft_bg"],
            fg=PALETTE["chip_soft_fg"],
            font=("Segoe UI Semibold", 8),
            padx=8,
            pady=3,
        )
        self.review_status_label.pack(side="left")
        self.review_delete_row = tk.Frame(gallery_actions, bg=PALETTE["surface_1"])
        self.delete_rejected_slot = tk.Frame(self.review_delete_row, bg=PALETTE["surface_1"], width=150, height=1)
        self.delete_rejected_slot.grid(row=0, column=0, sticky="w")
        self.delete_rejected_slot.grid_propagate(False)
        self.delete_rejected_btn = ttk.Button(
            self.delete_rejected_slot,
            text="Delete Rejected",
            style="MiniDanger.TButton",
            command=self.delete_rejected_images,
        )
        self.delete_rejected_btn.pack(fill="x")
        self.gallery_status = tk.Label(
            gallery_head,
            textvariable=self.gallery_status_var,
            bg=PALETTE["surface_1"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        )
        self.gallery_status.grid(row=0, column=2, sticky="e")

        canvas_wrap = tk.Frame(gallery_shell, bg=PALETTE["surface_3"], bd=0)
        canvas_wrap.grid(row=2, column=0, sticky="nsew")
        canvas_wrap.columnconfigure(0, weight=1)
        canvas_wrap.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(canvas_wrap, highlightthickness=0, background=PALETTE["surface_3"])
        self.v_scroll = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(canvas_wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)

        self.canvas_window = None
        self.canvas.bind("<Configure>", self._on_thumb_frame_configure)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel)
        self.canvas.bind("<Shift-Button-4>", self._on_shift_mouse_wheel)
        self.canvas.bind("<Shift-Button-5>", self._on_shift_mouse_wheel)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")

        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        tk.Label(
            right,
            textvariable=self.right_panel_title_var,
            bg=PALETTE["panel"],
            fg=PALETTE["text"],
            font=("Segoe UI Semibold", 16),
        ).grid(row=0, column=0, sticky="w", pady=(2, 8))

        self.inspector_panel = tk.Frame(right, bg=PALETTE["panel"])
        self.inspector_panel.grid(row=1, column=0, sticky="nsew")
        self.inspector_panel.columnconfigure(0, weight=1)
        self.inspector_panel.rowconfigure(2, weight=1)

        actions = tk.Frame(
            self.inspector_panel,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=12,
            pady=10,
        )
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(1, weight=1)

        actions_top = tk.Frame(actions, bg=PALETTE["surface_1"])
        actions_top.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.copy_prompt_btn = ttk.Button(actions_top, text="[C] Copy prompt", style="Soft.TButton", command=self.copy_current_prompt)
        self.copy_prompt_btn.pack(side="left")
        self._enable_button_hover_animation(self.copy_prompt_btn, is_accent=False)

        tk.Label(actions, text="Tags", bg=PALETTE["surface_1"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        self.current_tags_var = tk.StringVar(value="")
        self.current_tags_entry = ttk.Entry(actions, textvariable=self.current_tags_var)
        self.current_tags_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), ipady=4)
        self.save_tags_btn = ttk.Button(actions, text="[T] Save tags", style="Soft.TButton", command=self.save_current_tags)
        self.save_tags_btn.grid(row=1, column=2, sticky="e")
        self._enable_button_hover_animation(self.save_tags_btn, is_accent=False)

        self.inspector_summary = tk.Frame(
            self.inspector_panel,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=12,
            pady=12,
        )
        self.inspector_summary.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.inspector_summary.columnconfigure(0, weight=1)
        self.inspector_summary.columnconfigure(1, weight=1)
        self.inspector_summary.columnconfigure(2, weight=1)

        self.meta_file_var = tk.StringVar(value=NO_IMAGE_SELECTED_TITLE)
        self.meta_path_var = tk.StringVar(value="")
        tk.Label(
            self.inspector_summary,
            textvariable=self.meta_file_var,
            bg=PALETTE["surface_1"],
            fg=PALETTE["text"],
            font=("Segoe UI Semibold", 12),
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="ew")
        tk.Label(
            self.inspector_summary,
            textvariable=self.meta_path_var,
            bg=PALETTE["surface_1"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 8))

        self.summary_value_vars: dict[str, tk.StringVar] = {
            "Model": tk.StringVar(value="-"),
            "Sampler": tk.StringVar(value="-"),
            "Steps": tk.StringVar(value="-"),
            "CFG": tk.StringVar(value="-"),
            "Resolution": tk.StringVar(value="-"),
            "Size": tk.StringVar(value="-"),
        }
        summary_fields = ["Model", "Sampler", "Steps", "CFG", "Resolution", "Size"]
        for idx, field in enumerate(summary_fields):
            row = 2 + (idx // 3)
            col = idx % 3
            chip = tk.Frame(
                self.inspector_summary,
                bg=PALETTE["chip_soft_bg"],
                highlightthickness=1,
                highlightbackground=PALETTE["accent_muted"],
                bd=0,
                padx=10,
                pady=7,
            )
            chip.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
            self.summary_chip_frames[field] = chip
            self._summary_chip_hovered[field] = False
            title_label = ttk.Label(chip, text=field, style="ChipTitle.TLabel", anchor="w")
            title_label.pack(anchor="w")
            value_label = ttk.Label(chip, textvariable=self.summary_value_vars[field], style="ChipValue.TLabel", anchor="w")
            value_label.pack(anchor="w")
            for widget in (chip, title_label, value_label):
                widget.bind("<Button-1>", lambda _e, f=field: self._copy_summary_value(f), add="+")
                widget.bind("<Enter>", lambda _e, w=widget, f=field: self._on_summary_widget_enter(w, f), add="+")
                widget.bind("<Leave>", lambda _e, w=widget, f=field: self._on_summary_widget_leave(w, f), add="+")

        meta_wrap = tk.Frame(
            self.inspector_panel,
            bg=PALETTE["surface_1"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=0,
            pady=0,
        )
        meta_wrap.grid(row=2, column=0, sticky="nsew", pady=(0, 0))
        meta_wrap.columnconfigure(0, weight=1)
        meta_wrap.rowconfigure(1, weight=1)

        tk.Label(
            meta_wrap,
            text="Metadata details",
            bg=PALETTE["surface_1"],
            fg=PALETTE["muted"],
            font=("Segoe UI Semibold", 10),
            padx=14,
            pady=10,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        self.meta_text = tk.Text(
            meta_wrap,
            wrap="word",
            font=("Segoe UI", 10),
            padx=14,
            pady=14,
            bg=PALETTE["surface_1"],
            fg=PALETTE["text"],
            insertbackground=PALETTE["text"],
            relief="flat",
            highlightthickness=0,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["focus_ring"],
        )
        self.meta_scroll = ttk.Scrollbar(meta_wrap, orient="vertical", command=self.meta_text.yview)
        self.meta_text.configure(yscrollcommand=self.meta_scroll.set)

        self.meta_text.grid(row=1, column=0, sticky="nsew")
        self.meta_scroll.grid(row=1, column=1, sticky="ns")
        self._configure_metadata_tags()
        self.meta_text.configure(state="disabled")

        self.review_panel = tk.Frame(right, bg=PALETTE["panel"])
        self.review_panel.columnconfigure(0, weight=1)
        self.review_panel.rowconfigure(0, weight=1)
        self.inline_preview = InlinePreviewPane(self.review_panel)
        self.inline_preview.grid(row=0, column=0, sticky="nsew")

        self._sync_current_controls(None, None)
        self._set_metadata_text(METADATA_EMPTY_TEXT)
        self.bind("<KeyPress-r>", lambda _e: self._on_review_hotkey("reject"))
        self.bind("<KeyPress-u>", lambda _e: self._on_review_hotkey("unreviewed"))
        self.bind("<KeyPress-f>", lambda _e: self._on_favorite_hotkey())
        self.bind("<KeyPress-F>", lambda _e: self._on_favorite_hotkey())
        self.bind("<KeyPress-v>", lambda _e: self._on_review_mode_hotkey())
        self._sync_review_mode_ui()

    def add_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select a folder with images")
        if not selected:
            return

        path = Path(selected)
        if path not in self.selected_dirs:
            self.selected_dirs.append(path)
            self._refresh_folder_rows()
            self._set_status(f"Folders added: {len(self.selected_dirs)}", "info")
            self._schedule_save_state()

    def _toggle_folders_panel(self) -> None:
        self.folders_panel_visible = not self.folders_panel_visible
        self._apply_folders_panel_visibility()
        self._schedule_save_state()

    def _apply_folders_panel_visibility(self) -> None:
        if hasattr(self, "folders_panel"):
            if self.folders_panel_visible:
                self.folders_panel.grid()
            else:
                self.folders_panel.grid_remove()
        if hasattr(self, "folders_toggle_btn"):
            self.folders_toggle_btn.configure(
                text="[F] Hide folders" if self.folders_panel_visible else "[F] Show folders"
            )

    def clear_folders(self) -> None:
        self._scan_token += 1
        self.selected_dirs.clear()
        self._refresh_folder_rows()
        self._set_status("Folder list cleared", "info")
        self._schedule_save_state()
        self._cancel_metadata_warmup()

        self.all_image_paths = []
        self.image_paths = []
        self.image_root_map = {}
        self.file_mtime_cache = {}
        self._render_thumbnails([])

    def _remove_folder(self, path: Path) -> None:
        if path not in self.selected_dirs:
            return
        self.selected_dirs = [p for p in self.selected_dirs if p != path]
        self._refresh_folder_rows()
        self._set_status(f"Folder removed: {path}", "info")
        self._schedule_save_state()
        if self.selected_dirs:
            self.scan_images()
        else:
            self._scan_token += 1
            self._cancel_metadata_warmup()
            self.all_image_paths = []
            self.image_paths = []
            self.image_root_map = {}
            self.file_mtime_cache = {}
            self._render_thumbnails([])

    def _refresh_folder_rows(self) -> None:
        for child in self.folders_container.winfo_children():
            child.destroy()

        if not self.selected_dirs:
            tk.Label(
                self.folders_container,
                text="No folders selected",
                bg=PALETTE["surface_1"],
                fg=PALETTE["muted"],
                font=("Segoe UI", 10),
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            return

        for idx, path in enumerate(self.selected_dirs):
            row = tk.Frame(self.folders_container, bg=PALETTE["surface_1"], bd=0)
            row.grid(row=idx, column=0, sticky="ew", pady=1)
            row.columnconfigure(0, weight=1)

            tk.Label(
                row,
                text=str(path),
                bg=PALETTE["surface_1"],
                fg=PALETTE["text"],
                font=("Consolas", 10),
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=(2, 8))

            remove_btn = ttk.Button(
                row,
                text="[x]",
                style="MiniSoft.TButton",
                command=lambda p=path: self._remove_folder(p),
                width=3,
            )
            remove_btn.grid(row=0, column=1, sticky="e")

    def _set_status(self, text: str, kind: str = "info") -> None:
        self.status_var.set(text)
        color_map = {
            "info": PALETTE["status_info"],
            "busy": PALETTE["status_busy"],
            "ok": PALETTE["status_ok"],
            "warn": PALETTE["status_warn"],
            "error": PALETTE["status_error"],
        }
        color = color_map.get(kind, PALETTE["status_info"])
        if hasattr(self, "gallery_status"):
            self.gallery_status_var.set(f"* {text}")
            self.gallery_status.configure(fg=color if kind in {"warn", "error"} else PALETTE["muted"])

    def _load_state(self) -> None:
        data, error = self._state_store.load()
        if error:
            self._set_status(error, "warn")
        if not data:
            return
        dirs_raw = data.get("selected_dirs", [])
        if isinstance(dirs_raw, list):
            for item in dirs_raw:
                p = Path(str(item))
                if p.exists() and p.is_dir() and p not in self.selected_dirs:
                    self.selected_dirs.append(p)

        ui = data.get("ui", {})
        if isinstance(ui, dict):
            columns = str(ui.get("columns", DEFAULT_COLUMNS))
            thumb_size = ui.get("thumb_size", DEFAULT_THUMB_SIZE)
            sort_mode = str(ui.get("sort", "Newest"))
            review_mode = str(ui.get("review_filter", "All"))
            panel_visible = ui.get("folders_panel_visible", True)
            if columns.isdigit():
                self.columns_var.set(str(max(2, min(12, int(columns)))))
            if isinstance(thumb_size, int):
                self.thumb_size_var.set(float(max(100, min(320, thumb_size))))
            if sort_mode in {"Newest", "Oldest"}:
                self.sort_var.set(sort_mode)
            if review_mode in REVIEW_STATUSES:
                self.review_filter_var.set(review_mode)
            if isinstance(panel_visible, bool):
                self.folders_panel_visible = panel_visible

        raw = data.get("images", {})
        if isinstance(raw, dict):
            loaded_state: dict[str, dict[str, object]] = {}
            for path_str, item in raw.items():
                state_item = self._deserialize_image_state_item(item)
                if state_item is not None:
                    loaded_state[path_str] = state_item
            self.image_state = loaded_state

    def _deserialize_image_state_item(self, item: object) -> dict[str, object] | None:
        if not isinstance(item, dict):
            return None
        favorite = bool(item.get("favorite", False))
        tags_raw = item.get("tags", [])
        tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
        review_status = str(item.get("review_status", "")).strip().lower()
        state_item: dict[str, object] = {"favorite": favorite, "tags": tags}
        # Legacy migration: old "keep" review marks now map to favorites.
        if review_status == "keep":
            state_item["favorite"] = True
        elif review_status == "reject":
            state_item["review_status"] = review_status
        return state_item

    def _save_state(self) -> None:
        self._prune_image_state()
        payload = {
            "selected_dirs": [str(p) for p in self.selected_dirs],
            "ui": {
                "columns": self._safe_columns(),
                "thumb_size": int(round(self.thumb_size_var.get())),
                "sort": self.sort_var.get(),
                "review_filter": self.review_filter_var.get(),
                "folders_panel_visible": bool(self.folders_panel_visible),
            },
            "images": self.image_state,
        }
        error = self._state_store.save(payload)
        if error:
            self._set_status(error, "warn")

    def _schedule_save_state(self) -> None:
        if self._save_state_after_id is not None:
            self.after_cancel(self._save_state_after_id)
        self._save_state_after_id = self.after(STATE_SAVE_DEBOUNCE_MS, self._flush_scheduled_state_save)

    def _flush_scheduled_state_save(self) -> None:
        self._save_state_after_id = None
        self._save_state()

    def _get_image_state(self, path: Path, create: bool = False) -> dict[str, object]:
        key = str(path)
        if create and key not in self.image_state:
            self.image_state[key] = {"favorite": False, "tags": []}
        return self.image_state.get(key, {})

    def _prune_image_state(self, existing_keys: set[str] | None = None) -> None:
        self.image_state = ImageStateHelper.prune_image_state(self.image_state, existing_keys)

    @staticmethod
    def _normalized_review_status(value: object) -> str:
        return ImageStateHelper.normalized_review_status(value)

    def _get_review_status(self, path: Path | None) -> str:
        if path is None:
            return "unreviewed"
        return self._normalized_review_status(self._get_image_state(path).get("review_status", ""))

    @staticmethod
    def _normalize_tags(tags_raw: object) -> list[str]:
        return ImageStateHelper.normalize_tags(tags_raw)

    @staticmethod
    def _metadata_signature(path: Path) -> tuple[int, int, int]:
        return CatalogHelper.metadata_signature(path, METADATA_PARSE_REV)

    def _get_search_index_record(self, path: Path) -> tuple[str, str]:
        key = str(path)
        state = self.image_state.get(key, {})
        tags = self._normalize_tags(state.get("tags", []))
        tags_sig = "|".join(tags)
        mtime_ns, size, rev = self._metadata_signature(path)
        sig = (mtime_ns, size, rev, tags_sig)

        with self._cache_lock:
            cached = self.search_index_cache.get(key)
            if cached is not None and cached[0] == sig:
                return cached[1], cached[2]

        metadata = self._get_metadata_cached(path)
        prompt_text, search_text = CatalogHelper.build_search_record(path, metadata, tags)

        with self._cache_lock:
            self.search_index_cache[key] = (sig, prompt_text, search_text)
        return prompt_text, search_text

    def _invalidate_search_index(self, path: Path) -> None:
        key = str(path)
        with self._cache_lock:
            self.search_index_cache.pop(key, None)

    def _cancel_metadata_warmup(self) -> None:
        self._metadata_warmup_token += 1

    def _start_metadata_warmup(self, paths: list[Path]) -> None:
        if not paths:
            return
        self._metadata_warmup_token += 1
        token = self._metadata_warmup_token
        snapshot = list(paths)
        self._metadata_warmup_thread = threading.Thread(
            target=self._metadata_warmup_worker,
            args=(snapshot, token),
            daemon=True,
        )
        self._metadata_warmup_thread.start()

    def _metadata_warmup_worker(self, paths: list[Path], token: int) -> None:
        batch = 0
        for path in paths:
            if token != self._metadata_warmup_token:
                return
            try:
                self._get_search_index_record(path)
            except Exception:
                continue
            batch += 1
            if batch >= METADATA_WARMUP_BATCH:
                batch = 0
                if token != self._metadata_warmup_token:
                    return

    @staticmethod
    def _best_root_for_path(path: Path, roots: list[Path]) -> Path | None:
        return CatalogHelper.best_root_for_path(path, roots)

    def _subfolder_hint(self, path: Path) -> tuple[str | None, str | None]:
        key = str(path)
        root = self.image_root_map.get(key)
        if root is None:
            root = self._best_root_for_path(path, self._scan_roots_snapshot or self.selected_dirs)
            if root is not None:
                self.image_root_map[key] = root
        if root is None:
            return None, None
        try:
            rel_parent = path.parent.relative_to(root).as_posix()
        except Exception:
            return None, None
        if not rel_parent or rel_parent == ".":
            return None, None
        label = f"{root.name}/{rel_parent}"
        color_key = f"{root}|{rel_parent.lower()}"
        return label, color_key

    @staticmethod
    def _dot_color_for_key(color_key: str) -> str:
        if not color_key:
            return PALETTE["accent_active"]
        idx = zlib.crc32(color_key.encode("utf-8")) % len(SUBFOLDER_DOT_COLORS)
        return SUBFOLDER_DOT_COLORS[idx]

    def _show_folder_tooltip(self, text: str, x_root: int, y_root: int) -> None:
        if not text:
            return
        if self._folder_tip_win is None or not self._folder_tip_win.winfo_exists():
            tip = tk.Toplevel(self)
            tip.withdraw()
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            label = tk.Label(
                tip,
                text=text,
                bg=PALETTE["surface"],
                fg=PALETTE["text"],
                relief="solid",
                bd=1,
                padx=8,
                pady=4,
                font=("Segoe UI", 9),
            )
            label.pack()
            self._folder_tip_win = tip
            self._folder_tip_label = label
        if self._folder_tip_label is not None:
            self._folder_tip_label.configure(text=text)
        if self._folder_tip_win is not None:
            self._folder_tip_win.geometry(f"+{x_root + 14}+{y_root + 14}")
            self._folder_tip_win.deiconify()

    def _hide_folder_tooltip(self) -> None:
        if self._folder_tip_win is not None and self._folder_tip_win.winfo_exists():
            self._folder_tip_win.withdraw()

    def _clear_filters(self) -> None:
        self.search_var.set("")
        self.tag_filter_var.set("")
        self.favorites_only_var.set(False)
        self.review_filter_var.set("All")
        self.sort_var.set("Newest")
        self.apply_filters()

    def _show_empty_metadata_state(self) -> None:
        self._set_metadata_text(METADATA_EMPTY_TEXT)

    def _show_empty_preview_state(self, message: str = PREVIEW_EMPTY_TEXT) -> None:
        if self.review_mode_var.get():
            self.inline_preview.clear(message)

    def _schedule_apply_filters(self) -> None:
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(220, self.apply_filters)

    def apply_filters(self, preserve_view: bool = False) -> None:
        self._filter_after_id = None
        if not self.all_image_paths:
            self._filter_token += 1
            self._render_thumbnails([], preserve_view=preserve_view)
            return

        query = self.search_var.get().strip().lower()
        tag_filter = self.tag_filter_var.get().strip().lower()
        favorites_only = self.favorites_only_var.get()
        review_filter = self.review_filter_var.get().strip()
        sort_mode = self.sort_var.get()
        self._filter_token += 1
        token = self._filter_token
        if query or tag_filter:
            self._cancel_metadata_warmup()
            self._set_status("Filtering images...", "busy")

        self._filter_worker_thread = threading.Thread(
            target=self._filter_worker,
            args=(token, list(self.all_image_paths), query, tag_filter, favorites_only, review_filter, sort_mode, preserve_view),
            daemon=True,
        )
        self._filter_worker_thread.start()

    def _filter_worker(
        self,
        token: int,
        paths: list[Path],
        query: str,
        tag_filter: str,
        favorites_only: bool,
        review_filter: str,
        sort_mode: str,
        preserve_view: bool,
    ) -> None:
        filtered, cancelled = CatalogHelper.filter_paths(
            paths,
            query=query,
            tag_filter=tag_filter,
            favorites_only=favorites_only,
            review_filter=review_filter,
            sort_mode=sort_mode,
            get_state=self._get_image_state,
            get_review_status=self._get_review_status,
            get_search_record=self._get_search_index_record,
            sort_key=self._sort_mtime_cached,
            should_cancel=lambda: token != self._filter_token,
        )
        if cancelled:
            return
        self._post_to_ui(lambda: self._on_filters_ready(token, filtered, preserve_view))

    def _on_filters_ready(self, token: int, filtered: list[Path], preserve_view: bool) -> None:
        if token != self._filter_token:
            return
        self._render_thumbnails(filtered, preserve_view=preserve_view)

    def scan_images(self) -> None:
        if not self.selected_dirs:
            messagebox.showinfo("No folders", "Add at least one folder first")
            return

        self._cancel_metadata_warmup()
        self._scan_token += 1
        token = self._scan_token
        self._scan_roots_snapshot = list(self.selected_dirs)
        self._set_status("Scanning...", "busy")
        threading.Thread(target=self._scan_worker, args=(token,), daemon=True).start()

    def _scan_worker(self, token: int) -> None:
        try:
            roots = list(self._scan_roots_snapshot or self.selected_dirs)
            found, root_by_key, mtime_by_key, cancelled = CatalogHelper.scan_image_roots(
                roots,
                SUPPORTED_EXTENSIONS,
                should_cancel=lambda: token != self._scan_token,
            )
            if cancelled or token != self._scan_token:
                return
            self._post_to_ui(lambda: self._on_scan_complete(token, found, root_by_key, mtime_by_key))
        except Exception as exc:  # noqa: BLE001
            if token != self._scan_token:
                return
            self._post_to_ui(lambda: self._set_status(f"Scan error: {exc}", "error"))

    def _on_scan_complete(
        self,
        token: int,
        found: list[Path],
        root_by_key: dict[str, Path],
        mtime_by_key: dict[str, float],
    ) -> None:
        if token != self._scan_token:
            return
        self.all_image_paths = found
        self.image_root_map = {}
        for path in found:
            root = root_by_key.get(str(path))
            if root is not None:
                self.image_root_map[str(path)] = root
        # Keep cache entries only for existing files.
        existing = {str(p) for p in found}
        self._prune_image_state()
        self.file_mtime_cache = {key: float(value) for key, value in mtime_by_key.items() if key in existing}
        with self._cache_lock:
            (
                self.metadata_cache,
                self.metadata_cache_sig,
                self.search_index_cache,
                self.thumbnail_cache,
            ) = CatalogHelper.prune_runtime_caches(
                existing,
                self.metadata_cache,
                self.metadata_cache_sig,
                self.search_index_cache,
                self.thumbnail_cache,
            )
        self.apply_filters()
        self._start_metadata_warmup(found)

    def on_thumb_size_changed(self, _value: str) -> None:
        if self._thumb_render_after_id is not None:
            self.after_cancel(self._thumb_render_after_id)
        self._thumb_render_after_id = self.after(THUMB_SIZE_DEBOUNCE_MS, lambda: self._apply_thumb_size_change(force=False))

    def _apply_thumb_size_change(self, force: bool) -> None:
        self._thumb_render_after_id = None
        if not self.image_paths:
            return
        size = max(100, min(320, int(round(self.thumb_size_var.get()))))
        if not force and size == self._thumb_last_applied_size:
            return
        self._thumb_last_applied_size = size
        self._render_thumbnails(self.image_paths, preserve_view=True)
        self._schedule_save_state()

    def on_layout_changed(self) -> None:
        if self._column_render_after_id is not None:
            self.after_cancel(self._column_render_after_id)
        self._column_render_after_id = self.after(10, self._apply_layout_change)

    def _apply_layout_change(self) -> None:
        self._column_render_after_id = None
        value = self.columns_var.get().strip()
        if not value:
            return
        if not value.isdigit():
            self.columns_var.set(str(DEFAULT_COLUMNS))
            self._thumb_last_applied_columns = DEFAULT_COLUMNS
            if self.image_paths:
                self._render_thumbnails(self.image_paths, preserve_view=True)
                self._schedule_save_state()
            return

        clamped = max(2, min(12, int(value)))
        if value != str(clamped):
            self.columns_var.set(str(clamped))
        if clamped == self._thumb_last_applied_columns:
            return
        self._thumb_last_applied_columns = clamped

        if self.image_paths:
            self._render_thumbnails(self.image_paths, preserve_view=True)
            self._schedule_save_state()

    def _render_thumbnails(self, paths: list[Path], preserve_view: bool = False) -> None:
        self._thumb_render_token += 1
        token = self._thumb_render_token
        self._thumb_render_preserve_view = preserve_view
        self._thumb_render_view = (
            self.canvas.xview()[0] if preserve_view else 0.0,
            self.canvas.yview()[0] if preserve_view else 0.0,
        )
        self._thumb_render_paths = list(paths)
        self._thumb_render_selected_path = self.current_image_path

        if self._thumb_batch_after_id is not None:
            self.after_cancel(self._thumb_batch_after_id)
            self._thumb_batch_after_id = None

        self._start_thumbnail_render(token)

    def _start_thumbnail_render(self, token: int) -> None:
        if token != self._thumb_render_token:
            return

        self._hide_folder_tooltip()
        self.canvas.delete("thumb_item")

        self.thumbnail_refs.clear()
        self.thumb_cells_by_path.clear()
        self.thumb_inner_by_path.clear()
        self.thumb_widget_by_path.clear()
        self._hover_path_key = None
        self.image_paths = self._thumb_render_paths
        self._thumb_rendering = True
        self._thumb_batch_counter = 0

        if not self.image_paths:
            total = len(self.all_image_paths)
            self._set_status(f"No images found for current filter | Total indexed: {total}", "warn")
            self._set_metadata_text(NO_MATCH_TEXT)
            self.current_image_path = None
            self._sync_current_controls(None, None)
            self._show_empty_preview_state("Nothing matched this filter.")
            self._thumb_rendering = False
            self._update_canvas_window_size()
            return

        self._thumb_render_size = max(100, min(320, int(round(self.thumb_size_var.get()))))
        self._thumb_render_columns = self._safe_columns()
        self._thumb_name_font = tkfont.Font(family="Segoe UI", size=9)
        name_line_h = int(self._thumb_name_font.metrics("linespace"))
        self._thumb_show_caption = self._thumb_render_size >= 150
        self._thumb_caption_height = max(22, name_line_h + 8) if self._thumb_show_caption else 0
        self._thumb_inner_pad = 8
        self._thumb_cell_width = self._thumb_render_size + 22
        self._thumb_cell_height = (
            self._thumb_render_size
            + (self._thumb_inner_pad * 2)
            + self._thumb_caption_height
            + 6
        )
        self._thumb_cell_gap_x = 10
        self._thumb_cell_gap_y = 10
        self._thumb_render_index = 0
        self._thumb_decode_token = token
        with self._thumb_prepare_lock:
            self._thumb_prepared_images = {}
        self._render_skeleton_grid(len(self.image_paths))
        self._thumb_decode_thread = threading.Thread(
            target=self._decode_thumbnail_worker,
            args=(token, list(self.image_paths), self._thumb_render_size),
            daemon=True,
        )
        self._thumb_decode_thread.start()
        self._set_status(f"Rendering thumbnails: 0 / {len(self.image_paths)} ...", "busy")
        self._render_thumbnail_batch(token)

    def _render_thumbnail_batch(self, token: int) -> None:
        if token != self._thumb_render_token:
            return

        size = self._thumb_render_size
        columns = self._thumb_render_columns
        total_count = len(self.image_paths)

        for _ in range(THUMB_RENDER_BATCH):
            if self._thumb_render_index >= total_count:
                break
            idx = self._thumb_render_index
            path = self.image_paths[idx]
            cache_key = self._thumbnail_cache_key(path, size)
            thumb_img = self._get_cached_thumbnail(cache_key)
            if thumb_img is None:
                prepared = self._pop_prepared_thumbnail(idx)
                if prepared is None:
                    break
                prepared_key, prepared_image = prepared
                thumb_img = ImageTk.PhotoImage(prepared_image)
                self._store_thumbnail(prepared_key, thumb_img)
            row = idx // columns
            col = idx % columns
            self._create_thumbnail_cell(path, idx, row, col, size, thumb_img)
            self._thumb_render_index += 1

        self._thumb_batch_counter += 1
        if self._thumb_batch_counter % THUMB_LAYOUT_UPDATE_EVERY_BATCHES == 0 or self._thumb_render_index >= total_count:
            self._update_canvas_window_size()
        self._set_status(
            f"Rendering thumbnails: {self._thumb_render_index} / {total_count} | Sort: {self.sort_var.get()} | Columns: {columns} | Thumb size: {size}px",
            "busy",
        )

        if self._thumb_render_index < total_count:
            delay = 1 if self._thumb_render_index > 0 else 12
            self._thumb_batch_after_id = self.after(delay, lambda: self._render_thumbnail_batch(token))
            return

        self._thumb_batch_after_id = None
        self._finalize_thumbnail_render()

    def _finalize_thumbnail_render(self) -> None:
        self._thumb_rendering = False
        total = len(self.all_image_paths)
        size = self._thumb_render_size
        columns = self._thumb_render_columns
        self._set_status(
            f"Showing: {len(self.image_paths)} / {total} | Sort: {self.sort_var.get()} | Columns: {columns} | Thumb size: {size}px",
            "ok",
        )

        if self._thumb_render_preserve_view:
            x, y = self._thumb_render_view
            self.canvas.xview_moveto(x)
            self.canvas.yview_moveto(y)
        else:
            self.canvas.yview_moveto(0)
            self.canvas.xview_moveto(0)

        selected_path = self._thumb_render_selected_path
        if self._pending_focus_path and self._pending_focus_path in self.image_paths:
            idx = self.image_paths.index(self._pending_focus_path)
            self._pending_focus_path = None
            self.on_thumbnail_click(idx)
        elif selected_path and selected_path in self.image_paths:
            idx = self.image_paths.index(selected_path)
            self.on_thumbnail_click(idx)
        else:
            self._pending_focus_path = None
            self.current_image_path = None
            self._sync_current_controls(None, None)
            self._show_empty_metadata_state()
            self._show_empty_preview_state()

        self._refresh_thumb_cell_highlight()

    def _create_thumbnail_cell(
        self,
        path: Path,
        idx: int,
        row: int,
        col: int,
        size: int,
        thumb_img: ImageTk.PhotoImage,
    ) -> None:
        self.canvas.delete(f"skel_idx_{idx}")
        x0 = col * (self._thumb_cell_width + self._thumb_cell_gap_x)
        y0 = row * (self._thumb_cell_height + self._thumb_cell_gap_y)
        x1 = x0 + self._thumb_cell_width
        y1 = y0 + self._thumb_cell_height
        ix0 = x0 + 1
        iy0 = y0 + 1
        ix1 = x1 - 1
        iy1 = y1 - 1
        pad = int(getattr(self, "_thumb_inner_pad", 6))
        caption_h = int(getattr(self, "_thumb_caption_height", 24))
        tag = f"thumb_idx_{idx}"
        tags = ("thumb_item", tag)

        shell = self.canvas.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            fill=PALETTE["thumb_shadow"],
            outline=PALETTE["surface_3"],
            width=1,
            tags=tags,
        )
        cell = self.canvas.create_rectangle(
            ix0,
            iy0,
            ix1,
            iy1,
            fill=PALETTE["thumb_bg"],
            outline=PALETTE["border"],
            width=1,
            tags=tags,
        )
        self.thumb_cells_by_path[str(path)] = shell
        self.thumb_inner_by_path[str(path)] = cell

        img_x = (ix0 + ix1) / 2
        img_y = iy0 + pad + (size / 2)
        image_item = self.canvas.create_image(img_x, img_y, image=thumb_img, anchor="center", tags=tags)

        text_item: int | None = None
        if caption_h > 0:
            caption_width = max(36, self._thumb_cell_width - 12)
            font_obj = getattr(self, "_thumb_name_font", tkfont.Font(family="Segoe UI", size=10))
            caption_text = self._truncate_to_pixel_width(path.name, caption_width, font_obj)
            text_item = self.canvas.create_text(
                img_x,
                iy0 + pad + size + (caption_h / 2),
                text=caption_text,
                fill=PALETTE["muted"],
                font=font_obj,
                anchor="center",
                tags=tags,
            )
        self.thumb_widget_by_path[str(path)] = (image_item, text_item)

        state = self._get_image_state(path)
        if bool(state.get("favorite", False)):
            self.canvas.create_text(
                ix0 + 8,
                iy0 + 8,
                text="*",
                fill=PALETTE["accent_active"],
                font=("Segoe UI Semibold", 12),
                anchor="nw",
                tags=tags,
            )

        folder_hint, color_key = self._subfolder_hint(path)
        if folder_hint and color_key:
            dot_color = self._dot_color_for_key(color_key)
            dot_tag = f"{tag}_dot"
            dot_x = ix1 - 10
            dot_y = iy0 + 10
            shadow = self._mix_hex(PALETTE["text"], PALETTE["panel"], 0.58)
            ring = "#ffffff"
            core_outline = self._mix_hex(dot_color, "#2a1e3f", 0.34)
            highlight = self._mix_hex("#ffffff", dot_color, 0.24)

            # Compact, high-contrast marker: thin shadow + white ring + colored core.
            self.canvas.create_oval(
                dot_x - 5 + 1,
                dot_y - 5 + 1,
                dot_x + 5 + 1,
                dot_y + 5 + 1,
                fill="",
                outline=shadow,
                width=1,
                tags=("thumb_item", tag, dot_tag),
            )
            self.canvas.create_oval(
                dot_x - 5,
                dot_y - 5,
                dot_x + 5,
                dot_y + 5,
                fill="",
                outline=ring,
                width=2,
                tags=("thumb_item", tag, dot_tag),
            )
            self.canvas.create_oval(
                dot_x - 3,
                dot_y - 3,
                dot_x + 3,
                dot_y + 3,
                fill=dot_color,
                outline=core_outline,
                width=1,
                tags=("thumb_item", tag, dot_tag),
            )
            self.canvas.create_oval(
                dot_x - 1,
                dot_y - 1,
                dot_x + 0,
                dot_y + 0,
                fill=highlight,
                outline=highlight,
                width=1,
                tags=("thumb_item", tag, dot_tag),
            )
            self.canvas.tag_bind(
                dot_tag,
                "<Enter>",
                lambda e, text=folder_hint: self._show_folder_tooltip(text, e.x_root, e.y_root),
            )
            self.canvas.tag_bind(
                dot_tag,
                "<Motion>",
                lambda e, text=folder_hint: self._show_folder_tooltip(text, e.x_root, e.y_root),
            )
            self.canvas.tag_bind(dot_tag, "<Leave>", lambda _e: self._hide_folder_tooltip())

        self.canvas.tag_bind(tag, "<Button-1>", lambda _evt, i=idx: self.on_thumbnail_click(i))
        self.canvas.tag_bind(tag, "<Double-Button-1>", lambda _evt, i=idx: self.open_full_preview(i))
        self.canvas.tag_bind(tag, "<Enter>", lambda _evt, p=path: self._on_thumb_hover(p, True))
        self.canvas.tag_bind(tag, "<Leave>", lambda _evt, p=path: self._on_thumb_hover(p, False))

        self._apply_thumb_visual_state(path, hovering=False)
        self.thumbnail_refs.append(thumb_img)

    def _render_skeleton_grid(self, total_count: int) -> None:
        if total_count <= 0:
            return
        columns = max(1, self._thumb_render_columns)
        # Keep skeleton lightweight: render for initial viewport area only.
        max_skeleton = min(total_count, columns * 18)
        pad = int(getattr(self, "_thumb_inner_pad", 6))
        caption_h = int(getattr(self, "_thumb_caption_height", 0))
        size = int(self._thumb_render_size)
        for idx in range(max_skeleton):
            row = idx // columns
            col = idx % columns
            x0 = col * (self._thumb_cell_width + self._thumb_cell_gap_x)
            y0 = row * (self._thumb_cell_height + self._thumb_cell_gap_y)
            x1 = x0 + self._thumb_cell_width
            y1 = y0 + self._thumb_cell_height
            sx0 = x0 + 1
            sy0 = y0 + 1
            sx1 = x1 - 1
            sy1 = y1 - 1
            skel_tag = f"skel_idx_{idx}"
            tags = ("thumb_item", "thumb_skeleton", skel_tag)
            base_fill = PALETTE["skeleton_base"] if (idx % 2 == 0) else self._mix_hex(PALETTE["skeleton_base"], PALETTE["surface_2"], 0.35)
            self.canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                fill=PALETTE["thumb_shadow"],
                outline=PALETTE["thumb_shadow"],
                width=1,
                tags=tags,
            )
            self.canvas.create_rectangle(
                sx0,
                sy0,
                sx1,
                sy1,
                fill=base_fill,
                outline=PALETTE["border"],
                width=1,
                tags=tags,
            )
            self.canvas.create_rectangle(
                sx0 + pad,
                sy0 + pad,
                sx1 - pad,
                sy0 + pad + size,
                fill=PALETTE["skeleton_shine"],
                outline="",
                tags=tags,
            )
            if caption_h > 0:
                self.canvas.create_rectangle(
                    sx0 + pad,
                    sy0 + pad + size + 6,
                    sx1 - pad,
                    sy0 + pad + size + max(8, caption_h - 4),
                    fill=PALETTE["skeleton_shine"],
                    outline="",
                    tags=tags,
                )

    def _update_canvas_window_size(self) -> None:
        columns = max(1, getattr(self, "_thumb_render_columns", self._safe_columns()))
        cell_w = int(getattr(self, "_thumb_cell_width", max(116, int(round(self.thumb_size_var.get())) + 16)))
        cell_h = int(getattr(self, "_thumb_cell_height", max(144, int(round(self.thumb_size_var.get())) + 40)))
        gap_x = int(getattr(self, "_thumb_cell_gap_x", 3))
        gap_y = int(getattr(self, "_thumb_cell_gap_y", 3))
        count = len(self.image_paths)
        rows = (count + columns - 1) // columns if count else 0
        total_w = max(1, columns * (cell_w + gap_x) - gap_x)
        total_h = max(1, rows * (cell_h + gap_y) - gap_y)
        view_w = max(1, self.canvas.winfo_width())
        view_h = max(1, self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, max(total_w, view_w), max(total_h, view_h)))

    def _thumbnail_cache_key(self, path: Path, size: int) -> tuple[str, int, int, int]:
        sig_mtime, sig_size = self._file_signature(path)
        return str(path), size, sig_mtime, sig_size

    def _get_cached_thumbnail(self, cache_key: tuple[str, int, int, int]) -> ImageTk.PhotoImage | None:
        with self._cache_lock:
            cached = self.thumbnail_cache.get(cache_key)
            if cached is not None:
                self.thumbnail_cache.move_to_end(cache_key)
            return cached

    def _store_thumbnail(self, cache_key: tuple[str, int, int, int], image: ImageTk.PhotoImage) -> None:
        with self._cache_lock:
            self.thumbnail_cache[cache_key] = image
            self.thumbnail_cache.move_to_end(cache_key)
            while len(self.thumbnail_cache) > THUMB_CACHE_MAX:
                self.thumbnail_cache.popitem(last=False)

    def _prepare_thumbnail_image(self, path: Path, size: int) -> Image.Image:
        target = (size, size)
        try:
            with Image.open(path) as im:
                thumb = ImageOps.exif_transpose(im)
                thumb.thumbnail(target)
                bg = Image.new("RGB", target, (28, 28, 28))
                x = (target[0] - thumb.width) // 2
                y = (target[1] - thumb.height) // 2
                bg.paste(thumb.convert("RGB"), (x, y))
                return bg
        except Exception:
            return Image.new("RGB", target, (80, 40, 40))

    def _decode_thumbnail_worker(self, token: int, paths: list[Path], size: int) -> None:
        for idx, path in enumerate(paths):
            if token != self._thumb_decode_token or token != self._thumb_render_token:
                return
            cache_key = self._thumbnail_cache_key(path, size)
            if self._get_cached_thumbnail(cache_key) is not None:
                continue
            prepared = self._prepare_thumbnail_image(path, size)
            with self._thumb_prepare_lock:
                self._thumb_prepared_images[idx] = (cache_key, prepared)

    def _pop_prepared_thumbnail(
        self,
        idx: int,
    ) -> tuple[tuple[str, int, int, int], Image.Image] | None:
        with self._thumb_prepare_lock:
            return self._thumb_prepared_images.pop(idx, None)

    def on_thumbnail_click(self, index: int) -> None:
        if index < 0 or index >= len(self.image_paths):
            return
        path = self.image_paths[index]
        metadata = self._get_metadata_cached(path)
        self.current_image_path = path
        self._sync_current_controls(path, metadata)
        self._refresh_thumb_cell_highlight()
        if self.review_mode_var.get():
            self.inline_preview.load_path(path)
            return
        panel = self._build_details_view(path, metadata)
        self._set_metadata_text(panel)

    def open_full_preview(self, index: int) -> None:
        if index < 0 or index >= len(self.image_paths):
            return
        path = self.image_paths[index]
        if self.preview_window is None or not self.preview_window.winfo_exists():
            self.preview_window = PreviewWindow(self, path)
            return
        self.preview_window.load_path(path)
        self.preview_window.deiconify()
        self.preview_window.lift()
        self.preview_window.focus_force()

    def _on_thumb_hover(self, path: Path, entering: bool) -> None:
        key = str(path)
        if entering:
            self._hover_path_key = key
            self.canvas.configure(cursor="hand2")
        else:
            if self._hover_path_key == key:
                self._hover_path_key = None
                self.canvas.configure(cursor="")
        self._apply_thumb_visual_state(path, hovering=entering)

    def _refresh_thumb_cell_highlight(self) -> None:
        for key in self.thumb_cells_by_path:
            self._apply_thumb_visual_state(Path(key), hovering=(key == self._hover_path_key))

    def _review_thumb_palette(self, review_status: str) -> tuple[str, str]:
        return ReviewHelper.review_thumb_palette(review_status, PALETTE)

    def _apply_thumb_visual_state(self, path: Path, hovering: bool) -> None:
        key = str(path)
        shell = self.thumb_cells_by_path.get(key)
        cell = self.thumb_inner_by_path.get(key)
        widgets = self.thumb_widget_by_path.get(key)
        if shell is None or cell is None or not widgets:
            return
        image_item, text_item = widgets
        is_selected = self.current_image_path is not None and key == str(self.current_image_path)
        review_fill, review_outline = self._review_thumb_palette(self._get_review_status(path))

        shell_fill = PALETTE["thumb_shadow"]
        shell_outline = PALETTE["surface_3"]
        cell_fill = review_fill
        cell_outline = review_outline
        cell_width = 1
        text_fill = PALETTE["text"] if self._get_review_status(path) == "reject" else PALETTE["muted"]

        if hovering:
            shell_fill = self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent"], 0.08)
            shell_outline = shell_fill
            cell_fill = self._mix_hex(review_fill, PALETTE["thumb_hover_bg"], 0.48)
            cell_outline = self._mix_hex(review_outline, PALETTE["accent"], 0.26)
            text_fill = PALETTE["text"]

        if is_selected:
            shell_fill = self._mix_hex(shell_fill, PALETTE["accent_active"], 0.12)
            shell_outline = shell_fill
            cell_fill = self._mix_hex(review_fill, PALETTE["thumb_selected_bg"], 0.62)
            cell_outline = PALETTE["accent_active"]
            cell_width = 2
            text_fill = PALETTE["text"]

        self.canvas.itemconfigure(shell, fill=shell_fill, outline=shell_outline)
        self.canvas.itemconfigure(cell, outline=cell_outline, fill=cell_fill, width=cell_width)
        self.canvas.itemconfigure(image_item, state="normal")
        if text_item is not None:
            self.canvas.itemconfigure(text_item, fill=text_fill)

    def copy_current_prompt(self) -> None:
        if not self.current_image_path:
            messagebox.showinfo(NO_IMAGE_SELECTED_TITLE, "Select an image first.")
            return
        metadata = self._get_metadata_cached(self.current_image_path)
        prompt = self._pick(metadata, ["Prompt"])
        if not prompt:
            messagebox.showinfo("Prompt not found", "No positive prompt found in metadata.")
            return
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self._set_status("Positive prompt copied to clipboard", "ok")

    def _copy_summary_value(self, field: str) -> None:
        value_var = self.summary_value_vars.get(field)
        value = value_var.get().strip() if value_var is not None else ""
        if not value or value == "-":
            self._set_status(f"{field}: nothing to copy", "warn")
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self._set_status(f"{field} copied to clipboard", "ok")
        self._flash_summary_chip(field)

    def _on_summary_widget_enter(self, widget: tk.Misc, field: str) -> None:
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        self._on_summary_chip_hover(field, entering=True)

    def _on_summary_widget_leave(self, widget: tk.Misc, field: str) -> None:
        try:
            widget.configure(cursor="")
        except Exception:
            pass
        self._on_summary_chip_hover(field, entering=False)

    def _on_summary_chip_hover(self, field: str, entering: bool) -> None:
        chip = self.summary_chip_frames.get(field)
        if chip is None or not chip.winfo_exists():
            return
        self._summary_chip_hovered[field] = entering
        if field in self._summary_flash_after_ids:
            return
        chip.configure(
            highlightbackground=(self._summary_chip_hover_border if entering else PALETTE["accent_muted"]),
        )

    def _flash_summary_chip(self, field: str) -> None:
        chip = self.summary_chip_frames.get(field)
        if chip is None or not chip.winfo_exists():
            return

        pending = self._summary_flash_after_ids.get(field)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass

        chip.configure(highlightbackground=PALETTE["accent"])

        def restore() -> None:
            self._summary_flash_after_ids.pop(field, None)
            if chip.winfo_exists():
                chip.configure(
                    highlightbackground=(
                        self._summary_chip_hover_border
                        if self._summary_chip_hovered.get(field, False)
                        else PALETTE["accent_muted"]
                    )
                )

        self._summary_flash_after_ids[field] = self.after(240, restore)

    def toggle_current_favorite(self) -> None:
        if not self.current_image_path:
            messagebox.showinfo(NO_IMAGE_SELECTED_TITLE, "Select an image first.")
            return
        current_path = self.current_image_path
        self._pending_focus_path = self._next_review_focus_path(current_path)
        state = self._get_image_state(current_path, create=True)
        state["favorite"] = not bool(state.get("favorite", False))
        is_favorite = bool(state.get("favorite", False))
        self._apply_current_image_state_change(
            current_path,
            status_message=("Favorite added" if is_favorite else "Favorite removed"),
        )

    def save_current_tags(self) -> None:
        if not self.current_image_path:
            messagebox.showinfo(NO_IMAGE_SELECTED_TITLE, "Select an image first.")
            return
        current_path = self.current_image_path
        raw = self.current_tags_var.get()
        tags = [part.strip() for part in raw.split(",") if part.strip()]
        unique = sorted(set(tags), key=lambda s: s.lower())
        state = self._get_image_state(current_path, create=True)
        state["tags"] = unique
        self._apply_current_image_state_change(
            current_path,
            status_message=f"Saved tags: {', '.join(unique) if unique else 'None'}",
            invalidate_search=True,
        )

    def _review_display(self, status: str) -> tuple[str, str, str]:
        return ReviewHelper.review_display(status, PALETTE)

    def _apply_current_image_state_change(
        self,
        path: Path,
        *,
        status_message: str,
        invalidate_search: bool = False,
    ) -> None:
        self._prune_image_state()
        if invalidate_search:
            self._invalidate_search_index(path)
        self._schedule_save_state()
        self._set_status(status_message, "ok")
        self._sync_current_controls(path, self._get_metadata_cached(path))
        self.apply_filters(preserve_view=True)

    def _rejected_paths(self) -> list[Path]:
        return [path for path in self.all_image_paths if self._get_review_status(path) == "reject"]

    def _sync_review_header_ui(self) -> None:
        rejected_count = len(self._rejected_paths())
        target_visible = self.review_mode_var.get() and rejected_count > 0

        if rejected_count != self._delete_rejected_count:
            self.delete_rejected_btn.configure(text=f"Delete Rejected ({rejected_count})")
            self._delete_rejected_count = rejected_count
            self.after_idle(self._finalize_delete_rejected_layout)

        if target_visible != self._delete_rejected_visible:
            if target_visible:
                self.review_delete_row.grid(row=1, column=0, sticky="w", pady=(8, 0))
            else:
                self.review_delete_row.grid_remove()
            self._delete_rejected_visible = target_visible

    def _finalize_delete_rejected_layout(self) -> None:
        try:
            target_width = max(self.review_mode_btn.winfo_reqwidth(), self.delete_rejected_btn.winfo_reqwidth())
        except Exception:
            return
        if target_width <= 0:
            return
        self._delete_rejected_slot_width = target_width
        self.delete_rejected_slot.configure(width=target_width)

    def _next_review_focus_path(self, current_path: Path | None) -> Path | None:
        return ReviewHelper.next_review_focus_path(current_path, self.image_paths)

    def _finalize_review_status(self, path: Path, status: str) -> None:
        state = self._get_image_state(path, create=True)
        normalized = self._normalized_review_status(status)
        if normalized == "unreviewed":
            state.pop("review_status", None)
        else:
            state["review_status"] = normalized
        self._prune_image_state()
        self._invalidate_search_index(path)

    def set_current_review_status(self, status: str) -> None:
        if not self.current_image_path:
            messagebox.showinfo(NO_IMAGE_SELECTED_TITLE, "Select an image first.")
            return
        current_path = self.current_image_path
        self._pending_focus_path = self._next_review_focus_path(current_path)
        self._finalize_review_status(current_path, status)
        label, _bg, _fg = self._review_display(status)
        self._apply_current_image_state_change(
            current_path,
            status_message=f"Review status: {label}",
        )

    def _recycle_bin_delete(self, path: Path) -> bool:
        if os.name != "nt":
            return False
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = str(path) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return result == 0 and not bool(op.fAnyOperationsAborted)

    def _next_path_after_deletion(self, deleted_keys: set[str]) -> Path | None:
        return ReviewHelper.next_path_after_deletion(self.image_paths, self.current_image_path, deleted_keys)

    def delete_rejected_images(self) -> None:
        if self._delete_rejected_in_progress:
            self._set_status("Delete rejected already in progress...", "info")
            return
        rejected = self._rejected_paths()
        if not rejected:
            self._set_status("No rejected images to delete", "warn")
            self._sync_review_header_ui()
            return
        count = len(rejected)
        confirmed = messagebox.askyesno(
            "Delete rejected images",
            f"Delete {count} rejected image{'s' if count != 1 else ''}?\n\nFiles will be moved to Recycle Bin.",
            icon="warning",
        )
        if not confirmed:
            return
        self._delete_rejected_in_progress = True
        self.delete_rejected_btn.configure(state="disabled")
        self._set_status(f"Deleting {count} rejected image{'s' if count != 1 else ''}...", "busy")
        threading.Thread(
            target=self._delete_rejected_worker,
            args=(list(rejected),),
            daemon=True,
        ).start()

    def _delete_rejected_worker(self, rejected: list[Path]) -> None:
        deleted: list[Path] = []
        failed: list[Path] = []
        for path in rejected:
            if self._recycle_bin_delete(path):
                deleted.append(path)
            else:
                failed.append(path)
        self._post_to_ui(lambda: self._on_delete_rejected_complete(deleted, failed))

    def _on_delete_rejected_complete(self, deleted: list[Path], failed: list[Path]) -> None:
        self._delete_rejected_in_progress = False
        if hasattr(self, "delete_rejected_btn") and self.delete_rejected_btn.winfo_exists():
            self.delete_rejected_btn.configure(state="normal")
        if not deleted:
            messagebox.showerror("Delete failed", "Could not move rejected images to Recycle Bin.")
            self._set_status("Delete rejected failed", "error")
            return

        deleted_keys = {str(path) for path in deleted}
        self._pending_focus_path = self._next_path_after_deletion(deleted_keys)
        self.all_image_paths = [path for path in self.all_image_paths if str(path) not in deleted_keys]
        self.image_paths = [path for path in self.image_paths if str(path) not in deleted_keys]
        self.image_root_map = {key: value for key, value in self.image_root_map.items() if key not in deleted_keys}
        self.file_mtime_cache = {key: value for key, value in self.file_mtime_cache.items() if key not in deleted_keys}
        for key in deleted_keys:
            self.image_state.pop(key, None)
        with self._cache_lock:
            self.metadata_cache = {k: v for k, v in self.metadata_cache.items() if k not in deleted_keys}
            self.metadata_cache_sig = {k: v for k, v in self.metadata_cache_sig.items() if k not in deleted_keys}
            self.search_index_cache = {k: v for k, v in self.search_index_cache.items() if k not in deleted_keys}
            self.thumbnail_cache = OrderedDict((k, v) for k, v in self.thumbnail_cache.items() if k[0] not in deleted_keys)
        if self.current_image_path is not None and str(self.current_image_path) in deleted_keys:
            self.current_image_path = None
        self._schedule_save_state()
        self.apply_filters(preserve_view=True)
        if failed:
            self._set_status(f"Deleted {len(deleted)} rejected, failed {len(failed)}", "warn")
            messagebox.showwarning(
                "Partial delete",
                f"Moved {len(deleted)} rejected image{'s' if len(deleted) != 1 else ''} to Recycle Bin.\n"
                f"Could not delete {len(failed)} file{'s' if len(failed) != 1 else ''}.",
            )
        else:
            self._set_status(f"Deleted {len(deleted)} rejected image{'s' if len(deleted) != 1 else ''}", "ok")

    def _focus_is_text_input(self) -> bool:
        widget = self.focus_get()
        if widget is None:
            return False
        return widget.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}

    def _on_review_hotkey(self, status: str) -> None:
        if self._focus_is_text_input():
            return
        self.set_current_review_status(status)

    def _on_favorite_hotkey(self) -> None:
        if self._focus_is_text_input():
            return
        self.toggle_current_favorite()

    def _on_review_mode_hotkey(self) -> None:
        if self._focus_is_text_input():
            return
        self.toggle_review_mode()

    def toggle_review_mode(self) -> None:
        self.review_mode_var.set(not self.review_mode_var.get())
        self._sync_review_mode_ui()

    def _sync_review_mode_ui(self) -> None:
        review_enabled = self.review_mode_var.get()
        self.right_panel_title_var.set("Review" if review_enabled else "Inspector")
        if review_enabled:
            if not self.review_controls.winfo_manager():
                self.review_controls.pack(side="left", padx=(0, 8))
            self.inspector_panel.grid_remove()
            self.review_panel.grid(row=1, column=0, sticky="nsew")
            self.review_mode_btn.configure(text="[V] Exit review", style="MiniAccent.TButton")
            if self.current_image_path is not None:
                self.inline_preview.load_path(self.current_image_path)
            else:
                self._show_empty_preview_state()
        else:
            if self.review_controls.winfo_manager():
                self.review_controls.pack_forget()
            self.review_panel.grid_remove()
            self.inspector_panel.grid(row=1, column=0, sticky="nsew")
            self.review_mode_btn.configure(text="[V] Review mode", style="MiniSoft.TButton")
            if self.current_image_path is not None and self.current_image_path in self.image_paths:
                metadata = self._get_metadata_cached(self.current_image_path)
                self._set_metadata_text(self._build_details_view(self.current_image_path, metadata))
            else:
                self._show_empty_metadata_state()
        self._sync_review_header_ui()

    def _sync_current_controls(self, path: Path | None, metadata: dict[str, str] | None) -> None:
        if path is None:
            self.favorite_btn.configure(text="[F] Favorite", style="MiniSoft.TButton")
            self.current_tags_var.set("")
            self.copy_prompt_btn.configure(state="disabled")
            self.review_mode_btn.configure(state="normal")
            self.favorite_btn.configure(state="disabled")
            self.reject_btn.configure(state="disabled")
            self.reset_review_btn.configure(state="disabled")
            self.save_tags_btn.configure(state="disabled")
            label, bg, fg = self._review_display("unreviewed")
            self.review_status_var.set(label)
            self.review_status_label.configure(bg=bg, fg=fg)
            self._update_inspector_summary(None, None)
            self._sync_review_header_ui()
            return

        self.copy_prompt_btn.configure(state="normal")
        self.review_mode_btn.configure(state="normal")
        self.favorite_btn.configure(state="normal")
        self.reject_btn.configure(state="normal")
        self.reset_review_btn.configure(state="normal")
        self.save_tags_btn.configure(state="normal")
        state = self._get_image_state(path)
        is_favorite = bool(state.get("favorite", False))
        self.favorite_btn.configure(
            text="[F] Favorite",
            style=("MiniAccent.TButton" if is_favorite else "MiniSoft.TButton"),
        )
        tags = [str(x) for x in state.get("tags", []) if str(x).strip()]
        self.current_tags_var.set(", ".join(tags))
        label, bg, fg = self._review_display(self._get_review_status(path))
        self.review_status_var.set(label)
        self.review_status_label.configure(bg=bg, fg=fg)
        self._update_inspector_summary(path, metadata)
        self._sync_review_header_ui()

    def _update_inspector_summary(self, path: Path | None, metadata: dict[str, str] | None) -> None:
        if path is None or metadata is None:
            self.meta_file_var.set(NO_IMAGE_SELECTED_TITLE)
            self.meta_path_var.set("")
            for var in self.summary_value_vars.values():
                var.set("-")
            return

        self.meta_file_var.set(path.name)
        subfolder, _color_key = self._subfolder_hint(path)
        self.meta_path_var.set(subfolder or "Root folder")

        model = self._pick(metadata, ["Model"]) or "-"
        sampler = self._pick(metadata, ["Sampler"]) or "-"
        steps = self._pick(metadata, ["Steps"]) or "-"
        cfg = self._pick(metadata, ["CFG", "CFG scale"]) or "-"
        resolution = self._pick(metadata, ["Resolution", "Size"]) or "-"
        if resolution == "-":
            width = self._pick(metadata, ["_width"])
            height = self._pick(metadata, ["_height"])
            if width and height:
                resolution = f"{width}x{height}"
        file_size = "-"
        try:
            file_size = f"{path.stat().st_size / 1024:.1f} KB"
        except Exception:
            pass

        self.summary_value_vars["Model"].set(model)
        self.summary_value_vars["Sampler"].set(sampler)
        self.summary_value_vars["Steps"].set(steps)
        self.summary_value_vars["CFG"].set(cfg)
        self.summary_value_vars["Resolution"].set(resolution)
        self.summary_value_vars["Size"].set(file_size)

    def _get_metadata_cached(self, path: Path) -> dict[str, str]:
        key = str(path)
        sig = self._metadata_signature(path)
        with self._cache_lock:
            cached = self.metadata_cache.get(key)
            cached_sig = self.metadata_cache_sig.get(key)
            if cached is not None and cached_sig == sig:
                return cached
        extracted = self._extract_metadata(path)
        with self._cache_lock:
            self.metadata_cache[key] = extracted
            self.metadata_cache_sig[key] = sig
        return extracted

    def _extract_metadata(self, path: Path) -> dict[str, str]:
        return MetadataParser.extract_metadata(path)

    def _extract_generation_fields(self, raw: dict[str, str]) -> dict[str, str]:
        return MetadataParser.extract_generation_fields(raw)

    def _parse_comfy_prompt_json(self, text: str) -> dict[str, str]:
        return MetadataParser.parse_comfy_prompt_json(text)

    @staticmethod
    def _guess_model_from_text(text: str) -> str:
        return MetadataParser.guess_model_from_text(text)

    @staticmethod
    def _normalize_model_value(value: str) -> str:
        return MetadataParser.normalize_model_value(value)

    @staticmethod
    def _clean_lora_name(value: str) -> str:
        return MetadataParser.clean_lora_name(value)

    def _extract_loras_from_text(self, text: str) -> set[str]:
        return MetadataParser.extract_loras_from_text(text)

    def _extract_loras_from_json_obj(self, obj: object, in_lora_context: bool = False) -> set[str]:
        return MetadataParser.extract_loras_from_json_obj(obj, in_lora_context)

    @staticmethod
    def _looks_like_lora_value(value: str, key_low: str, in_lora_context: bool) -> bool:
        return MetadataParser.looks_like_lora_value(value, key_low, in_lora_context)

    @staticmethod
    def _looks_like_non_lora_asset(value: str) -> bool:
        return MetadataParser.looks_like_non_lora_asset(value)

    def _extract_non_lora_assets_from_text(self, text: str) -> set[str]:
        return MetadataParser.extract_non_lora_assets_from_text(text)

    def _extract_non_lora_assets_from_json_obj(self, obj: object) -> set[str]:
        return MetadataParser.extract_non_lora_assets_from_json_obj(obj)

    def _parse_sd_parameters(self, text: str) -> tuple[str, str, dict[str, str]]:
        return MetadataParser.parse_sd_parameters(text)

    def _parse_kv_tail(self, tail: str) -> dict[str, str]:
        return MetadataParser.parse_kv_tail(tail)

    def _build_details_view(self, path: Path, metadata: dict[str, str]) -> str:
        state = self._get_image_state(path)
        user_tags = [str(x) for x in state.get("tags", []) if str(x).strip()]
        favorite_mark = "*" if bool(state.get("favorite", False)) else "-"

        prompt = self._pick(metadata, ["Prompt"]) or "-"
        negative_prompt = self._pick(metadata, ["Negative prompt"]) or "-"
        loras = self._pick(metadata, ["LoRAs"]) or self._extract_loras(prompt, metadata)

        title = f"{favorite_mark} {path.name}"
        return (
            f"{title}\n"
            f"{'=' * len(title)}\n\n"
            f"PROMPT\n"
            f"{'-' * 70}\n"
            f"{prompt}\n\n"
            f"NEGATIVE PROMPT\n"
            f"{'-' * 70}\n"
            f"{negative_prompt}\n\n"
            f"LoRAs\n"
            f"{'-' * 70}\n"
            f"{loras}\n\n"
            f"Tags\n"
            f"{'-' * 70}\n"
            f"{', '.join(user_tags) if user_tags else 'None'}\n"
        )

    def _pick(self, data: dict[str, str], keys: list[str]) -> str:
        return MetadataParser.pick(data, keys)

    def _extract_loras(self, prompt: str, metadata: dict[str, str]) -> str:
        return MetadataParser.extract_loras(prompt, metadata)

    @staticmethod
    def _canonical_asset_name(value: str) -> str:
        return MetadataParser.canonical_asset_name(value)

    @staticmethod
    def _is_modelish_node_type(node_type: str) -> bool:
        return MetadataParser.is_modelish_node_type(node_type)

    @staticmethod
    def _iter_strings(value: object) -> list[str]:
        return MetadataParser.iter_strings(value)

    def _extract_model_assets_from_workflow_json(self, text: str) -> set[str]:
        return MetadataParser.extract_model_assets_from_workflow_json(text)

    def _extract_loras_from_workflow_json(self, text: str) -> set[str]:
        return MetadataParser.extract_loras_from_workflow_json(text)

    def _extract_loras_from_lora_node(self, node: dict[str, object]) -> set[str]:
        return MetadataParser.extract_loras_from_lora_node(node)

    def _extract_loras_from_lora_payload(self, payload: object) -> set[str]:
        return MetadataParser.extract_loras_from_lora_payload(payload)

    def _safe_columns(self) -> int:
        value = self.columns_var.get().strip()
        if not value.isdigit():
            return DEFAULT_COLUMNS
        return max(2, min(12, int(value)))

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        return CatalogHelper.safe_mtime(path)

    def _sort_mtime_cached(self, path: Path) -> float:
        key = str(path)
        cached = self.file_mtime_cache.get(key)
        if cached is not None:
            return cached
        mtime = self._safe_mtime(path)
        self.file_mtime_cache[key] = mtime
        return mtime

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        return CatalogHelper.file_signature(path)

    def _configure_metadata_tags(self) -> None:
        self.meta_text.tag_configure("file_title", font=("Segoe UI Semibold", 14), foreground=self._metadata_tag_targets["file_title"])
        self.meta_text.tag_configure("section", font=("Segoe UI Semibold", 11), foreground=self._metadata_tag_targets["section"])
        self.meta_text.tag_configure("section_negative", font=("Segoe UI Semibold", 11), foreground=self._metadata_tag_targets["section_negative"])
        self.meta_text.tag_configure("separator", foreground=self._metadata_tag_targets["separator"])
        self.meta_text.tag_configure("field_name", foreground=self._metadata_tag_targets["field_name"], font=("Segoe UI Semibold", 10))
        self.meta_text.tag_configure("field_value", foreground=self._metadata_tag_targets["field_value"], font=("Segoe UI", 10))
        self.meta_text.tag_configure(
            "glass_prompt",
            background=PALETTE["glass_prompt"],
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=8,
            spacing3=8,
        )
        self.meta_text.tag_configure(
            "glass_negative",
            background=PALETTE["glass_negative"],
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=8,
            spacing3=8,
        )
        self.meta_text.tag_configure(
            "glass_main",
            background=PALETTE["glass_main"],
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=8,
            spacing3=8,
        )
        self.meta_text.tag_configure(
            "glass_misc",
            background=PALETTE["glass_misc"],
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=8,
            spacing3=8,
        )
        self.meta_text.tag_configure(
            "chip_value",
            background=PALETTE["chip_soft_bg"],
            foreground=PALETTE["chip_soft_fg"],
            font=("Segoe UI Semibold", 9),
        )

    def _set_metadata_text(self, text: str) -> None:
        if self._meta_fade_after_id is not None:
            self.after_cancel(self._meta_fade_after_id)
            self._meta_fade_after_id = None
        self.meta_text.configure(state="normal")
        self.meta_text.delete("1.0", "end")
        self.meta_text.insert("1.0", text)
        self._highlight_metadata_text()
        self.meta_text.configure(state="disabled")
        self.meta_text.yview_moveto(0)
        self._start_metadata_fade_in()

    def _highlight_metadata_text(self) -> None:
        lines = self.meta_text.get("1.0", "end-1c").splitlines()
        section_styles = {
            "PROMPT": "glass_prompt",
            "NEGATIVE PROMPT": "glass_negative",
            "LoRAs": "glass_misc",
            "Tags": "glass_misc",
        }
        section_rows: list[tuple[int, str]] = []

        for i, line in enumerate(lines, start=1):
            start = f"{i}.0"
            end = f"{i}.end"
            stripped = line.strip()

            if i == 1 and stripped:
                self.meta_text.tag_add("file_title", start, end)
                continue

            if stripped in {"PROMPT", "LoRAs", "Tags"}:
                section_rows.append((i, stripped))
                self.meta_text.tag_add("section", start, end)
                continue

            if stripped == "NEGATIVE PROMPT":
                section_rows.append((i, stripped))
                self.meta_text.tag_add("section_negative", start, end)
                continue

            if stripped.startswith("---"):
                self.meta_text.tag_add("separator", start, end)
                continue

            if ":" in line and not stripped.startswith("http"):
                key, _value = line.split(":", 1)
                if key and len(key) <= 14:
                    key_end = f"{i}.{len(key) + 1}"
                    self.meta_text.tag_add("field_name", start, key_end)
                    self.meta_text.tag_add("field_value", key_end, end)
                    key_clean = key.strip()
                    if key_clean in {"Model", "Sampler", "Scheduler", "Seed", "CFG", "Steps", "Resolution", "Size"}:
                        value_start = line.find(":") + 1
                        while value_start < len(line) and line[value_start] == " ":
                            value_start += 1
                        if value_start < len(line):
                            self.meta_text.tag_add("chip_value", f"{i}.{value_start}", end)

        if section_rows:
            for idx, (line_no, section_name) in enumerate(section_rows):
                next_line = section_rows[idx + 1][0] if idx + 1 < len(section_rows) else len(lines) + 1
                end_line = max(line_no, next_line - 1)
                block_tag = section_styles.get(section_name)
                if block_tag:
                    self.meta_text.tag_add(block_tag, f"{line_no}.0", f"{end_line}.end")

    def _on_close(self) -> None:
        self._scan_token += 1
        self._filter_token += 1
        self._thumb_decode_token += 1
        self._cancel_metadata_warmup()
        if self._thumb_render_after_id is not None:
            self.after_cancel(self._thumb_render_after_id)
            self._thumb_render_after_id = None
        if self._thumb_batch_after_id is not None:
            self.after_cancel(self._thumb_batch_after_id)
            self._thumb_batch_after_id = None
        if self._column_render_after_id is not None:
            self.after_cancel(self._column_render_after_id)
            self._column_render_after_id = None
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
            self._filter_after_id = None
        if self._meta_fade_after_id is not None:
            self.after_cancel(self._meta_fade_after_id)
            self._meta_fade_after_id = None
        if self._thumb_frame_configure_after_id is not None:
            self.after_cancel(self._thumb_frame_configure_after_id)
            self._thumb_frame_configure_after_id = None
        if self._wheel_y_after_id is not None:
            self.after_cancel(self._wheel_y_after_id)
            self._wheel_y_after_id = None
        if self._wheel_x_after_id is not None:
            self.after_cancel(self._wheel_x_after_id)
            self._wheel_x_after_id = None
        if self._save_state_after_id is not None:
            self.after_cancel(self._save_state_after_id)
            self._save_state_after_id = None
        for after_id in self._button_anim_after_ids.values():
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._button_anim_after_ids.clear()
        for after_id in self._summary_flash_after_ids.values():
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._summary_flash_after_ids.clear()
        self._shutdown_ui_dispatcher()
        if self._folder_tip_win is not None and self._folder_tip_win.winfo_exists():
            self._folder_tip_win.destroy()
            self._folder_tip_win = None
            self._folder_tip_label = None
        if self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.destroy()
            self.preview_window = None
        self._save_state()
        self.destroy()

    def _enable_button_hover_animation(self, button: ttk.Button, is_accent: bool) -> None:
        widget_id = id(button)
        token = self._button_anim_tokens.get(widget_id, 0) + 1
        self._button_anim_tokens[widget_id] = token

        if is_accent:
            base_bg = PALETTE["accent"]
            hover_bg = PALETTE["accent_hover"]
            fg = "#ffffff"
            base_border = PALETTE["accent"]
            hover_border = PALETTE["accent_hover"]
        else:
            base_bg = PALETTE["surface"]
            hover_bg = PALETTE["surface_alt"]
            fg = PALETTE["text"]
            base_border = PALETTE["border"]
            hover_border = PALETTE["accent"]

        style_name = f"Hover{widget_id}.TButton"
        self.style.configure(
            style_name,
            background=base_bg,
            foreground=fg,
            bordercolor=base_border,
            padding=(14, 8),
            relief="flat",
            font=("Segoe UI Semibold", 10) if is_accent else ("Segoe UI", 10),
        )
        button.configure(style=style_name)
        self._button_target_colors[widget_id] = (base_bg, base_border)

        button.bind("<Enter>", lambda _e, b=button, accent=is_accent: self._animate_button_hover(b, accent, hover=True), add="+")
        button.bind("<Leave>", lambda _e, b=button, accent=is_accent: self._animate_button_hover(b, accent, hover=False), add="+")

    def _animate_button_hover(self, button: ttk.Button, is_accent: bool, hover: bool) -> None:
        widget_id = id(button)
        token = self._button_anim_tokens.get(widget_id, 0) + 1
        self._button_anim_tokens[widget_id] = token

        if is_accent:
            target_bg = PALETTE["accent_hover"] if hover else PALETTE["accent"]
            target_border = PALETTE["accent_hover"] if hover else PALETTE["accent"]
        else:
            target_bg = PALETTE["surface_alt"] if hover else PALETTE["surface"]
            target_border = PALETTE["accent"] if hover else PALETTE["border"]

        style_name = button.cget("style")
        current_bg = self.style.lookup(style_name, "background") or self._button_target_colors.get(widget_id, (target_bg, target_border))[0]
        current_border = self.style.lookup(style_name, "bordercolor") or self._button_target_colors.get(widget_id, (target_bg, target_border))[1]
        self._button_target_colors[widget_id] = (target_bg, target_border)
        self._run_button_animation(button, current_bg, target_bg, current_border, target_border, token, step=0)

    def _run_button_animation(
        self,
        button: ttk.Button,
        from_bg: str,
        to_bg: str,
        from_border: str,
        to_border: str,
        token: int,
        step: int,
    ) -> None:
        widget_id = id(button)
        if self._button_anim_tokens.get(widget_id) != token:
            return

        steps = 7
        t_linear = step / steps
        t = 1.0 - (1.0 - t_linear) * (1.0 - t_linear)
        style_name = button.cget("style")
        bg = self._mix_hex(from_bg, to_bg, t)
        border = self._mix_hex(from_border, to_border, t)
        self.style.configure(style_name, background=bg, bordercolor=border)

        if step < steps:
            after_id = self.after(
                14,
                lambda: self._run_button_animation(
                    button, from_bg, to_bg, from_border, to_border, token, step + 1
                ),
            )
            self._button_anim_after_ids[widget_id] = after_id

    def _start_metadata_fade_in(self) -> None:
        self.meta_text.configure(fg=self._meta_start_color)
        for tag_name in self._metadata_tag_targets:
            self.meta_text.tag_configure(tag_name, foreground=self._meta_start_color)
        self._run_metadata_fade(step=0, total_steps=8)

    def _run_metadata_fade(self, step: int, total_steps: int) -> None:
        t_linear = step / total_steps
        t = 1.0 - (1.0 - t_linear) * (1.0 - t_linear)
        self.meta_text.configure(fg=self._mix_hex(self._meta_start_color, PALETTE["text"], t))
        for tag_name, target in self._metadata_tag_targets.items():
            self.meta_text.tag_configure(tag_name, foreground=self._mix_hex(self._meta_start_color, target, t))

        if step < total_steps:
            self._meta_fade_after_id = self.after(16, lambda: self._run_metadata_fade(step + 1, total_steps))
        else:
            self._meta_fade_after_id = None

    @staticmethod
    def _mix_hex(start_hex: str, end_hex: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        s = start_hex.lstrip("#")
        e = end_hex.lstrip("#")
        sr, sg, sb = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        er, eg, eb = int(e[0:2], 16), int(e[2:4], 16), int(e[4:6], 16)
        r = round(sr + (er - sr) * t)
        g = round(sg + (eg - sg) * t)
        b = round(sb + (eb - sb) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _on_thumb_frame_configure(self, _event: tk.Event) -> None:
        if self._thumb_frame_configure_after_id is not None:
            return
        self._thumb_frame_configure_after_id = self.after_idle(self._flush_thumb_frame_configure)

    def _flush_thumb_frame_configure(self) -> None:
        self._thumb_frame_configure_after_id = None
        if self.canvas.winfo_exists():
            self._update_canvas_window_size()

    @staticmethod
    def _wheel_delta(event: tk.Event) -> int:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            return delta
        num = int(getattr(event, "num", 0) or 0)
        if num == 4:
            return 120
        if num == 5:
            return -120
        return 0

    def _flush_vertical_wheel(self) -> None:
        self._wheel_y_after_id = None
        if not self.canvas.winfo_exists():
            self._wheel_y_delta_accum = 0
            return
        delta = self._wheel_y_delta_accum
        steps = -int(delta / 120)
        if steps != 0:
            self.canvas.yview_scroll(steps, "units")
            self._wheel_y_delta_accum = delta + (steps * 120)
            self.after_idle(self._stabilize_canvas_after_scroll)

    def _flush_horizontal_wheel(self) -> None:
        self._wheel_x_after_id = None
        if not self.canvas.winfo_exists():
            self._wheel_x_delta_accum = 0
            return
        delta = self._wheel_x_delta_accum
        steps = -int(delta / 120)
        if steps != 0:
            self.canvas.xview_scroll(steps, "units")
            self._wheel_x_delta_accum = delta + (steps * 120)
            self.after_idle(self._stabilize_canvas_after_scroll)

    def _stabilize_canvas_after_scroll(self) -> None:
        if not self.canvas.winfo_exists():
            return
        try:
            self.canvas.update_idletasks()
            bbox = self.canvas.bbox("all")
            if bbox:
                self.canvas.configure(scrollregion=bbox)
        except Exception:
            pass

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        if not self.canvas.winfo_exists():
            return "break"
        if self._thumb_rendering:
            return "break"
        self._hide_folder_tooltip()
        self._wheel_y_delta_accum += self._wheel_delta(event)
        if self._wheel_y_after_id is None:
            self._wheel_y_after_id = self.after_idle(self._flush_vertical_wheel)
        return "break"

    def _on_shift_mouse_wheel(self, event: tk.Event) -> None:
        if not self.canvas.winfo_exists():
            return "break"
        if self._thumb_rendering:
            return "break"
        self._hide_folder_tooltip()
        self._wheel_x_delta_accum += self._wheel_delta(event)
        if self._wheel_x_after_id is None:
            self._wheel_x_after_id = self.after_idle(self._flush_horizontal_wheel)
        return "break"

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        return value if len(value) <= max_len else value[: max_len - 3] + "..."

    @staticmethod
    def _truncate_to_pixel_width(value: str, max_px: int, font_obj: tkfont.Font) -> str:
        if max_px <= 0:
            return ""
        if font_obj.measure(value) <= max_px:
            return value

        ellipsis = "..."
        ellipsis_w = font_obj.measure(ellipsis)
        if ellipsis_w >= max_px:
            return ""

        lo = 0
        hi = len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font_obj.measure(value[:mid]) + ellipsis_w <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return value[:lo].rstrip() + ellipsis


def main() -> None:
    enable_high_dpi()
    app = ImageMetadataViewer()
    app.mainloop()


if __name__ == "__main__":
    main()

