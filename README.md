# CreatorAssistant – AI Clip Detection for League Videos

Automatically detect highlights in your OBS-recorded League of Legends gameplay and extract Shorts/TikTok/Reels-ready clips.

## How It Works

1. **AI detection** – Analyzes audio energy and visual motion to find exciting moments (team fights, kills, etc.)
2. **Clip extraction** – Uses FFmpeg to cut clips at detected timestamps
3. **9:16 conversion** – Converts to vertical format for YouTube Shorts, TikTok, Instagram Reels

## Prerequisites

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

**Process all videos in this folder:**
```bash
python main.py
```

**Process specific videos:**
```bash
python main.py "2025-12-12 00-29-52.mp4" "2025-12-25 02-29-53.mp4"
```

Clips are saved to the `outputs/` folder as `{video_name}_clip_01.mp4`, `_clip_02.mp4`, etc.

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
2. **While playing:** League must be in an active match. Start OBS when the game loads in.
3. **Multiple games:** Leave the logger running across your full session. It detects new games and logs all kills. Stop with Ctrl+C when done.
4. **When processing:** Run `python main.py` — it will use kill timestamps instead of AI detection.

No Overwolf needed. The logger uses Riot's local API (port 2999), which Overwolf also uses.

## Workflow

```
[Optional: python game_events_logger.py while playing]
OBS recording → (optional: edit in Filmora) → CreatorAssistant → Shorts upload
```

- **With events file:** Clips are cut exactly at kill timestamps.
- **Without events file:** AI detection finds highlights from audio + motion.

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
     title_template: "League Clip {n}"
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
