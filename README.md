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

See **[BUILD.md](BUILD.md)** for PyInstaller commands (`CreatorAssistant` + `CreatorAssistantGameLogger`).

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

## Upload to Instagram Reels (optional)

Instagram's publishing API downloads the video from a URL. It cannot read a file on your PC. CreatorAssistant uploads the clip to a **Vercel Blob** store on the same project as the callback site (`creator-assistant.vercel.app`), gives that URL to Instagram, then **deletes the object** after the Reel is published. The object is also deleted if container creation, processing, or publishing fails.

The desktop app does **not** send clips to anonymous hosts (tmpfiles.org, litterbox.catbox.moe, transfer.sh, 0x0.st). If hosting is not configured, the Instagram upload stops with an error.

YouTube and TikTok are unchanged: those clips go straight to the platform and do not use this store.

### One-time setup (project owner)

1. **Meta app** (Instagram Login), from the comments in `config.example.yaml`:
   - OAuth redirect URI: `https://creator-assistant.vercel.app/callback.html`
   - Native/desktop app on, with that same HTTPS callback URL
   - Use the Instagram App ID and secret from **Instagram → API setup with Instagram Login** (not the Facebook App ID)
   - Enable `instagram_business_content_publish` and add your professional account

2. **Blob store** on the Vercel project that serves the callback site. From that linked project:

   ```bash
   vercel blob create-store creator-assistant-ig --access private --yes
   ```

   Or in the dashboard: Storage → Blob → create a **private** store and connect it to Production (and Preview, if you use it). Connecting the store gives the API OIDC access (`BLOB_STORE_ID`). Leave `BLOB_READ_WRITE_TOKEN` on Vercel if the dashboard created one. **Do not copy either credential into `config.yaml` or git.**

3. **Limited upload secret** (this is what the desktop app stores):

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   In the Vercel project, set `IG_STORAGE_TOKEN` to that value for Production. Redeploy so `api/ig-media` is live. The route only accepts `video/mp4`, only under `ig-reels/<random-id>.mp4`, and only up to 512 MiB (override with `IG_BLOB_MAX_BYTES` if you need to).

4. **`config.yaml`** (copy from `config.example.yaml`):

   ```yaml
   instagram:
     enabled: true
     instagram_app_id: "..."
     instagram_app_secret: "..."
     redirect_uri: "https://creator-assistant.vercel.app/callback.html"
     ig_user_id: "..."
     storage:
       provider: vercel_blob
       api_url: "https://creator-assistant.vercel.app/api/ig-media"
       token: "<same value as IG_STORAGE_TOKEN>"
   ```

   `api_url` is the host. Point it at another deployment of this repo if you are not using the production site. `provider` must be `vercel_blob`.

### What end users need

People who receive the app do **not** create a Vercel account or a Blob store. They need `instagram.storage.api_url` and `instagram.storage.token` in the `config.yaml` beside the app (see [BUILD.md](BUILD.md)). That token can upload and delete `ig-reels/*` clips through the API. It cannot list the store or read the Blob credential. Rotate `IG_STORAGE_TOKEN` on Vercel and in every distributed config to revoke it.

The first Instagram upload still opens a browser so you can sign in. The token is saved to `instagram_token.json`.

### If Instagram cannot fetch the clip

The default store is **private**. Instagram receives a signed URL that lasts 30 minutes (the file is deleted sooner, as soon as publishing finishes or fails). The upload URL itself expires after 15 minutes.

If the Reel fails with a media-download error, Meta's fetcher may be refusing the signed URL. Create a **public** Blob store instead, set `IG_BLOB_ACCESS=public` on the Vercel project, and redeploy. The app then gives Instagram a plain `https://….public.blob.vercel-storage.com/ig-reels/<id>.mp4` URL. The id is unguessable, the CDN cache is 60 seconds, and the app still deletes the object when it is done. `IG_BLOB_ACCESS` must match the store (`private` or `public`).

### Leftover files

If the app is killed after the upload and before the delete, the object stays in the store. A private URL stops working after 30 minutes; the bytes remain until you delete `ig-reels/…` in the Vercel Blob dashboard. A public object stays readable at that URL until you delete it.
