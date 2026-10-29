# LiteGallery

LiteGallery is a Windows-first local photo manager focused on reducing storage
use while keeping a fast catalog for RAW, JPG, TIFF, and PSD files.

## Features

- Scan the current folder only and report image count and total size; double-click child folders to navigate.
- Browse pictures in an Explorer-style thumbnail grid with capture date and time.
- Double-click a photo to open the viewer; use the left/right arrow keys to move through the current folder.
- Rate photos from 1 to 5, with per-rating JPEG long-edge limits.
- Keep retained, pending-delete, and pending-processing areas; Delete stages,
  Backspace restores, and purging waits for Lightroom removal acknowledgement.
- Apply a luminance-histogram exposure gate to rating-4 exports; use Lightroom
  Classic to render an edited TIFF for preview and final export.
- Press E on any selected photo to stage it for processing; the rendered TIFF is
  previewed in the arrow-key viewer before the user starts final export.
- Store the catalog, ratings, states, and export limits in SQLite without modifying
  source files.
- Export CR2, CR3, NEF, DNG, TIFF, PSD, and oversized JPG sources to rating-sized
  JPEGs; verify output before recycling source RAW/JPG files for ratings 1–4.
- Preserve RAW files for rating 5 and export at native dimensions by default.
- Preview PSD composites and manage PSD files alongside other photos.
- Open the selected original directly in a configured Photoshop executable.
- Explicitly send selected photos to the active Lightroom Classic Catalog and
  synchronize app-initiated removals through the companion plug-in.
- Request a temporary 16-bit TIFF rendered from Lightroom edits, preview it,
  and use it as the final rating-sized JPG export source.

## Requirements

- Windows 10 or 11
- Python 3.14 (project runtime: `S:\Python314\python.exe`)
- Lightroom Classic for Catalog synchronization
- Photoshop for editing handoff
- ExifTool is optional and can be selected in Settings to copy metadata to JPG

## Run from source

```powershell
cd W:\Development\LiteGallery
$python = "S:\Python314\python.exe"
$env:TEMP = "S:\Python314\Temp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "S:\Python314\Cache\pip"
$env:PYINSTALLER_CONFIG_DIR = "S:\Python314\Cache\pyinstaller"
& $python -m pip install -e ".[dev,build]"
& $python -m photo_manager
```

The installer is stored at `S:\Python314\python-3.14.8-amd64.exe`. Runtime,
test, and build packages are installed in the main interpreter's global
`S:\Python314\Lib\site-packages`. LiteGallery does not create or use a virtual
environment. Temporary files, pip cache, and PyInstaller cache are redirected
to S: as well.

Run core tests with:

```powershell
& $python -m pytest -q
```

## Lightroom Classic setup

1. Start LiteGallery and click **Prepare Lightroom Plugin**.
2. In Lightroom Classic, open **File → Plug-in Manager → Add**.
3. Select `%LOCALAPPDATA%\LiteGallery\LightroomPlugin\PhotoManager.lrplugin`.
4. Keep Lightroom Classic running while sending or removing cataloged photos.

Only photos explicitly sent from LiteGallery are added. Lightroom-side changes
are not imported back. Requests wait in a durable local queue while Lightroom
is closed. For pending-processing photos, LiteGallery asks the plug-in to render
the active Catalog version to a temporary 16-bit TIFF, previews that render,
and uses it as the final JPEG export source. The TIFF is removed after export
or canceling the processing stage. Synced source files are moved to the Recycle
Bin only after Lightroom confirms Catalog removal.

The plug-in uses Lightroom Classic SDK catalog and export calls. Verify them
against the installed Lightroom Classic version before relying on destructive
operations. If rendering or removal fails, LiteGallery retains the source and
reports the failed operation; it does not automate Lightroom's UI.

## RAW conversion and metadata

Default maximum long edges are 1280, 1920, 2560, and 3840 pixels for ratings
1–4; rating 5 keeps native pixel dimensions and its RAW. These limits and the
global JPEG quality can be changed in Settings. Rating-4 exposure checks flag
an endpoint histogram bin when the 0 or 255 bin is at least twice its adjacent
1 or 254 bin. ExifTool is optional; without it, source metadata is not copied.

LibRaw camera support varies by camera model and version. Validate CR2, CR3,
NEF, and DNG using actual files from each target camera before batch use.
Manufacturer naming is typically Canon CR2/CR3, Nikon NEF, and DJI DNG.

## Validation status

Core unit tests cover current-folder scanning, capture dates, SQLite ratings
and states, histogram checks, JPEG sizing, and the Lightroom request queue.
Lightroom SDK integration, actual camera RAW compatibility, Photoshop launch
behavior, PSD samples, and packaged Windows deployment require validation with
the corresponding Adobe applications and representative files.

## Build a Windows folder distribution

```powershell
$python = "S:\Python314\python.exe"
$env:TEMP = "S:\Python314\Temp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "S:\Python314\Cache\pip"
$env:PYINSTALLER_CONFIG_DIR = "S:\Python314\Cache\pyinstaller"
& $python -m pip install -e ".[dev,build]"
& $python -m PyInstaller --noconfirm --clean --windowed --name LiteGallery `
  --add-data "lightroom_plugin;lightroom_plugin" `
  --collect-all rawpy --collect-all psd_tools run_litegallery.py
```

The command collects the Lightroom plug-in and RAW libraries. ExifTool remains
an optional external executable and must be configured in Settings. Validate
the generated `dist\LiteGallery` folder on a clean Windows machine.
