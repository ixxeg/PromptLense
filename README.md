# PromptLens

Local Windows desktop app (Tkinter) for browsing generated images, reviewing generations, and reading embedded metadata.

<img width="3838" height="2051" alt="image" src="https://github.com/user-attachments/assets/d67c5abe-e1f1-4b61-96d0-b03c39b5ad6c" />


PromptLens is built for image-generation workflows where files already contain useful metadata. It scans one or more folders, builds a fast thumbnail catalog, shows generation details in a structured inspector, and adds a lightweight review pass for favorites, rejects, and custom tags.

## Features
- Recursive multi-folder scan
- Support for `png`, `jpg`, `jpeg`, `webp`, `bmp`, `tif`, `tiff`
- Adjustable thumbnail size and grid columns
- Search, prompt-tag filter, favorites-only filter, review filter, and newest/oldest sorting
- Inspector with prompt, negative prompt, model, sampler, scheduler, seed, CFG, steps, resolution, file size, and LoRAs when available
- Full preview on double-click, plus inline preview in review mode
- Review workflow with `Favorite`, `Reject`, `Reset`, and bulk delete of rejected images to Recycle Bin
- Clipboard helpers for prompt text and inspector summary values
- GitHub release check from the app menu

## Requirements
- Windows
- Python 3.10+

## Quick Start
```powershell
py -m pip install -r requirements.txt
py app.py
```

## Build EXE
Recommended `onedir` build:
```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```
Output: `dist\PromptLens\PromptLens.exe`

Single-file build:
```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile
```
Output: `dist\PromptLens.exe`

## Controls
- Gallery: mouse wheel scroll, `Shift + wheel` for horizontal scroll, click to inspect, double-click to open full preview
- Review mode: `V` toggle, `F` favorite, `R` reject, `U` reset
- Preview window: mouse wheel zoom, `+` / `-`, `Ctrl + 0`, `F` fit
- Inspector: copy positive prompt, save comma-separated tags, click summary chips to copy values

## State
PromptLens writes `.image_catalog_state.json` next to `app.py` in source mode or next to the `.exe` in packaged mode. The file is updated after closing the app and stores selected folders, UI preferences, and per-image review state.

## Metadata
- Reads metadata from `PIL.Image.info` and EXIF
- Parses A1111-style `parameters`
- Parses ComfyUI JSON blocks (`prompt` / workflow metadata)
- Available fields depend on the generator and workflow

## Updates
`App -> Check for updates` checks the latest GitHub release. In EXE mode, if a newer `.exe` asset is available, PromptLens can download it and restart into the updated version.

## Tests
```powershell
py -m unittest tests.test_app_helpers
```

## Project Files
- `app.py` - main application
- `app_helpers.py` - metadata parsing, preview, state, filtering, review helpers
- `build_exe.ps1` - PyInstaller build script
- `PromptLens.spec` - PyInstaller spec file
