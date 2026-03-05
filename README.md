# PromptLens

Local desktop app (Tkinter) for browsing generated images and reading generation metadata.

<img width="3839" height="2044" alt="image" src="https://github.com/user-attachments/assets/1d786320-9745-4ec4-9572-446558b30597" />


## Highlights
- Multi-folder scan (recursive)
- Fast thumbnail catalog with adjustable:
  - thumb size
  - columns
- Full preview on double-click with zoom controls
- Structured metadata panel:
  - Prompt / Negative prompt
  - Model / Sampler / Scheduler
  - Seed / CFG / Steps
  - Resolution / file size
  - LoRAs
- Favorites + custom tags
- Search/filter/sort:
  - global search (filename + key metadata fields)
  - prompt tag filter
  - favorites-only
  - `Newest` / `Oldest`
- Subfolder markers:
  - colored dot per subfolder group
  - tooltip with folder path

## Requirements
- Windows
- Python 3.10+ (tested with newer versions too)

## Quick Start
```powershell
py -m pip install -r requirements.txt
py app.py
```

## Build EXE (Windows)
### Recommended (`onedir`)
```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```
Output:
- `dist\LocalImageMetadataCatalog\LocalImageMetadataCatalog.exe`

### Single-file (`onefile`)
```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile
```
Output:
- `dist\LocalImageMetadataCatalog.exe`

## Controls
- Catalog:
  - mouse wheel: vertical scroll
  - shift + wheel: horizontal scroll
  - click thumbnail: select + metadata
  - double-click thumbnail: full preview
- Preview window:
  - mouse wheel: zoom in/out
  - `+` / `-`: zoom
  - `Ctrl + 0`: 100%
  - `F`: fit to window

## Data & State
- App state file: `.image_catalog_state.json`
- In script mode: stored next to `app.py`
- In EXE mode: stored next to `.exe`
- Stores:
  - selected folders
  - UI preferences
  - favorites/tags

## Metadata Notes
- Reads metadata from `PIL.Image.info` and EXIF.
- Parses A1111-style `parameters`.
- Parses ComfyUI JSON blocks (`prompt` / workflow metadata).
- Field availability depends on generator/workflow.

## Project Files
- `app.py` - main application
- `build_exe.ps1` - build helper script
- `requirements.txt` - Python dependencies

## License
Choose and add a license file if you plan public reuse (for example MIT).
