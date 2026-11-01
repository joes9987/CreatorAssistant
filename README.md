# CreatorAssistant – AI Clip Detection for League Videos

Automatically detect highlights in your OBS-recorded League of Legends gameplay and extract Shorts/TikTok/Reels-ready clips.

## Install (Windows)

Download **CreatorAssistant-v1.0.1-win64.zip** from the [GitHub Releases](https://github.com/joes9987/CreatorAssistant/releases) page. Extract the **whole** zip, then run `CreatorAssistant.exe` inside the `CreatorAssistant` folder.

The folder must contain `CreatorAssistant.exe`, `CreatorAssistant-core.exe`, `_internal`, `config.yaml`, and `CreatorAssistantGameLogger.exe`. Copying only the `.exe` will not start.

Edit `config.yaml` in that folder for your accounts. API keys stay on your PC. They are not written to `creatorassistant.log`.

**Requirements:** Windows 10 or 11, 64-bit. FFmpeg on PATH, or `ffmpeg_path` in `config.yaml` (see below). Python is not required for this zip.

**SmartScreen:** The exe is unsigned. Windows may say it protected your PC. Choose **More info**, then **Run anyway**.

**Check the download** (PowerShell). `Hash` should match the hex at the start of `CreatorAssistant-v1.0.1-win64.zip.sha256` on the release (case does not matter; the file also names the zip):

```powershell
Get-FileHash .\CreatorAssistant-v1.0.1-win64.zip -Algorithm SHA256
```

## How It Works

1. **AI detection** – Analyzes audio energy and visual motion to find exciting moments (team fights, kills, etc.)
2. **Clip extraction** – Uses FFmpeg to cut clips at detected timestamps
3. **9:16 conversion** – Converts to vertical format for YouTube Shorts, TikTok, Instagram Reels

## Prerequisites

The Windows zip in **Install** does not need Python. To run from source:

- **Python 3.9+**
- **FFmpeg** – Required for video/audio processing
  - [Download for Windows](https://www.gyan.dev/ffmpeg/builds/) (get `ffmpeg-release-essentials.zip`)
  - Extract somewhere (e.g. `C:\ffmpeg-essentials`). CreatorAssistant will auto-detect if the `bin` folder is in PATH or in `C:\ffmpeg\bin` / `C:\ffmpeg-essentials\bin`
  - If not found: add the `bin` folder to your System PATH, or set `ffmpeg_path: "C:\\path\\to\\bin"` in `config.yaml`
- Your gameplay videos (`.mp4` or `.mkv`)

## Setup

```bash
cd CreatorAssistant
pip install -r requirements.txt
```

## Usage

### Desktop UI (recommended)

```bash
python run_main_gui.py
```

- Defaults to your **`input/`** folder (create it, or set **Save as default recordings folder** after browsing elsewhere). Preferences are stored in `gui_settings.json`.
- **Process selected** / **Process all in folder** runs detection + extract; upload checkboxes use the same dialog as the CLI.

**Game events logger (separate window):**

```bash
python run_logger_gui.py
```

Use **Stop and save** when you finish recording if OBS WebSocket auto-stop does not fire.

### Command line

**Browse for videos** (starts in `input/` per `config.yaml` → `paths.default_input_dir`):

```bash
python main.py
```

**Process specific videos:**
```bash
python main.py "2025-12-12 00-29-52.mp4" "2025-12-25 02-29-53.mp4"
```

Clips are saved to the `outputs/` folder as `{video_name}_clip_01.mp4`, `_clip_02.mp4`, etc.

### Standalone `.exe` builds

Windows users should use the zip in **Install** above. To build that zip locally, see **[BUILD.md](BUILD.md)**.

## Configuration

Edit `config.yaml` to tune behavior:

| Setting | Description | Default |
|---------|-------------|---------|
| `sensitivity` | How aggressive detection is (0.3 = strict, 0.7 = more clips) | 0.5 |
| `min_score` | Minimum score to accept (0–1). Higher = stricter | 0.6 |
| `min_prominence` | Peak must stand out from neighbors (reduces noise) | 0.15 |
| `max_clips_per_video` | Max clips per source video | 5 |
| `duration_seconds` | Target clip length | 30 |
| `crf` | Video quality (18 = high, 23 = smaller file) | 18 |
| `audio_weight` / `motion_weight` | Balance audio vs motion. For League, try 0.6/0.4 | 0.5 each |

- **More clips:** Increase `sensitivity` or `max_clips_per_video`, lower `min_score`
- **Fewer, better clips:** Decrease `sensitivity`, raise `min_score` to 0.7+
- **Better video quality:** Keep `crf` at 18 or lower; use `preset: "slow"`

## Output

- **Format:** MP4, 1080×1920 (9:16 vertical)
- **Location:** `outputs/` folder
- **Ready for:** YouTube Shorts, TikTok, Instagram Reels

## Accurate kill-based clips (recommended)

For precise kill moments, use the **game events logger** (Riot's Live Client Data API — same data source as Overwolf):

1. **Before playing:** Start the logger:
   ```bash
   python game_events_logger.py
   ```
   Or use **`python run_logger_gui.py`** for a window with **Stop and save**.
2. **While playing:** League must be in an active match. Start OBS when the game loads in.
3. **Multiple games:** Leave the logger running across your full session. It detects new games and logs all kills.
4. **Auto-stop:** With OBS WebSocket (Tools → WebSocket Server Settings), the logger may exit when you stop OBS recording. Install `obsws-python` and ensure the server is enabled in OBS. If it does not stop, use **Stop and save** in the logger app or Ctrl+C in the terminal.
5. **Multiple recordings:** Each session saves to `eventlogs/events_YYYY-MM-DD_HH-MM-SS.json`. When you run `main.py` on several videos, it automatically picks the matching eventlog per video.
6. **When processing:** Run `python main.py` — it will use kill timestamps instead of AI detection.

No Overwolf needed. The logger uses Riot's local API (port 2999), which Overwolf also uses.

## Workflow

```
[Optional: python game_events_logger.py while playing]
OBS recording → (optional: edit in Filmora) → CreatorAssistant → Shorts upload
```

- **With eventlogs:** Clips are cut exactly at kill timestamps (main.py matches each video to its session file).
- **Without eventlogs:** AI detection finds highlights from audio + motion.

## Upload to YouTube Shorts (optional)

1. **Google Cloud setup:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a project (or select one)
   - Enable **YouTube Data API v3** (APIs & Services → Library → YouTube Data API v3)
   - Create OAuth credentials: APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: **Desktop app**
   - Download the JSON and save it as `client_secrets.json` in the CreatorAssistant folder

2. **Enable upload in config:**
   ```yaml
   youtube:
     enabled: true
     privacy: "private"   # or "public" / "unlisted"
     creator_name: "joes9987"
     title_template: "{creator} | League clip {num} with {champion}"
   ```

3. **First run:** A browser window opens to log in to your Google account and authorize the app. A `youtube_token.json` file is created so you won't need to log in again.

4. **Note:** New/unverified API projects may only allow private uploads until your app is audited by Google.

## Upload to TikTok (optional)

1. **TikTok Developer setup:**
   - Go to [TikTok for Developers](https://developers.tiktok.com/)
   - Create an app and get `client_key` and `client_secret`
   - Add redirect URI: `http://localhost:8080/callback`
   - Request the `video.publish` scope (may require app review)

2. **Enable in config.yaml:**
   ```yaml
   tiktok:
     enabled: true
     client_key: "your_client_key"
     client_secret: "your_client_secret"
   ```

3. **First run:** A browser URL will be shown; visit it to authorize. A local server catches the callback and saves the token.

4. **Note:** Unverified TikTok apps may only post to private accounts until approved.

## Troubleshooting

**`Failed to load Python DLL` / `_internal\python313.dll`, or “Missing _internal folder”.** The download was only the exe. Extract `CreatorAssistant-v1.0.1-win64.zip` completely and run `CreatorAssistant.exe` from that folder. Do not copy `CreatorAssistant.exe` out by itself. `CreatorAssistant.exe` is a small launcher: if `_internal` is missing it shows that message instead of the Python DLL error. `CreatorAssistant-core.exe` is the real app and still needs `_internal` beside it.

**`Missing config.yaml`.** Keep `config.yaml` next to `CreatorAssistant.exe`. The zip includes one copied from `config.example.yaml`.

**FFmpeg not found.** Install FFmpeg and add its `bin` folder to PATH, or set `ffmpeg_path` in `config.yaml`.

**Send a bug report.** Attach `creatorassistant.log` from the app folder. It records startup and errors with API keys and home-folder names removed. Include your Windows version, the app version (`CreatorAssistant.exe --version`), and the exact error text. A [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) asks for those.
