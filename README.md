# Auto Clipper

Auto Clipper is a local app that turns one long video into short vertical clips. You give it a file or a URL and it:

1. downloads or copies the video (yt-dlp for URLs),
2. transcribes it with word timestamps (faster-whisper, runs on CPU or CUDA),
3. asks Claude for the most viral moments (the model comes from settings, default `claude-opus-5-5`),
4. cuts each moment into a 9:16 1080x1920 clip,
5. writes a **narration** script for each clip in English and Hindi. The narration is new commentary spoken over the clip, not a dub of what is said in it. It also writes a title, a description and hashtags,
6. generates the voice with a local [Voicebox](https://github.com/jamiepine/voicebox) server. Hindi always uses the `chatterbox` engine,
7. burns word-by-word captions in the clip's language (Noto Sans for English, Noto Sans Devanagari for Hindi) and mixes in the original audio: it is ducked to `audio.original_volume` while the narration plays and comes back to `audio.original_volume_after` before and after it (with `audio.duck_ramp_seconds` ramps), then the mix is loudness-normalised,
8. writes `final.mp4` and `metadata.json` for each clip in each language.

The narration is sized to the clip from the voice's words per second (`script.wps_en` / `script.wps_hi`, `script.fill_ratio`). If a voice-over is longer than its clip, it is sped up slightly, never more than `audio.max_speedup`. If it would need more than that, it is first shortened by the LLM, and trimmed with a fade only as a last resort. If a new voice-over is much shorter than its clip (below `script.lengthen_below`), the LLM lengthens the narration once; the longer take is kept only if it fits, otherwise the original take stays. Lengthening happens only when the script was just written or regenerated, or no voice exists yet: resuming a job (`run --job`) never rewrites a narration you edited, and `rerender` never lengthens.

Step 1 (the command-line pipeline) and step 2 (a local web UI, see [Usage: Web UI](#usage-web-ui)) exist today. See the [Roadmap](#roadmap) for what comes next.

---

## Setup

### 1. Python (via uv)

Auto Clipper needs Python 3.10 to 3.13 and uses [uv](https://docs.astral.sh/uv/).

**Linux**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd auto-clipper
uv sync
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
cd auto-clipper
uv sync
```

`uv sync` creates `.venv` and installs the `autoclipper` command. Run every command through uv: `uv run autoclipper ...`.

### 2. FFmpeg (with libass)

Captions are burned in with FFmpeg's `ass` filter with `shaping=complex` (HarfBuzz shaping, needed for correct Devanagari conjuncts and vowel signs), so the FFmpeg build must include libass.

- **Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`
- **Windows:** `winget install Gyan.FFmpeg` installs the gyan.dev "full" build. You can also download it from https://www.gyan.dev/ffmpeg/builds/ and add its `bin` folder to `PATH`.

Check that it works with `uv run autoclipper doctor`. The `ffmpeg filters` line must list `subtitles, ass, amix, atempo`.

### 3. Fonts: Noto Sans and Noto Sans Devanagari

You can install them in either of two ways:

- **Project-local (works the same on every OS):** download *Noto Sans* and *Noto Sans Devanagari* from Google Fonts and copy the `.ttf` files (for example `NotoSans-Regular.ttf`, `NotoSans-Bold.ttf`, `NotoSansDevanagari-Regular.ttf`, `NotoSansDevanagari-Bold.ttf`) into `assets/fonts/`. When that folder holds fonts, FFmpeg is pointed at it with `fontsdir`.
- **System-wide:**
  - Linux: `sudo apt install fonts-noto-core` (or `fonts-noto`). Then check with `fc-list | grep -i "Noto Sans Devanagari"`.
  - Windows: right-click the `.ttf` files and choose **Install for all users** (or plain **Install** for your user only). `autoclipper doctor` finds them by file name in `%WINDIR%\Fonts` and `%LOCALAPPDATA%\Microsoft\Windows\Fonts` (Windows has no `fc-list`).

### 4. Voicebox (local text to speech)

1. Install Voicebox from its releases page (desktop app) or run its server, following the Voicebox README.
   **Linux:** the releases have no Linux AppImage, so run the backend from source (`python -m backend.main` in its own venv). The exact steps used on the dev machine are in the "Ops (Voicebox)" notes in `docs/TEAM_LOG.md`.
2. Start it. Auto Clipper expects it at `http://127.0.0.1:17493`. To use another address, change it with `autoclipper settings set voicebox.url=http://host:port`.
3. In Voicebox, create one voice profile for English and one for Hindi. Hindi uses the multilingual **Chatterbox** engine.
4. Find the profile ids and save them in the settings:
   ```bash
   uv run autoclipper profiles
   uv run autoclipper settings set voicebox.english.profile_id=<id>
   uv run autoclipper settings set voicebox.hindi.profile_id=<id>
   # optional: English engine (Hindi is always chatterbox)
   uv run autoclipper settings set voicebox.english.engine=chatterbox
   ```

Engine notes:
- Valid engine ids: `qwen`, `qwen_custom_voice`, `luxtts`, `chatterbox`, `chatterbox_turbo`, `tada`, `kokoro`.
- `chatterbox` is **Chatterbox Multilingual** (23 languages, including `hi`). That is why it is the default for both languages.
- `chatterbox_turbo` is English only. Never use it for Hindi.
- A cloned profile needs a reference sample uploaded (in the Voicebox app, or `POST /profiles/{id}/samples`) before `chatterbox` can use it.
- If every generation fails with `'NoneType' object is not callable`, the chatterbox venv is missing `setuptools<81` (resemble-perth imports `pkg_resources`). Install `setuptools<81` into the Voicebox venv.
- On CPU, a 60 s narration takes a few minutes per language. If generations time out, raise the limit, for example `uv run autoclipper settings set voicebox.timeout_seconds=1200`.

Auto Clipper calls these endpoints: `POST /generate {text, profile_id, language, engine}`, then it polls `GET /history/{id}` until the status is `completed`, then it fetches `GET /audio/{id}`. It reads the profile list from `GET /profiles`.

### Windows: faster-whisper and NVIDIA GPUs

With an NVIDIA GPU, faster-whisper (CTranslate2) needs the **CUDA 12 cuBLAS** and **cuDNN 9** runtime libraries, which pip does not install. Without them, transcription fails with errors such as `cublas64_12.dll is not found`. Either install those runtimes, or run on the CPU:

```bash
uv run autoclipper settings set whisper.device=cpu
```

With the default `whisper.device=auto`, Auto Clipper falls back to the CPU automatically when loading the CUDA libraries fails.

### 5. Anthropic credentials

Use either of these.

**Option A: `ant` CLI login (no key stored in the project).** Install the [Anthropic CLI](https://github.com/anthropics/anthropic-cli/releases) and run:

```bash
ant auth login
```

The Anthropic SDK picks up the active `ant` profile automatically.

**Option B: API key in `.env`.**

```bash
cp .env.example .env          # Windows: copy .env.example .env
# edit .env:  ANTHROPIC_API_KEY=sk-ant-...
```

The key is read from the environment variable named by `llm.api_key_env` (default `ANTHROPIC_API_KEY`). A `.env` file in the project root is loaded automatically. If the key is set, it wins over the `ant` login.

### 6. Node 22+ (only for step 3, later)

The current CLI does not need Node. The HyperFrames animated captions planned for step 3 will need **Node.js 22 or newer**. `doctor` warns if your Node is older; on this dev machine it is v16.

### 7. Check everything

```bash
uv run autoclipper doctor
```

`doctor` checks:
- ffmpeg and ffprobe are on PATH,
- ffmpeg has the subtitles, ass, amix and atempo filters,
- the fonts are installed, either in assets/fonts or found by `fc-list`,
- Voicebox is reachable and the profile ids are set,
- the API key is present,
- the Node version.

It exits with code 1 if anything required is missing.

---

## CLI usage

```bash
uv run autoclipper run <file-or-url> [--clips N] [--min S] [--max S] [--langs en,hi] [--job ID]
uv run autoclipper rerender <job_id> <clip> <lang>
uv run autoclipper status <job_id>
uv run autoclipper jobs
uv run autoclipper profiles
uv run autoclipper settings show
uv run autoclipper settings set key.path=value [key.path=value ...]
uv run autoclipper doctor
```

Add `--debug` to any command to see full tracebacks. Exit codes: 0 ok, 1 error, 2 usage error.

Examples:

```bash
# 3 clips of 20-45 s, English and Hindi
uv run autoclipper run "~/Videos/talk.mp4" --clips 3 --min 20 --max 45 --langs en,hi

# a URL, English only
uv run autoclipper run "https://www.youtube.com/watch?v=..." --langs en

# resume an interrupted job (finished stages are skipped)
uv run autoclipper run --job 20261122-101500-ab12cd

# edit a script, then re-render that one clip
#   data/jobs/<job>/clips/00/hi/script.json  -> change "narration" or "title"
uv run autoclipper rerender 20261122-101500-ab12cd 0 hi
```

Progress lines look like `[<job_id>] <stage> <clip>/<lang> <pct>% <message>`, for example:

```
[20261122-101500-ab12cd] transcribe 40% 16.0/40.0s
[20261122-101500-ab12cd] voice 01/hi 100% 9.8s, tempo x1.08
```

**Re-runs are idempotent.** A stage is skipped when its output file exists and is not empty. When a stage is regenerated, every stage after it in that clip and language runs again.

**Failures are isolated.** If one clip fails in one language (for example a Voicebox error), the error is recorded in `job.json` and every other clip keeps going. The job fails only if a job-level stage fails (ingest, transcribe, moments, or every cut), or if every clip-language fails.

**Rerender** uses the saved `script.json`, which you may have edited. The voice is regenerated only when the narration text changed: the sha256 of the narration is compared with `narration_hash` in `voice.json`. Captions, `final.mp4` and `metadata.json` are always rebuilt. If the voice stage had to shorten the narration to fit the clip, the shortened text is written back to `script.json`, so the captions always match the audio.

Clip indexes are 0-based and match the `clips/NN` folders.

---

## Usage: Web UI

```bash
uv run autoclipper serve                 # http://127.0.0.1:8765/ , opens a browser tab
uv run autoclipper serve --no-browser --port 8765
```

The page lets you start a job from an uploaded file or a URL, follow its progress, review each clip in each language (the video plays in the page), edit the narration, title, description and hashtags, re-render one clip, change the settings and run the doctor checks. Jobs and re-renders run one at a time in a single background worker, in order. The CLI and the web UI share `data/`; a `job.lock` file stops both from running the same job at once.

The server binds to loopback only by default. The page gets an access token (stored in `data/web/token`) when it is loaded from the same machine; API calls without it get 401, and with a wrong one 403. Stop the server with Ctrl-C, or `kill -INT <pid>` when it runs in the background.

**Currently running (started by the team lead on 2026-11-22):** `autoclipper serve --host 127.0.0.1 --port 8765 --no-browser`, PID `1796838` (also in `/tmp/claude-1000/ac_serve.pid`), log in `/tmp/claude-1000/ac_serve.log`. Stop it with:

```bash
kill -INT 1796838      # or: pkill -INT -f "autoclipper serve --host 127.0.0.1 --port 8765"
```

---

## Data layout

Everything is stored under `data/`, or under `$AUTOCLIPPER_DATA_DIR` if that is set:

```
data/
  settings.json                     all settings (see below)
  jobs/<job_id>/
    job.json                        status + per-stage progress (job and per clip per language)
    source.mp4                      copied or downloaded source
    audio.wav                       16 kHz mono for ASR
    transcript.json                 segments + word timestamps
    moments.json                    chosen moments (start, end, hook, reason, score)
    clips/00/
      cut.mp4                       1080x1920 cut with original audio
      en/
        script.json                 narration, title, description, hashtags  <- editable
        voice_raw.wav               Voicebox output
        voice.wav                   fitted voice (<= clip length)
        voice.json                  durations, tempo, narration_hash
        words.json                  timed caption words
        captions.ass                word-by-word ASS subtitles
        final.mp4                   the short
        metadata.json               {"language","title","description","hashtags"}
      hi/ ...
    clips/01/ ...
```

---

## Settings reference

Change settings with `autoclipper settings set key.path=value`. The value is parsed as JSON when it can be, so `3`, `true`, `null` and `["en","hi"]` all work; anything else is stored as a string. Every change is validated before it is saved, and unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `languages` | `["en","hi"]` | Output languages (`en`, `hi`). `languages=en` also works. |
| `llm.model` | `claude-opus-5-5` | Claude model used for moments and scripts |
| `llm.effort` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `llm.use_fallbacks` | `true` | Server-side fallback if a request is declined |
| `llm.api_key_env` | `ANTHROPIC_API_KEY` | Name of the environment variable that holds the key |
| `llm.max_tokens` | `16000` | Output token budget per Claude call (1024-64000). Long sources (2 h+) at high effort may need more, e.g. `llm.max_tokens=32000` |
| `whisper.model` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `whisper.device` | `auto` | `auto` / `cpu` / `cuda` (auto uses CUDA if a GPU is found, and falls back to CPU if the CUDA libraries fail to load) |
| `whisper.compute_type` | `auto` | auto = `int8` on CPU, `float16` on CUDA |
| `whisper.language` | `null` | Source language; `null` = auto-detect |
| `voicebox.url` | `http://127.0.0.1:17493` | Voicebox server |
| `voicebox.poll_interval` | `1.0` | Seconds between history polls (0.1-10) |
| `voicebox.timeout_seconds` | `600` | Max wait per generation, in seconds (10-7200) |
| `voicebox.english.profile_id` | `""` | English voice profile id (see `profiles`) |
| `voicebox.english.engine` | `chatterbox` | English TTS engine |
| `voicebox.hindi.profile_id` | `""` | Hindi voice profile id |
| `voicebox.hindi.engine` | `chatterbox` | Always forced to `chatterbox` for Hindi |
| `clips.count` | `5` | Clips per video (1-30) |
| `clips.min_seconds` / `clips.max_seconds` | `20` / `60` | Clip length bounds |
| `clips.width` / `clips.height` | `1080` / `1920` | Output size (240-3840, even numbers) |
| `clips.reframe` | `crop` | `crop` = center crop; `blur` = whole frame over a blurred fill |
| `audio.original_volume` | `0.15` | Original audio level WHILE the voice speaks (ducked, 0-1) |
| `audio.original_volume_after` | `1.0` | Original audio level before and after the narration (0-2) |
| `audio.duck_ramp_seconds` | `0.4` | Fade time between the two levels (0-2 s) |
| `audio.voice_volume` | `1.0` | Voice level (0-2) |
| `audio.max_speedup` | `1.25` | Max atempo used to fit the voice into the clip |
| `audio.loudnorm` | `true` | Normalize the loudness of the final mix (a limiter always keeps peaks below 0 dBFS) |
| `audio.target_lufs` | `-14` | Loudness target for `audio.loudnorm` (-30 to -8 LUFS) |
| `script.wps_en` / `script.wps_hi` | `3.0` / `3.35` | Words per second of the voice, used to size the narration (measured on chatterbox; 1-6) |
| `script.fill_ratio` | `0.88` | Share of the clip the narration should fill (0.5-1) |
| `script.lengthen_below` | `0.75` | A new voice shorter than this share of the clip gets one "lengthen" retry (0.3-0.95; never on rerender) |
| `captions.font_en` / `captions.font_hi` | `Noto Sans` / `Noto Sans Devanagari` | Caption fonts |
| `captions.font_size` | `78` | Caption font size (px at 1080x1920, 20-200) |
| `captions.words_per_line` | `3` | Words shown per caption line (1-8) |
| `captions.primary_color` / `captions.highlight_color` | white / yellow | ASS `&HAABBGGRR` colors (`&H` + 8 hex digits) |
| `captions.position_pct` | `72` | Vertical centre of the caption line, in % of the height from the top (5-95). `null` = use `captions.margin_v` |
| `captions.margin_v` | `520` | Distance of the captions from the bottom, in px, 0 to `clips.height` (only when `captions.position_pct` is `null`) |
| `web.max_upload_mb` | `4096` | Largest upload the web UI accepts, in MB (1-102400) |
| `web.allowed_extensions` | `[".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ogv"]` | Video file types the web UI accepts (lower-case, with the dot) |

`--clips`, `--min`, `--max` and `--langs` on `run` override these settings for that run only.

---

## Development

```bash
uv run pytest -q        # fast suite; tests marked slow are skipped by default
uv run pytest -m slow   # only the slow tests (real models or network)
```

Tests never call the real Anthropic API, a real Voicebox server, or download whisper models or videos. They use the fakes in `tests/fakes.py` and a synthetic video made by ffmpeg. Each test gets its own data dir through `AUTOCLIPPER_DATA_DIR`, set in `tests/conftest.py`. The same conftest also guards the whole suite. It removes `ANTHROPIC_*` variables and points `HOME` at a temp dir. It makes `pipeline._make_llm/_make_voicebox/_make_transcriber` raise unless a test has `@pytest.mark.real_clients`, and it blocks socket connections to anything but loopback. Network or model tests must be marked `@pytest.mark.slow`.

The pipeline gets its LLM, Voicebox client and transcriber injected (`autoclipper.pipeline.Deps`). When none are passed, the real clients are built lazily, and only when a stage actually needs them.

---

## Roadmap

- **Step 1 (done): CLI pipeline end to end.**
- **Step 2 (done): web UI.** A FastAPI app with one background worker thread (no Redis) that runs `pipeline.run_job`. The UI reads progress from `job.json` and lets you edit `script.json` and settings in the browser, then re-render.
- **Step 3: HyperFrames animated captions.** These will reuse `words.json` and need Node 22+.
- **Step 4: auto-publish.** English clips go to one channel and Hindi clips to another, on YouTube, Facebook and Instagram Reels, using `metadata.json`.
