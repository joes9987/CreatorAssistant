# Building desktop executables (PyInstaller)

Release builds use **`packaging/build.ps1`**. It creates a clean virtualenv, builds the one-folder app (`CreatorAssistant-core.exe` + `_internal`), the one-file game logger, and the small `CreatorAssistant.exe` launcher, then writes:

`dist\CreatorAssistant-v1.0.1-win64.zip`

The zip contains the full `CreatorAssistant` folder (`CreatorAssistant.exe`, `CreatorAssistant-core.exe`, `_internal`, `CreatorAssistantGameLogger.exe`, `config.yaml` copied from `config.example.yaml`, and `README.md`). It does **not** copy your local `config.yaml`.

```powershell
./packaging/build.ps1
```

`-StageOnly` stops before the zip. `-ZipOnly` zips an existing stage folder and omits `creatorassistant.log`. Visual Studio C++ build tools are required for the launcher. Pushing a `v*` tag that matches `version.py` runs the same build on GitHub Actions and attaches the zip and a `.sha256` file to the release.

The main app is a **one-folder** PyInstaller build. `_internal` must stay next to `CreatorAssistant-core.exe`. The game logger is **one-file** so it does not need its own `_internal`.

## Apps

CreatorAssistant can be packaged as two separate apps:

1. **Clip workflow** — `run_main_gui.py` → browse `input/` (or your folder), detect/extract, optional uploads.
2. **Game events logger** — `run_logger_gui.py` → manual **Stop and save** when OBS does not auto-stop.

## Notes

- **FFmpeg** is not bundled. Users need it on PATH or `ffmpeg_path` in `config.yaml`.
- Do not upload `CreatorAssistant.exe` by itself. The release asset is the zip from `packaging/build.ps1`, which includes `_internal`.
- First run may be blocked by SmartScreen (unsigned exe). See the Install section in `README.md`.
