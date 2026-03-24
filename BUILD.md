# Building desktop executables (PyInstaller)

CreatorAssistant can be packaged as two separate apps:

1. **Clip workflow** — `run_main_gui.py` → browse `input/` (or your folder), detect/extract, optional uploads.
2. **Game events logger** — `run_logger_gui.py` → manual **Stop and save** when OBS does not auto-stop.

## One-time setup

```powershell
cd path\to\CreatorAssistant
pip install -r requirements.txt
pip install pyinstaller
```

Copy `config.example.yaml` to `config.yaml` and configure before distributing (secrets stay beside the exe).

## Clip workflow exe

From the project root (folder that contains `main.py`, `detect.py`, `gui\`, etc.):

```powershell
pyinstaller --noconfirm --windowed --name CreatorAssistant `
  --add-data "config.example.yaml;." `
  run_main_gui.py
```

- If you use a single-file build, add `--onefile` (slower startup; one `.exe`).
- Heavy deps (OpenCV, numpy, librosa) may need extra flags on some machines, e.g.:

```powershell
pyinstaller --noconfirm --windowed --name CreatorAssistant `
  --collect-all cv2 --collect-all librosa --collect-all soundfile `
  run_main_gui.py
```

After a successful build, copy **`config.yaml`** (your real config) next to `dist\CreatorAssistant\CreatorAssistant.exe` (or next to the one-file exe). The GUI also writes **`gui_settings.json`** next to the exe for the saved default recordings folder.

## Game logger exe

```powershell
pyinstaller --noconfirm --windowed --name CreatorAssistantGameLogger `
  run_logger_gui.py
```

Optional: `--onefile`, and include `obsws-python` if you want OBS WebSocket auto-stop bundled:

```powershell
pip install obsws-python
pyinstaller --noconfirm --windowed --name CreatorAssistantGameLogger `
  --hidden-import=obsws_python `
  run_logger_gui.py
```

## CLI `main.py` (console)

```powershell
pyinstaller --noconfirm --console --name CreatorAssistantCLI `
  --collect-all cv2 --collect-all librosa --collect-all soundfile `
  main.py
```

## Notes

- **FFmpeg** is not bundled; users still need FFmpeg on PATH or `ffmpeg_path` in `config.yaml`.
- Test each exe on a clean PC or VM before sharing.
- First run may be blocked by SmartScreen (unsigned exe).
