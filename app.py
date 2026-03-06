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
from urllib import error as urlerror
from urllib import request as urlrequest

from PIL import Image, ImageOps, ImageTk

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
UPDATE_REPO_OWNER = "ixxeg"
UPDATE_REPO_NAME = "PromptLens"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/releases"


def get_app_dir() -> Path:
    # For PyInstaller onefile/onedir, keep state near the executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


STATE_FILE = get_app_dir() / ".image_catalog_state.json"
PALETTE = {
    "bg": "#f3effb",
    "panel": "#ede6fa",
    "surface": "#ffffff",
    "surface_alt": "#f6f1ff",
    "surface_1": "#ffffff",
    "surface_2": "#f7f2ff",
    "surface_3": "#efe7fb",
    "gradient_top": "#f7f2ff",
    "gradient_mid": "#f2eafb",
    "gradient_bottom": "#ede4f9",
    "text": "#322547",
    "muted": "#7b6a96",
    "accent": "#8f79d8",
    "accent_hover": "#9f8ae4",
    "accent_active": "#7e66cb",
    "accent_muted": "#c8b9ea",
    "border": "#d7caee",
    "focus_ring": "#a08bd8",
    "danger": "#b54b7e",
    "chip_bg": "#ece3fb",
    "chip_fg": "#4a3680",
    "chip_soft_bg": "#f1e9ff",
    "chip_soft_fg": "#5f4f86",
    "glass_prompt": "#f4efff",
    "glass_negative": "#faedf5",
    "glass_main": "#f0ecff",
    "glass_misc": "#f5f1ff",
    "shadow": "#dfd2f3",
    "thumb_shadow": "#e9def6",
    "thumb_bg": "#fdfcff",
    "thumb_hover_bg": "#f8f5ff",
    "thumb_selected_bg": "#f7f3ff",
    "status_info": "#8f79d8",
    "status_busy": "#a08bd8",
    "status_ok": "#6f9f8a",
    "status_warn": "#b08f65",
    "status_error": "#b54b7e",
    "skeleton_base": "#ebe2fb",
    "skeleton_shine": "#f5efff",
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


class PreviewWindow(tk.Toplevel):
    def __init__(self, parent: tk.Tk, image_path: Path) -> None:
        super().__init__(parent)
        self.geometry("1200x820")

        self.image_path = image_path
        self.zoom = 1.0
        self.min_zoom = 0.1
        self.max_zoom = 8.0
        self.base_image: Image.Image | None = None
        self._image_pyramid: list[tuple[float, Image.Image]] = []
        self.tk_image: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._last_render_key: tuple[int, int, int, int, int] | None = None
        self._pending_zoom: float | None = None
        self._wheel_delta_accum = 0
        self._wheel_flush_after_id: str | None = None
        self._quality_after_id: str | None = None
        self._load_token = 0
        self._load_thread: threading.Thread | None = None
        self.path_var = tk.StringVar(value=str(image_path))
        self._loading_item: int | None = None

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

        self.zoom_var = tk.StringVar(value="100%")
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
        self._loading_item = self.canvas.create_text(
            0,
            0,
            text="Loading preview...",
            fill="#f2f2f2",
            font=("Segoe UI Semibold", 12),
            state="hidden",
        )

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

    def load_path(self, image_path: Path) -> None:
        self.image_path = image_path
        self.title(f"Preview: {image_path.name}")
        self.path_var.set(str(image_path))
        self.zoom = 1.0
        self._pending_zoom = None
        self._last_render_key = None
        self._cancel_quality_render()
        self._start_load_image()

    def _start_load_image(self) -> None:
        self._load_token += 1
        token = self._load_token
        self.base_image = None
        self._image_pyramid = []
        self.tk_image = None
        if self._image_item is not None:
            self.canvas.itemconfigure(self._image_item, image="")
        self.canvas.configure(scrollregion=(0, 0, max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())))
        self._show_loading("Loading preview...")
        self._load_thread = threading.Thread(
            target=self._load_image_worker,
            args=(self.image_path, token),
            daemon=True,
        )
        self._load_thread.start()

    def _load_image_worker(self, image_path: Path, token: int) -> None:
        try:
            with Image.open(image_path) as im:
                base_image = ImageOps.exif_transpose(im).convert("RGB")
            pyramid = self._build_image_pyramid(base_image)
            self.after(0, lambda: self._on_image_loaded(token, image_path, base_image, pyramid))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self._on_image_load_failed(token, exc))

    def _on_image_loaded(
        self,
        token: int,
        image_path: Path,
        base_image: Image.Image,
        pyramid: list[tuple[float, Image.Image]],
    ) -> None:
        if token != self._load_token or image_path != self.image_path:
            return
        self.base_image = base_image
        self._image_pyramid = pyramid
        self._hide_loading()
        self.after_idle(self._fit_once_ready)

    def _on_image_load_failed(self, token: int, exc: Exception) -> None:
        if token != self._load_token:
            return
        self._hide_loading()
        messagebox.showerror("Preview error", f"Cannot open image:\n{exc}")
        self.destroy()

    @staticmethod
    def _build_image_pyramid(base_image: Image.Image) -> list[tuple[float, Image.Image]]:
        pyramid: list[tuple[float, Image.Image]] = [(1.0, base_image)]
        current = base_image
        scale = 1.0
        while min(current.width, current.height) > 512:
            next_size = (max(1, current.width // 2), max(1, current.height // 2))
            current = current.resize(next_size, Image.Resampling.BOX)
            scale *= 0.5
            pyramid.append((scale, current))
        return pyramid

    def _choose_render_source(self, target_w: int, target_h: int) -> tuple[float, Image.Image]:
        if not self._image_pyramid:
            return 1.0, self.base_image
        source_scale, source_image = self._image_pyramid[0]
        for scale, image in self._image_pyramid[1:]:
            if image.width >= target_w and image.height >= target_h:
                source_scale, source_image = scale, image
                continue
            break
        return source_scale, source_image

    def _show_loading(self, text: str) -> None:
        if self._loading_item is None:
            return
        self.canvas.itemconfigure(self._loading_item, text=text, state="normal")
        self._position_loading_item()

    def _hide_loading(self) -> None:
        if self._loading_item is not None:
            self.canvas.itemconfigure(self._loading_item, state="hidden")

    def _position_loading_item(self) -> None:
        if self._loading_item is None:
            return
        x = max(20, self.canvas.winfo_width() // 2)
        y = max(20, self.canvas.winfo_height() // 2)
        self.canvas.coords(self._loading_item, x, y)

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        self._position_loading_item()

    def _fit_once_ready(self) -> None:
        if self.base_image is None:
            return
        self.update_idletasks()
        if self.canvas.winfo_width() <= 2 or self.canvas.winfo_height() <= 2:
            self.after(30, self._fit_once_ready)
            return
        self.fit_to_window()

    def _render(self, resample: int = Image.Resampling.LANCZOS) -> None:
        if self.base_image is None:
            return
        w = max(1, int(self.base_image.width * self.zoom))
        h = max(1, int(self.base_image.height * self.zoom))
        source_scale, source_image = self._choose_render_source(w, h)
        render_key = (w, h, int(resample), source_image.width, source_image.height)
        if self._last_render_key == render_key and self.tk_image is not None:
            self.zoom_var.set(f"{int(self.zoom * 100)}%")
            return
        resized = source_image.resize((w, h), resample)
        self.tk_image = ImageTk.PhotoImage(resized)
        if self._image_item is None:
            self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.itemconfigure(self._image_item, image=self.tk_image)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self.zoom_var.set(f"{int(self.zoom * 100)}%")
        self._last_render_key = render_key

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom * 1.15)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom / 1.15)

    def reset_zoom(self) -> None:
        self.set_zoom(1.0)

    def fit_to_window(self) -> None:
        if self.base_image is None:
            return
        self.update_idletasks()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        zx = cw / self.base_image.width
        zy = ch / self.base_image.height
        self.set_zoom(min(zx, zy))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def set_zoom(self, value: float) -> None:
        self._set_zoom(value, interactive=False)

    def _set_zoom(self, value: float, interactive: bool) -> None:
        clamped = max(self.min_zoom, min(self.max_zoom, value))
        self._pending_zoom = clamped
        if abs(clamped - self.zoom) < 1e-9:
            if self.tk_image is None:
                self._render(Image.Resampling.LANCZOS)
                return
            if interactive:
                self._schedule_quality_render()
            return
        self.zoom = clamped
        if interactive:
            self._render(Image.Resampling.BILINEAR)
            self._schedule_quality_render()
        else:
            self._cancel_quality_render()
            self._render(Image.Resampling.LANCZOS)

    def _schedule_quality_render(self) -> None:
        if self._quality_after_id is not None:
            self.after_cancel(self._quality_after_id)
        self._quality_after_id = self.after(130, self._render_quality)

    def _cancel_quality_render(self) -> None:
        if self._quality_after_id is not None:
            self.after_cancel(self._quality_after_id)
            self._quality_after_id = None

    def _render_quality(self) -> None:
        self._quality_after_id = None
        self._render(Image.Resampling.LANCZOS)

    @staticmethod
    def _wheel_step(event: tk.Event) -> int:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            return delta
        num = int(getattr(event, "num", 0) or 0)
        if num == 4:
            return 120
        if num == 5:
            return -120
        return 0

    def _flush_wheel_zoom(self) -> None:
        self._wheel_flush_after_id = None
        delta = self._wheel_delta_accum
        self._wheel_delta_accum = 0
        if delta == 0:
            return
        ticks = int(delta / 120)
        if ticks == 0:
            ticks = 1 if delta > 0 else -1
        base = self._pending_zoom if self._pending_zoom is not None else self.zoom
        factor = 1.12 ** abs(ticks)
        target = base * factor if ticks > 0 else base / factor
        self._pending_zoom = target
        self._set_zoom(target, interactive=True)

    def destroy(self) -> None:
        self._load_token += 1
        if self._wheel_flush_after_id is not None:
            try:
                self.after_cancel(self._wheel_flush_after_id)
            except Exception:
                pass
            self._wheel_flush_after_id = None
        self._cancel_quality_render()
        super().destroy()

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        # Mouse wheel always zooms in preview mode (coalesced for smoother interaction).
        self._wheel_delta_accum += self._wheel_step(event)
        if self._wheel_flush_after_id is None:
            self._wheel_flush_after_id = self.after(24, self._flush_wheel_zoom)


class ImageMetadataViewer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Local Image Metadata Catalog")
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
            "separator": "#b7a9d5",
            "field_name": "#6a5795",
            "field_value": PALETTE["text"],
        }
        self._meta_start_color = "#b6aacd"
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
            background=PALETTE["panel"],
            foreground=PALETTE["text"],
            font=("Segoe UI Semibold", 13),
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
            padding=(14, 8),
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
            font=("Segoe UI Semibold", 10),
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
                self.after(0, lambda: self._on_update_check_finished("You already have the latest version.", "ok"))
                return

            asset_url, asset_name = self._choose_release_asset(assets)
            if not asset_url:
                self.after(
                    0,
                    lambda: self._on_update_check_no_asset(latest, html_url),
                )
                return

            self.after(
                0,
                lambda: self._on_update_available(latest, asset_url, asset_name, html_url),
            )
        except (urlerror.URLError, TimeoutError) as exc:
            self.after(0, lambda: self._on_update_check_finished(f"Update check failed: {exc}", "error"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self._on_update_check_finished(f"Update check failed: {exc}", "error"))

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
            self.after(0, lambda: self._apply_downloaded_update(download_path))
        except Exception as exc:  # noqa: BLE001
            try:
                if download_path.exists():
                    download_path.unlink()
            except Exception:
                pass
            self.after(0, lambda: self._on_update_check_finished(f"Download failed: {exc}", "error"))

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
            f"set TARGET={target_exe}\r\n"
            f"set NEWFILE={new_exe}\r\n"
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

        top = ttk.Frame(self, padding=10, style="Root.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        header_shell = tk.Frame(
            top,
            bg=PALETTE["surface_2"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=10,
            pady=8,
        )
        header_shell.grid(row=0, column=0, sticky="ew")
        header_shell.columnconfigure(0, weight=1)

        controls_row = tk.Frame(header_shell, bg=PALETTE["surface_2"])
        controls_row.grid(row=0, column=0, sticky="ew")
        controls_row.columnconfigure(0, weight=1)

        controls_left = tk.Frame(controls_row, bg=PALETTE["surface_2"])
        controls_left.grid(row=0, column=0, sticky="w")
        controls_right = tk.Frame(controls_row, bg=PALETTE["surface_2"])
        controls_right.grid(row=0, column=1, sticky="e")

        self.add_btn = ttk.Button(controls_left, text="[+] Add folder", command=self.add_folder, style="Soft.TButton")
        self.add_btn.pack(side="left", padx=(0, 8))
        self.clear_btn = ttk.Button(controls_left, text="[-] Clear folders", command=self.clear_folders, style="Soft.TButton")
        self.clear_btn.pack(side="left", padx=(0, 12))
        self.folders_toggle_btn = ttk.Button(controls_left, text="[F] Hide folders", command=self._toggle_folders_panel, style="Soft.TButton")
        self.folders_toggle_btn.pack(side="left", padx=(0, 12))
        self.scan_btn = ttk.Button(controls_left, text="[S] Scan", command=self.scan_images, style="Accent.TButton")
        self.scan_btn.pack(side="left", padx=(0, 8))
        self._enable_button_hover_animation(self.add_btn, is_accent=False)
        self._enable_button_hover_animation(self.clear_btn, is_accent=False)
        self._enable_button_hover_animation(self.folders_toggle_btn, is_accent=False)
        self._enable_button_hover_animation(self.scan_btn, is_accent=True)

        tk.Label(
            controls_right,
            text="Thumb size",
            bg=PALETTE["surface_2"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))
        self.thumb_size_scale = ttk.Scale(
            controls_right,
            from_=100,
            to=320,
            variable=self.thumb_size_var,
            command=self.on_thumb_size_changed,
        )
        self.thumb_size_scale.pack(side="left", padx=(0, 12))
        self.thumb_size_scale.bind("<ButtonRelease-1>", lambda _e: self._apply_thumb_size_change(force=True))

        tk.Label(
            controls_right,
            text="Columns",
            bg=PALETTE["surface_2"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))
        self.columns_spin = ttk.Spinbox(
            controls_right,
            from_=2,
            to=12,
            textvariable=self.columns_var,
            width=7,
            command=self.on_layout_changed,
            font=("Segoe UI Semibold", 12),
        )
        self.columns_spin.pack(side="left")
        self.columns_spin.bind("<Return>", lambda _e: self._apply_layout_change())
        self.columns_spin.bind("<FocusOut>", lambda _e: self._apply_layout_change())

        filter_row = tk.Frame(header_shell, bg=PALETTE["surface_3"])
        filter_row.grid(row=1, column=0, sticky="ew", pady=(8, 0), padx=2)
        filter_row.columnconfigure(1, weight=1)

        tk.Label(filter_row, text="Search", bg=PALETTE["surface_3"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.search_entry = ttk.Entry(filter_row, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(8, 10))
        self.search_entry.bind("<KeyRelease>", lambda _e: self._schedule_apply_filters())

        tk.Label(filter_row, text="Prompt tag", bg=PALETTE["surface_3"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.tag_entry = ttk.Entry(filter_row, textvariable=self.tag_filter_var, width=18)
        self.tag_entry.grid(row=0, column=3, sticky="w", padx=(0, 10))
        self.tag_entry.bind("<KeyRelease>", lambda _e: self._schedule_apply_filters())

        self.favorites_only_chk = tk.Checkbutton(
            filter_row,
            text="Favorites only",
            variable=self.favorites_only_var,
            command=self.apply_filters,
            bg=PALETTE["surface_3"],
            fg=PALETTE["text"],
            activebackground=PALETTE["surface_3"],
            activeforeground=PALETTE["text"],
            selectcolor=PALETTE["surface_1"],
            font=("Segoe UI", 10),
            bd=0,
            highlightthickness=0,
        )
        self.favorites_only_chk.grid(row=0, column=4, sticky="w", padx=(0, 10))

        tk.Label(filter_row, text="Sort", bg=PALETTE["surface_3"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=5, sticky="e", padx=(0, 6))
        self.sort_combo = ttk.Combobox(
            filter_row,
            textvariable=self.sort_var,
            values=("Newest", "Oldest"),
            state="readonly",
            width=10,
        )
        self.sort_combo.grid(row=0, column=6, sticky="w", padx=(0, 10))
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())

        self.clear_filter_btn = ttk.Button(filter_row, text="[C] Clear filters", style="Soft.TButton", command=self._clear_filters)
        self.clear_filter_btn.grid(row=0, column=7, sticky="w")
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
        self.folders_panel.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.folders_panel.columnconfigure(0, weight=1)

        self.folders_container = tk.Frame(self.folders_panel, bg=PALETTE["surface_1"], bd=0)
        self.folders_container.grid(row=0, column=0, sticky="ew")
        self.status_pill = tk.Frame(
            header_shell,
            bg=PALETTE["surface_2"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=8,
            pady=4,
        )
        self.status_pill.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.status_dot = tk.Canvas(
            self.status_pill,
            width=10,
            height=10,
            bg=PALETTE["surface_2"],
            highlightthickness=0,
            bd=0,
        )
        self.status_dot.pack(side="left", padx=(0, 6))
        self._status_dot_item = self.status_dot.create_oval(1, 1, 9, 9, fill=PALETTE["status_info"], outline="")
        self.status_label = tk.Label(
            self.status_pill,
            textvariable=self.status_var,
            bg=PALETTE["surface_2"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
        )
        self.status_label.pack(side="left")
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

        left = ttk.Frame(body, padding=(10, 6, 6, 10), style="Panel.TFrame")
        right = ttk.Frame(body, padding=(6, 6, 10, 10), style="Panel.TFrame")
        body.add(left, minsize=520, stretch="always")
        body.add(right, minsize=380)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(left, highlightthickness=0, background=PALETTE["surface_3"])
        self.v_scroll = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(left, orient="horizontal", command=self.canvas.xview)
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
        right.rowconfigure(3, weight=1)

        ttk.Label(right, text="Metadata", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(right, style="Panel.TFrame")
        actions.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        actions.columnconfigure(1, weight=1)

        actions_top = ttk.Frame(actions, style="Panel.TFrame")
        actions_top.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.copy_prompt_btn = ttk.Button(actions_top, text="[C] Copy prompt", style="Soft.TButton", command=self.copy_current_prompt)
        self.copy_prompt_btn.pack(side="left", padx=(0, 8))
        self.favorite_btn = ttk.Button(actions_top, text="[*] Favorite", style="Soft.TButton", command=self.toggle_current_favorite)
        self.favorite_btn.pack(side="left")
        self._enable_button_hover_animation(self.copy_prompt_btn, is_accent=False)
        self._enable_button_hover_animation(self.favorite_btn, is_accent=False)

        ttk.Label(actions, text="Tags", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        self.current_tags_var = tk.StringVar(value="")
        self.current_tags_entry = ttk.Entry(actions, textvariable=self.current_tags_var)
        self.current_tags_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.save_tags_btn = ttk.Button(actions, text="[T] Save tags", style="Soft.TButton", command=self.save_current_tags)
        self.save_tags_btn.grid(row=1, column=2, sticky="e")
        self._enable_button_hover_animation(self.save_tags_btn, is_accent=False)

        self.inspector_summary = tk.Frame(
            right,
            bg=PALETTE["surface_2"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            bd=0,
            padx=10,
            pady=8,
        )
        self.inspector_summary.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.inspector_summary.columnconfigure(0, weight=1)
        self.inspector_summary.columnconfigure(1, weight=1)
        self.inspector_summary.columnconfigure(2, weight=1)

        self.meta_file_var = tk.StringVar(value="No image selected")
        self.meta_path_var = tk.StringVar(value="")
        tk.Label(
            self.inspector_summary,
            textvariable=self.meta_file_var,
            bg=PALETTE["surface_2"],
            fg=PALETTE["text"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="ew")
        tk.Label(
            self.inspector_summary,
            textvariable=self.meta_path_var,
            bg=PALETTE["surface_2"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 6))

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
                highlightthickness=2,
                highlightbackground=PALETTE["accent_muted"],
                bd=0,
                padx=8,
                pady=5,
            )
            chip.grid(row=row, column=col, sticky="ew", padx=3, pady=3)
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

        meta_wrap = ttk.Frame(right, style="Surface.TFrame")
        meta_wrap.grid(row=3, column=0, sticky="nsew", pady=(0, 0))
        meta_wrap.columnconfigure(0, weight=1)
        meta_wrap.rowconfigure(0, weight=1)

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
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["focus_ring"],
        )
        self.meta_scroll = ttk.Scrollbar(meta_wrap, orient="vertical", command=self.meta_text.yview)
        self.meta_text.configure(yscrollcommand=self.meta_scroll.set)

        self.meta_text.grid(row=0, column=0, sticky="nsew")
        self.meta_scroll.grid(row=0, column=1, sticky="ns")
        self._configure_metadata_tags()
        self.meta_text.configure(state="disabled")
        self._sync_current_controls(None, None)

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
        if not hasattr(self, "status_dot"):
            return
        color_map = {
            "info": PALETTE["status_info"],
            "busy": PALETTE["status_busy"],
            "ok": PALETTE["status_ok"],
            "warn": PALETTE["status_warn"],
            "error": PALETTE["status_error"],
        }
        color = color_map.get(kind, PALETTE["status_info"])
        self.status_dot.itemconfig(self._status_dot_item, fill=color)

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
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
                panel_visible = ui.get("folders_panel_visible", True)
                if columns.isdigit():
                    self.columns_var.set(str(max(2, min(12, int(columns)))))
                if isinstance(thumb_size, int):
                    self.thumb_size_var.set(float(max(100, min(320, thumb_size))))
                if sort_mode in {"Newest", "Oldest"}:
                    self.sort_var.set(sort_mode)
                if isinstance(panel_visible, bool):
                    self.folders_panel_visible = panel_visible

            raw = data.get("images", {})
            if isinstance(raw, dict):
                for path_str, item in raw.items():
                    if not isinstance(item, dict):
                        continue
                    favorite = bool(item.get("favorite", False))
                    tags_raw = item.get("tags", [])
                    tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
                    self.image_state[path_str] = {"favorite": favorite, "tags": tags}
        except Exception:
            self.image_state = {}

    def _save_state(self) -> None:
        self._prune_image_state()
        payload = {
            "selected_dirs": [str(p) for p in self.selected_dirs],
            "ui": {
                "columns": self._safe_columns(),
                "thumb_size": int(round(self.thumb_size_var.get())),
                "sort": self.sort_var.get(),
                "folders_panel_visible": bool(self.folders_panel_visible),
            },
            "images": self.image_state,
        }
        try:
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

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
        pruned: dict[str, dict[str, object]] = {}
        for path_key, state in self.image_state.items():
            if existing_keys is not None and path_key not in existing_keys:
                continue
            favorite = bool(state.get("favorite", False))
            tags = self._normalize_tags(state.get("tags", []))
            if not favorite and not tags:
                continue
            pruned[path_key] = {"favorite": favorite, "tags": tags}
        self.image_state = pruned

    @staticmethod
    def _normalize_tags(tags_raw: object) -> list[str]:
        if not isinstance(tags_raw, list):
            return []
        return sorted({str(tag).strip().lower() for tag in tags_raw if str(tag).strip()})

    @staticmethod
    def _metadata_signature(path: Path) -> tuple[int, int, int]:
        try:
            stat = path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size), METADATA_PARSE_REV
        except Exception:
            return -1, -1, METADATA_PARSE_REV

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
        prompt_text = str(metadata.get("Prompt", "")).strip().lower()
        haystack_parts = [path.name.lower()]
        if tags:
            haystack_parts.append(" ".join(tags))
        for field in ("Prompt", "Negative prompt", "Model", "Sampler", "Scheduler", "Seed", "CFG", "Steps", "Resolution", "LoRAs"):
            value = str(metadata.get(field, "")).strip().lower()
            if value:
                haystack_parts.append(value)
        search_text = " | ".join(haystack_parts)

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
        best: Path | None = None
        for root in roots:
            try:
                path.relative_to(root)
            except Exception:
                continue
            if best is None or len(root.parts) > len(best.parts):
                best = root
        return best

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
        self.sort_var.set("Newest")
        self.apply_filters()

    def _schedule_apply_filters(self) -> None:
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(220, self.apply_filters)

    def apply_filters(self) -> None:
        self._filter_after_id = None
        if not self.all_image_paths:
            self._filter_token += 1
            self._render_thumbnails([])
            return

        query = self.search_var.get().strip().lower()
        tag_filter = self.tag_filter_var.get().strip().lower()
        favorites_only = self.favorites_only_var.get()
        sort_mode = self.sort_var.get()
        self._filter_token += 1
        token = self._filter_token
        if query or tag_filter:
            self._cancel_metadata_warmup()
            self._set_status("Filtering images...", "busy")

        self._filter_worker_thread = threading.Thread(
            target=self._filter_worker,
            args=(token, list(self.all_image_paths), query, tag_filter, favorites_only, sort_mode),
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
        sort_mode: str,
    ) -> None:
        filtered: list[Path] = []
        for path in paths:
            if token != self._filter_token:
                return
            state = self._get_image_state(path)
            favorite = bool(state.get("favorite", False))

            if favorites_only and not favorite:
                continue
            if tag_filter or query:
                prompt_text, search_text = self._get_search_index_record(path)
                if tag_filter and tag_filter not in prompt_text:
                    continue
                if query and query not in search_text:
                    continue

            filtered.append(path)

        reverse = sort_mode != "Oldest"
        filtered.sort(key=self._sort_mtime_cached, reverse=reverse)
        self.after(0, lambda: self._on_filters_ready(token, filtered))

    def _on_filters_ready(self, token: int, filtered: list[Path]) -> None:
        if token != self._filter_token:
            return
        self._render_thumbnails(filtered)

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
            found_by_key: dict[str, Path] = {}
            root_by_key: dict[str, Path] = {}
            mtime_by_key: dict[str, float] = {}
            for root in roots:
                if token != self._scan_token:
                    return
                for file_path in root.rglob("*"):
                    if token != self._scan_token:
                        return
                    try:
                        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                            continue
                    except Exception:
                        continue
                    mtime: float | None = None
                    try:
                        mtime = float(file_path.stat().st_mtime)
                    except Exception:
                        pass
                    try:
                        canonical = file_path.resolve()
                    except Exception:
                        canonical = file_path
                    key = str(canonical)
                    if key not in found_by_key:
                        found_by_key[key] = canonical
                        if mtime is not None:
                            mtime_by_key[key] = mtime
                    elif mtime is not None and mtime > mtime_by_key.get(key, 0.0):
                        mtime_by_key[key] = mtime
                    prev_root = root_by_key.get(key)
                    if prev_root is None or len(root.parts) > len(prev_root.parts):
                        root_by_key[key] = root
            found_keys = sorted(found_by_key.keys(), key=lambda k: mtime_by_key.get(k, 0.0), reverse=True)
            found = [found_by_key[key] for key in found_keys]
            if token != self._scan_token:
                return
            self.after(0, lambda: self._on_scan_complete(token, found, root_by_key, mtime_by_key))
        except Exception as exc:  # noqa: BLE001
            if token != self._scan_token:
                return
            self.after(0, lambda: self._set_status(f"Scan error: {exc}", "error"))

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
            self.metadata_cache = {k: v for k, v in self.metadata_cache.items() if k in existing}
            self.metadata_cache_sig = {k: v for k, v in self.metadata_cache_sig.items() if k in existing}
            self.search_index_cache = {k: v for k, v in self.search_index_cache.items() if k in existing}
            self.thumbnail_cache = OrderedDict(
                (k, v) for k, v in self.thumbnail_cache.items() if k[0] in existing
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
            self._set_metadata_text("No images found")
            self.current_image_path = None
            self._sync_current_controls(None, None)
            self._thumb_rendering = False
            self._update_canvas_window_size()
            return

        self._thumb_render_size = max(100, min(320, int(round(self.thumb_size_var.get()))))
        self._thumb_render_columns = self._safe_columns()
        self._thumb_name_font = tkfont.Font(family="Segoe UI", size=10)
        name_line_h = int(self._thumb_name_font.metrics("linespace"))
        self._thumb_show_caption = self._thumb_render_size >= 150
        self._thumb_caption_height = max(24, name_line_h + 10) if self._thumb_show_caption else 0
        self._thumb_inner_pad = 6
        self._thumb_cell_width = self._thumb_render_size + 16
        self._thumb_cell_height = (
            self._thumb_render_size
            + (self._thumb_inner_pad * 2)
            + self._thumb_caption_height
            + 2
        )
        self._thumb_cell_gap_x = 3
        self._thumb_cell_gap_y = 3
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
        if selected_path and selected_path in self.image_paths:
            idx = self.image_paths.index(selected_path)
            self.on_thumbnail_click(idx)
        else:
            self.current_image_path = None
            self._sync_current_controls(None, None)
            self._set_metadata_text("Select an image to view metadata")

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
            outline=PALETTE["thumb_shadow"],
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
                fill=PALETTE["text"],
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
        panel = self._build_details_view(path, metadata)
        self.current_image_path = path
        self._sync_current_controls(path, metadata)
        self._refresh_thumb_cell_highlight()
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
        shell = self.thumb_cells_by_path.get(key)
        cell = self.thumb_inner_by_path.get(key)
        widgets = self.thumb_widget_by_path.get(key)
        if shell is None or cell is None or not widgets:
            return
        image_item, text_item = widgets
        if self.current_image_path and key == str(self.current_image_path):
            self.canvas.itemconfigure(shell, fill=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent_active"], 0.18), outline=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent_active"], 0.18))
            self.canvas.itemconfigure(cell, outline=PALETTE["accent_active"], fill=PALETTE["thumb_selected_bg"])
            self.canvas.itemconfigure(image_item, state="normal")
            if text_item is not None:
                self.canvas.itemconfigure(text_item, fill=PALETTE["text"])
            return
        if entering:
            self._hover_path_key = key
            self.canvas.configure(cursor="hand2")
            self.canvas.itemconfigure(shell, fill=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent"], 0.12), outline=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent"], 0.12))
            self.canvas.itemconfigure(cell, outline=self._mix_hex(PALETTE["border"], PALETTE["accent"], 0.25), fill=PALETTE["thumb_hover_bg"])
            if text_item is not None:
                self.canvas.itemconfigure(text_item, fill=PALETTE["text"])
        else:
            if self._hover_path_key == key:
                self._hover_path_key = None
                self.canvas.configure(cursor="")
            self.canvas.itemconfigure(shell, fill=PALETTE["thumb_shadow"], outline=PALETTE["thumb_shadow"])
            self.canvas.itemconfigure(cell, outline=PALETTE["border"], fill=PALETTE["thumb_bg"])
            if text_item is not None:
                self.canvas.itemconfigure(text_item, fill=PALETTE["text"])

    def _refresh_thumb_cell_highlight(self) -> None:
        selected_key = str(self.current_image_path) if self.current_image_path else ""
        for key, shell in self.thumb_cells_by_path.items():
            cell = self.thumb_inner_by_path.get(key)
            widgets = self.thumb_widget_by_path.get(key)
            if not widgets or cell is None:
                continue
            image_item, text_item = widgets
            if key == selected_key:
                self.canvas.itemconfigure(shell, fill=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent_active"], 0.18), outline=self._mix_hex(PALETTE["thumb_shadow"], PALETTE["accent_active"], 0.18))
                self.canvas.itemconfigure(cell, outline=PALETTE["accent_active"], fill=PALETTE["thumb_selected_bg"])
                self.canvas.itemconfigure(image_item, state="normal")
                if text_item is not None:
                    self.canvas.itemconfigure(text_item, fill=PALETTE["text"])
            else:
                self.canvas.itemconfigure(shell, fill=PALETTE["thumb_shadow"], outline=PALETTE["thumb_shadow"])
                self.canvas.itemconfigure(cell, outline=PALETTE["border"], fill=PALETTE["thumb_bg"])
                self.canvas.itemconfigure(image_item, state="normal")
                if text_item is not None:
                    self.canvas.itemconfigure(text_item, fill=PALETTE["text"])

    def copy_current_prompt(self) -> None:
        if not self.current_image_path:
            messagebox.showinfo("No image selected", "Select an image first.")
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
            messagebox.showinfo("No image selected", "Select an image first.")
            return
        state = self._get_image_state(self.current_image_path, create=True)
        state["favorite"] = not bool(state.get("favorite", False))
        self._prune_image_state()
        self._schedule_save_state()
        self._sync_current_controls(self.current_image_path, self._get_metadata_cached(self.current_image_path))
        self.apply_filters()

    def save_current_tags(self) -> None:
        if not self.current_image_path:
            messagebox.showinfo("No image selected", "Select an image first.")
            return
        raw = self.current_tags_var.get()
        tags = [part.strip() for part in raw.split(",") if part.strip()]
        unique = sorted(set(tags), key=lambda s: s.lower())
        state = self._get_image_state(self.current_image_path, create=True)
        state["tags"] = unique
        self._prune_image_state()
        self._invalidate_search_index(self.current_image_path)
        self._schedule_save_state()
        self._set_status(f"Saved tags: {', '.join(unique) if unique else 'None'}", "ok")
        self.apply_filters()

    def _sync_current_controls(self, path: Path | None, metadata: dict[str, str] | None) -> None:
        if path is None:
            self.favorite_btn.configure(text="[*] Favorite")
            self.current_tags_var.set("")
            self.copy_prompt_btn.configure(state="disabled")
            self.favorite_btn.configure(state="disabled")
            self.save_tags_btn.configure(state="disabled")
            self._update_inspector_summary(None, None)
            return

        self.copy_prompt_btn.configure(state="normal")
        self.favorite_btn.configure(state="normal")
        self.save_tags_btn.configure(state="normal")
        state = self._get_image_state(path)
        is_favorite = bool(state.get("favorite", False))
        self.favorite_btn.configure(text="[*] Favorited" if is_favorite else "[*] Favorite")
        tags = [str(x) for x in state.get("tags", []) if str(x).strip()]
        self.current_tags_var.set(", ".join(tags))
        self._update_inspector_summary(path, metadata)

    def _update_inspector_summary(self, path: Path | None, metadata: dict[str, str] | None) -> None:
        if path is None or metadata is None:
            self.meta_file_var.set("No image selected")
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
        result: dict[str, str] = {}

        try:
            with Image.open(path) as im:
                result["_width"] = str(im.width)
                result["_height"] = str(im.height)

                for key, value in (im.info or {}).items():
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    result[str(key)] = str(value)

                exif = im.getexif()
                if exif:
                    for tag_id, value in exif.items():
                        result[f"EXIF_{tag_id}"] = str(value)
        except Exception as exc:  # noqa: BLE001
            result["_error"] = str(exc)

        normalized = self._extract_generation_fields(result)
        result.update(normalized)
        return result

    def _extract_generation_fields(self, raw: dict[str, str]) -> dict[str, str]:
        parsed: dict[str, str] = {}

        for key in ("parameters", "Comment", "comment", "UserComment"):
            if key not in raw:
                continue
            text = str(raw[key])
            if "Negative prompt:" in text or "Steps:" in text:
                prompt, neg, tail = self._parse_sd_parameters(text)
                if prompt:
                    parsed.setdefault("Prompt", prompt)
                if neg:
                    parsed.setdefault("Negative prompt", neg)
                parsed.update({k: v for k, v in tail.items() if k not in parsed})

        prompt_json = raw.get("prompt")
        if prompt_json and str(prompt_json).lstrip().startswith("{"):
            comfy = self._parse_comfy_prompt_json(str(prompt_json))
            parsed.update({k: v for k, v in comfy.items() if v and k not in parsed})

        # Some generators keep workflow/extra metadata in additional JSON blocks.
        for key in ("workflow", "extra_pnginfo"):
            block = raw.get(key)
            if not block:
                continue
            text = str(block)
            if text.lstrip().startswith("{"):
                comfy2 = self._parse_comfy_prompt_json(text)
                parsed.update({k: v for k, v in comfy2.items() if v and k not in parsed})
            model_guess = self._guess_model_from_text(text)
            if model_guess and "Model" not in parsed:
                parsed["Model"] = model_guess

        pick_map = {
            "Model": [
                "Model",
                "model",
                "model_name",
                "sd_model_name",
                "sd_model_hash",
                "ckpt_name",
                "unet_name",
                "checkpoint",
                "checkpoint_name",
            ],
            "Prompt": ["Prompt", "prompt_text", "prompt"],
            "Negative prompt": ["Negative prompt", "negative_prompt", "negative"],
            "Sampler": ["Sampler", "sampler", "sampler_name"],
            "Scheduler": ["Scheduler", "scheduler", "Schedule type"],
            "CFG": ["CFG", "CFG scale", "cfg", "cfg_scale"],
            "Seed": ["Seed", "seed"],
            "Steps": ["Steps", "steps"],
            "Resolution": ["Resolution", "Size", "size"],
        }

        for target, keys in pick_map.items():
            for key in keys:
                if key in parsed and parsed[key]:
                    value = str(parsed[key])
                    if target == "Model":
                        value = self._normalize_model_value(value)
                        if not value:
                            continue
                    parsed[target] = value
                    break
                if key in raw and raw[key]:
                    value = str(raw[key])
                    if target == "Model":
                        value = self._normalize_model_value(value)
                        if not value:
                            continue
                    parsed[target] = value
                    break

        loras_found: set[str] = set()
        excluded_assets: set[str] = set()

        # 1) Prefer structured workflow parsing for Comfy/rgthree LoRA nodes.
        for key in ("prompt", "workflow", "extra_pnginfo"):
            value = raw.get(key)
            if value:
                value_text = str(value)
                loras_found.update(self._extract_loras_from_workflow_json(value_text))
                excluded_assets.update(self._extract_model_assets_from_workflow_json(value_text))
                excluded_assets.update(self._extract_non_lora_assets_from_text(value_text))

        # 2) Fallback to explicit LoRA-like textual fields only (avoid full workflow notes noise).
        for key in (
            "LoRAs",
            "loras",
            "lora",
            "Lora hashes",
            "parameters",
            "Comment",
            "comment",
            "UserComment",
        ):
            value = raw.get(key)
            if value:
                value_text = str(value)
                loras_found.update(self._extract_loras_from_text(value_text))
                excluded_assets.update(self._extract_non_lora_assets_from_text(value_text))

        # 3) Also parse already normalized positive prompt for <lora:...> entries.
        prompt_norm = parsed.get("Prompt", "")
        if prompt_norm:
            loras_found.update(self._extract_loras_from_text(prompt_norm))
            excluded_assets.update(self._extract_non_lora_assets_from_text(prompt_norm))

        model_hint = self._clean_lora_name(parsed.get("Model", ""))
        if model_hint:
            excluded_assets.add(model_hint)

        if loras_found:
            canonical_excluded = {self._canonical_asset_name(item) for item in excluded_assets if item}
            loras_found = {
                item
                for item in loras_found
                if self._canonical_asset_name(item) not in canonical_excluded and not self._looks_like_non_lora_asset(item)
            }
            if loras_found:
                parsed["LoRAs"] = ", ".join(sorted(loras_found, key=str.lower))

        return parsed

    def _parse_comfy_prompt_json(self, text: str) -> dict[str, str]:
        out: dict[str, str] = {}

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return out

            # Comfy may wrap the actual prompt graph in nested objects.
            if "prompt" in data and isinstance(data["prompt"], dict):
                data = data["prompt"]

            clip_texts: list[str] = []
            loras: list[str] = []

            for node in data.values():
                if not isinstance(node, dict):
                    continue
                cls = str(node.get("class_type", ""))
                inputs = node.get("inputs", {})
                if not isinstance(inputs, dict):
                    continue

                if cls == "CLIPTextEncode" and isinstance(inputs.get("text"), str):
                    clip_texts.append(inputs["text"].strip())

                if "KSampler" in cls:
                    out.setdefault("Sampler", str(inputs.get("sampler_name", "")))
                    out.setdefault("Scheduler", str(inputs.get("scheduler", "")))
                    out.setdefault("Seed", str(inputs.get("seed", "")))
                    out.setdefault("CFG", str(inputs.get("cfg", "")))
                    out.setdefault("Steps", str(inputs.get("steps", "")))

                if "CheckpointLoader" in cls or "UNETLoader" in cls or "DiffusionModelLoader" in cls:
                    for key in (
                        "ckpt_name",
                        "unet_name",
                        "model_name",
                        "checkpoint_name",
                        "checkpoint",
                        "model",
                    ):
                        value = self._normalize_model_value(str(inputs.get(key, "")).strip())
                        if value:
                            out.setdefault("Model", value)
                            break

                if "EmptyLatentImage" in cls:
                    w = str(inputs.get("width", "")).strip()
                    h = str(inputs.get("height", "")).strip()
                    if w and h:
                        out.setdefault("Resolution", f"{w}x{h}")

                if "lora" in cls.lower():
                    for k, v in inputs.items():
                        if not isinstance(v, str):
                            continue
                        k_low = str(k).lower()
                        is_explicit_lora_key = bool(
                            re.fullmatch(r"lora(_\d+)?_name", k_low)
                            or k_low in {"lora_name", "lora_file", "lora_path", "lora"}
                        )
                        if is_explicit_lora_key:
                            name = self._clean_lora_name(v)
                            if name and not self._looks_like_non_lora_asset(name):
                                loras.append(name)
                    if "lora_name" in inputs:
                        name = self._clean_lora_name(str(inputs.get("lora_name", "")))
                        if name and not self._looks_like_non_lora_asset(name):
                            loras.append(name)

                if "Model" not in out:
                    for key in ("ckpt_name", "unet_name", "model_name", "checkpoint_name", "checkpoint", "model"):
                        value = self._normalize_model_value(str(inputs.get(key, "")).strip())
                        if value:
                            out["Model"] = value
                            break

            if clip_texts:
                out.setdefault("Prompt", clip_texts[0])
            if len(clip_texts) > 1:
                out.setdefault("Negative prompt", clip_texts[1])
            if loras:
                out.setdefault("LoRAs", ", ".join(sorted(set(loras), key=str.lower)))
            else:
                # Fallback for custom loaders (e.g. rgthree power lora nodes).
                lora_guess = self._extract_loras_from_json_obj(data)
                if lora_guess:
                    out.setdefault("LoRAs", ", ".join(sorted(lora_guess, key=str.lower)))

            if "Model" not in out:
                model_guess = self._guess_model_from_text(text)
                if model_guess:
                    out["Model"] = model_guess

            return out
        except Exception:
            return out

    @staticmethod
    def _guess_model_from_text(text: str) -> str:
        # Fallback for workflows that don't expose a direct "Model" field.
        patterns = [
            r"([A-Za-z0-9_./\\-]+\.(?:safetensors|ckpt|pth))",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _normalize_model_value(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        # Comfy node-link placeholders like "['50', 0]" or "[50, 0]" are not model names.
        if re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", value):
            return ""
        if value.lower() in {"none", "null", "[]"}:
            return ""
        return value

    @staticmethod
    def _clean_lora_name(value: str) -> str:
        value = (value or "").strip().strip("\"'")
        if not value:
            return ""
        if re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", value):
            return ""
        value = value.replace("\\", "/")
        if "/" in value:
            value = value.split("/")[-1]
        if ":" in value and not value.lower().endswith(":safetensors"):
            # Handle entries like "name: hash" and keep left side.
            left = value.split(":", 1)[0].strip()
            if left:
                value = left
        if value.lower().endswith(".safetensors"):
            value = value[:-12]
        return value.strip()

    def _extract_loras_from_text(self, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found

        maybe_json = text.lstrip()
        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                data = json.loads(maybe_json)
                found.update(self._extract_loras_from_json_obj(data))
            except Exception:
                pass

        for match in re.findall(r"<lora:([^:>]+)(?::[^>]+)?>", text, flags=re.IGNORECASE):
            name = self._clean_lora_name(match)
            if name:
                found.add(name)

        # JSON style: "lora_name": "xxx.safetensors", "lora_1_name": "..."
        for match in re.findall(
            r'(?i)"(?:lora(?:_[0-9]+)?_name|lora_name|lora_file|lora_path)"\s*:\s*"([^"]+)"',
            text,
        ):
            name = self._clean_lora_name(match)
            if name:
                found.add(name)

        # Non-JSON / flat metadata style.
        for match in re.findall(
            r"(?i)\b(?:lora(?:_[0-9]+)?_name|lora_name|lora_file|lora_path)\s*[:=]\s*([^\n\r,]+)",
            text,
        ):
            name = self._clean_lora_name(match)
            if name:
                found.add(name)

        # A1111 "Lora hashes: name: hash, name2: hash2"
        for block in re.findall(r"(?i)Lora hashes\s*:\s*([^\n\r]+)", text):
            for part in block.split(","):
                candidate = part.split(":", 1)[0].strip()
                name = self._clean_lora_name(candidate)
                if name:
                    found.add(name)

        return found

    def _extract_loras_from_json_obj(self, obj: object, in_lora_context: bool = False) -> set[str]:
        found: set[str] = set()

        if isinstance(obj, dict):
            class_type = str(obj.get("class_type", "")).lower()
            context_here = in_lora_context or ("lora" in class_type)

            for key, value in obj.items():
                key_low = str(key).lower()
                key_is_lora = "lora" in key_low or key_low.startswith("add_lora")
                child_context = context_here or key_is_lora

                if isinstance(value, str):
                    if self._looks_like_lora_value(value, key_low, child_context):
                        cleaned = self._clean_lora_name(value)
                        if cleaned:
                            found.add(cleaned)
                elif isinstance(value, (dict, list)):
                    found.update(self._extract_loras_from_json_obj(value, child_context))

        elif isinstance(obj, list):
            for item in obj:
                found.update(self._extract_loras_from_json_obj(item, in_lora_context))

        return found

    @staticmethod
    def _looks_like_lora_value(value: str, key_low: str, in_lora_context: bool) -> bool:
        v = (value or "").strip()
        if not v:
            return False
        if re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", v):
            return False
        if v.lower() in {"none", "null", "false", "true"}:
            return False

        lv = v.lower()
        is_safetensors = lv.endswith(".safetensors")
        key_model_like = any(
            x in key_low
            for x in (
                "model",
                "ckpt",
                "checkpoint",
                "unet",
                "base_model",
                "diffusion_model",
                "vae",
                "clip",
                "text_encoder",
                "textencoder",
                "tokenizer",
                "encoder",
            )
        )
        key_has_lora = "lora" in key_low
        non_lora_name = ImageMetadataViewer._looks_like_non_lora_asset(v)

        # Strong signal: explicit LoRA key or node context.
        if key_has_lora and (is_safetensors or "." not in lv):
            return True

        # Generic fallback for keys that often hold named LoRA slots.
        if is_safetensors and any(x in key_low for x in ("name", "file", "path")) and not key_model_like and not non_lora_name:
            return True

        return False

    @staticmethod
    def _looks_like_non_lora_asset(value: str) -> bool:
        v = (value or "").strip().lower().replace("\\", "/")
        if not v:
            return False
        if v.endswith(".safetensors"):
            v = v[:-12]
        name = v.split("/")[-1]
        bad_tokens = (
            "vae",
            "text_encoder",
            "text-encoder",
            "clip",
            "tokenizer",
            "unet",
            "checkpoint",
            "ckpt",
            "diffusion_model",
            "image_vae",
            "textencoder",
        )
        return any(token in name for token in bad_tokens)

    def _extract_non_lora_assets_from_text(self, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found

        maybe_json = text.lstrip()
        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                data = json.loads(maybe_json)
                found.update(self._extract_non_lora_assets_from_json_obj(data))
            except Exception:
                pass

        non_lora_keys = {
            "ckpt_name",
            "checkpoint_name",
            "checkpoint",
            "model_name",
            "model",
            "unet_name",
            "vae_name",
            "vae",
            "text_encoder",
            "clip_name",
            "clip",
        }
        patterns = [
            r'(?i)"(ckpt_name|checkpoint_name|checkpoint|model_name|model|unet_name|vae_name|text_encoder|clip_name|clip|vae)"\s*:\s*"([^"]+)"',
            r"(?i)\b(ckpt_name|checkpoint_name|checkpoint|model_name|model|unet_name|vae_name|text_encoder|clip_name|clip|vae)\s*[:=]\s*([^\n\r,]+)",
        ]
        for pattern in patterns:
            for key, value in re.findall(pattern, text):
                key_low = str(key).strip().lower()
                cleaned = self._clean_lora_name(value)
                if not cleaned:
                    continue
                # Strong exclude by key semantics.
                if key_low in non_lora_keys and "lora" not in key_low:
                    found.add(cleaned)
                    continue
                if self._looks_like_non_lora_asset(cleaned):
                    found.add(cleaned)
                elif any(token in cleaned.lower() for token in ("qwen", "vae", "text_encoder", "clip", "unet")):
                    found.add(cleaned)
        return found

    def _extract_non_lora_assets_from_json_obj(self, obj: object) -> set[str]:
        found: set[str] = set()
        model_keys = {
            "ckpt_name",
            "checkpoint_name",
            "checkpoint",
            "model_name",
            "model",
            "unet_name",
            "vae_name",
            "vae",
            "text_encoder",
            "clip_name",
            "clip",
            "base_model",
            "diffusion_model",
        }

        if isinstance(obj, dict):
            for key, value in obj.items():
                key_low = str(key).lower()
                if isinstance(value, str):
                    cleaned = self._clean_lora_name(value)
                    if not cleaned:
                        continue
                    if key_low in model_keys and "lora" not in key_low:
                        found.add(cleaned)
                    elif self._looks_like_non_lora_asset(cleaned):
                        found.add(cleaned)
                elif isinstance(value, (dict, list)):
                    found.update(self._extract_non_lora_assets_from_json_obj(value))
        elif isinstance(obj, list):
            for item in obj:
                found.update(self._extract_non_lora_assets_from_json_obj(item))

        return found

    def _parse_sd_parameters(self, text: str) -> tuple[str, str, dict[str, str]]:
        prompt = ""
        negative = ""
        tail_data: dict[str, str] = {}

        if "Negative prompt:" in text:
            prompt_part, rest = text.split("Negative prompt:", 1)
            prompt = prompt_part.strip()
            if "Steps:" in rest:
                neg_part, tail = rest.split("Steps:", 1)
                negative = neg_part.strip()
                tail_data.update(self._parse_kv_tail("Steps:" + tail))
            else:
                negative = rest.strip()
        elif "Steps:" in text:
            prompt_part, tail = text.split("Steps:", 1)
            prompt = prompt_part.strip()
            tail_data.update(self._parse_kv_tail("Steps:" + tail))
        else:
            prompt = text.strip()

        return prompt, negative, tail_data

    def _parse_kv_tail(self, tail: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in tail.split(","):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                out[key] = value

        if "CFG scale" in out and "CFG" not in out:
            out["CFG"] = out["CFG scale"]
        if "Size" in out and "Resolution" not in out:
            out["Resolution"] = out["Size"]

        return out

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
        for key in keys:
            value = str(data.get(key, "")).strip()
            if value:
                return value
        return ""

    def _extract_loras(self, prompt: str, metadata: dict[str, str]) -> str:
        loras: set[str] = set()
        excluded: set[str] = set()
        loras.update(self._extract_loras_from_text(prompt))
        excluded.update(self._extract_non_lora_assets_from_text(prompt))

        # Structured LoRA extraction from Comfy workflow blocks.
        for key in ("prompt", "workflow", "extra_pnginfo"):
            value = str(metadata.get(key, "")).strip()
            if value:
                loras.update(self._extract_loras_from_workflow_json(value))
                excluded.update(self._extract_model_assets_from_workflow_json(value))
                excluded.update(self._extract_non_lora_assets_from_text(value))

        # Textual fallback for explicit LoRA fields only.
        for key in ("LoRAs", "loras", "lora", "Lora hashes", "parameters", "Comment", "comment", "UserComment"):
            value = str(metadata.get(key, "")).strip()
            if value:
                loras.update(self._extract_loras_from_text(value))
                excluded.update(self._extract_non_lora_assets_from_text(value))

        # Remove accidental model name from LoRA list when both are similar.
        model_name = self._clean_lora_name(self._pick(metadata, ["Model"]))
        if model_name and model_name in loras:
            loras.discard(model_name)
        if model_name:
            excluded.add(model_name)

        if excluded:
            canonical_excluded = {self._canonical_asset_name(item) for item in excluded if item}
            loras = {item for item in loras if self._canonical_asset_name(item) not in canonical_excluded}
        loras = {item for item in loras if not self._looks_like_non_lora_asset(item)}

        return ", ".join(sorted(loras, key=str.lower)) if loras else "None"

    @staticmethod
    def _canonical_asset_name(value: str) -> str:
        cleaned = ImageMetadataViewer._clean_lora_name(value).lower()
        if not cleaned:
            return ""
        return re.sub(r"[^a-z0-9]+", "", cleaned)

    @staticmethod
    def _is_modelish_node_type(node_type: str) -> bool:
        low = (node_type or "").strip().lower()
        if not low or "lora" in low:
            return False
        model_tokens = (
            "loader",
            "checkpoint",
            "ckpt",
            "unet",
            "vae",
            "clip",
            "textencoder",
            "text_encoder",
            "diffusion",
            "model",
        )
        return any(token in low for token in model_tokens)

    @staticmethod
    def _iter_strings(value: object) -> list[str]:
        out: list[str] = []
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                out.extend(ImageMetadataViewer._iter_strings(v))
        elif isinstance(value, list):
            for item in value:
                out.extend(ImageMetadataViewer._iter_strings(item))
        return out

    def _extract_model_assets_from_workflow_json(self, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found

        maybe_json = text.lstrip()
        if not (maybe_json.startswith("{") and maybe_json.endswith("}")):
            return found
        try:
            data = json.loads(maybe_json)
        except Exception:
            return found

        def add_candidate(raw_value: str) -> None:
            cleaned = self._clean_lora_name(raw_value)
            if cleaned:
                found.add(cleaned)

        # Comfy workflow format: {"nodes":[...]}
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            for node in data["nodes"]:
                if not isinstance(node, dict):
                    continue
                node_type = str(node.get("type", ""))
                if not self._is_modelish_node_type(node_type):
                    continue

                widgets = node.get("widgets_values")
                if isinstance(widgets, list):
                    for value in self._iter_strings(widgets):
                        low = value.lower()
                        if any(ext in low for ext in (".safetensors", ".ckpt", ".pth", ".pt")):
                            add_candidate(value)

                props = node.get("properties")
                if isinstance(props, dict):
                    models = props.get("models")
                    if isinstance(models, list):
                        for item in models:
                            if not isinstance(item, dict):
                                continue
                            for key in ("name", "path", "model", "checkpoint", "ckpt_name", "model_name"):
                                value = item.get(key)
                                if isinstance(value, str):
                                    add_candidate(value)

        # Comfy prompt format: {"1":{"class_type":"...","inputs":{...}}, ...}
        prompt_obj: object = data
        if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
            prompt_obj = data["prompt"]
        if isinstance(prompt_obj, dict):
            for node in prompt_obj.values():
                if not isinstance(node, dict):
                    continue
                class_type = str(node.get("class_type", ""))
                if not self._is_modelish_node_type(class_type):
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict):
                    continue
                for key in (
                    "ckpt_name",
                    "checkpoint_name",
                    "checkpoint",
                    "model_name",
                    "model",
                    "unet_name",
                    "vae_name",
                    "vae",
                    "clip_name",
                    "clip",
                    "text_encoder",
                    "text_encoder_name",
                ):
                    value = inputs.get(key)
                    if isinstance(value, str):
                        add_candidate(value)

        return found

    def _extract_loras_from_workflow_json(self, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found
        maybe_json = text.lstrip()
        if not (maybe_json.startswith("{") and maybe_json.endswith("}")):
            return found
        try:
            data = json.loads(maybe_json)
        except Exception:
            return found

        # Comfy workflow format: {"nodes":[...]}
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            for node in data["nodes"]:
                if not isinstance(node, dict):
                    continue
                node_type = str(node.get("type", "")).lower()
                if "lora" not in node_type:
                    continue
                found.update(self._extract_loras_from_lora_node(node))

        # Comfy prompt format: {"1":{"class_type":"...","inputs":{...}}, ...}
        prompt_obj: object = data
        if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
            prompt_obj = data["prompt"]
        if isinstance(prompt_obj, dict):
            for node in prompt_obj.values():
                if not isinstance(node, dict):
                    continue
                class_type = str(node.get("class_type", "")).lower()
                if "lora" not in class_type:
                    continue
                inputs = node.get("inputs", {})
                if isinstance(inputs, dict):
                    found.update(self._extract_loras_from_lora_payload(inputs))

        return found

    def _extract_loras_from_lora_node(self, node: dict[str, object]) -> set[str]:
        found: set[str] = set()
        widgets = node.get("widgets_values")
        if isinstance(widgets, list):
            for item in widgets:
                found.update(self._extract_loras_from_lora_payload(item))
        return found

    def _extract_loras_from_lora_payload(self, payload: object) -> set[str]:
        found: set[str] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_low = str(key).lower()
                if isinstance(value, str):
                    if re.fullmatch(r"lora(_\d+)?", key_low) or re.fullmatch(r"lora(_\d+)?_name", key_low) or key_low in {"lora_name", "lora_file", "lora_path", "lora_model_name"}:
                        name = self._clean_lora_name(value)
                        if name and not self._looks_like_non_lora_asset(name):
                            found.add(name)
                elif isinstance(value, (dict, list)):
                    found.update(self._extract_loras_from_lora_payload(value))
        elif isinstance(payload, list):
            for item in payload:
                found.update(self._extract_loras_from_lora_payload(item))
        return found

    def _safe_columns(self) -> int:
        value = self.columns_var.get().strip()
        if not value.isdigit():
            return DEFAULT_COLUMNS
        return max(2, min(12, int(value)))

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

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
        try:
            stat = path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size)
        except Exception:
            return -1, -1

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

