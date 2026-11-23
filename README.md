# Auto Clipper

Auto Clipper is a local app that turns one long video into short vertical clips. You give it a file or a URL and it:

1. downloads or copies the video (yt-dlp for URLs),
2. transcribes it with word timestamps (faster-whisper, runs on CPU or CUDA),
3. asks Claude for the most viral moments (the model comes from settings, default `claude-opus-5-5`),
4. cuts each moment into a 9:16 1080x1920 clip,
5. writes a **narration** script for each clip in English and Hindi. The narration is new commentary spoken over the clip, not a dub of what is said in it. It also writes a title, a description and hashtags,
6. generates the voice with a local [Voicebox](https://github.com/jamiepine/voicebox) server. Hindi always uses the `chatterbox` engine,
7. burns word-by-word captions in the clip's language (Noto Sans for English, Noto Sans Devanagari for Hindi) and mixes the narration over the original audio (or, optionally, over the original with its speech removed by Demucs): the voice and the background are measured and the background sits a fixed number of dB under the voice while it speaks, however loud the source is, then the mix is loudness-normalised (see [Audio mix](#audio-mix-voice-over)),
8. writes `final.mp4` and `metadata.json` for each clip in each language.

You can also switch the narration off and keep only captions on the clip's own audio: see [Captions only (no voice-over)](#captions-only-no-voice-over).

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

### 6. HyperFrames animated captions (optional)

By default captions are burned in with libass (`captions.engine=ass`), which needs nothing beyond FFmpeg. The optional **HyperFrames** engine (`captions.engine=hyperframes`) draws animated word-by-word captions (the spoken word pops, or karaoke-style highlighting) as an HTML page, renders it with the [HyperFrames](https://github.com/heygen-com/hyperframes) CLI to a transparent overlay video, and composites that over the clip. The audio mix (ducking, ramp-back, loudnorm, limiter) is exactly the same for both engines. It needs **Node.js 22 or newer**, the `hyperframes` npm package and a headless Chrome, all installed per user (nothing global; your system `node` can stay as it is).

**Default engine (team lead sign-off, 2026-11-23): `ass`.** HyperFrames passed the speed and quality gate: on this CPU a 28.8 s clip takes about 44 s for the whole render stage in English and Hindi (overlay at 25 fps plus composite; about 10-16 s when the overlay is cached), the captions are correct (timing, pop, Devanagari conjuncts, position), and the audio is bit-identical to the ASS render. It still stays opt-in because (1) it needs an optional install (Node 22, the npm package, Chrome); as the default, every install without it would get a fallback warning on every clip, (2) it is about 5x slower than ASS per clip, and (3) existing renders count as ASS renders, so switching the default would make every existing final stale. Turn it on with `uv run autoclipper settings set captions.engine=hyperframes` or in **Settings**.

**Linux**
```bash
# 1. Node 22 LTS: download the linux-x64 tarball from https://nodejs.org/en/download and extract it
mkdir -p ~/tools && tar -xJf node-v22.*-linux-x64.tar.xz -C ~/tools && mv ~/tools/node-v22.*-linux-x64 ~/tools/node22
~/tools/node22/bin/node --version            # v22.x
# 2. HyperFrames (pinned), into ~/tools/hyperframes - PATH is changed for this one command only
PATH=$HOME/tools/node22/bin:$PATH npm --prefix ~/tools/hyperframes install --save-exact hyperframes@0.9.52
# 3. one-time headless Chrome download (about 100 MB, into ~/.cache/hyperframes)
~/tools/node22/bin/node ~/tools/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs browser ensure
```

**Windows (PowerShell)**
```powershell
# 1. Node 22: extract the win-x64 zip from https://nodejs.org/en/download to %USERPROFILE%\tools\node22
#    (node.exe at the top level of that folder), or: winget install OpenJS.NodeJS.LTS
& "$env:USERPROFILE\tools\node22\node.exe" --version
# 2. HyperFrames (pinned)
& "$env:USERPROFILE\tools\node22\npm.cmd" --prefix "$env:USERPROFILE\tools\hyperframes" install --save-exact hyperframes@0.9.52
# 3. one-time headless Chrome download
& "$env:USERPROFILE\tools\node22\node.exe" "$env:USERPROFILE\tools\hyperframes\node_modules\hyperframes\bin\hyperframes.mjs" browser ensure
# 4. tell Auto Clipper where node is (the zip has no bin\ folder; both layouts are accepted)
uv run autoclipper settings set "hyperframes.node_bin_dir=%USERPROFILE%\tools\node22"
```
With `winget`, Node is on PATH: set `hyperframes.node_bin_dir=""` (empty = use the `node` on PATH) instead.

**Enable it**
```bash
uv run autoclipper settings set captions.engine=hyperframes        # optional: hyperframes.style=karaoke
uv run autoclipper doctor                                          # node, hyperframes, chrome, ffmpeg vp9 alpha
uv run autoclipper rerender <job> <clip> <lang>                    # or resume a job: stale renders are redone
```
Or use the web UI: **Settings > Captions > Caption engine**, the **HyperFrames captions** section, then **Doctor**.

Notes:
- **Fallback.** If Node or HyperFrames is missing, too old, fails or times out (`hyperframes.timeout_seconds`) for a clip, that clip is rendered with the ASS captions instead. The job never fails because of it; the warning is shown in the job log (`WARNING ...`), in the render stage message (`final.mp4 (ASS fallback: ...)`) and as a yellow banner on the clip's review page. A resume (`run --job`) retries HyperFrames for those clips.
- **WebM alpha needs the libvpx decoder.** The overlay is VP9 WebM with alpha by default; FFmpeg must have the `libvpx-vp9` decoder (Linux distro builds and the gyan.dev full build on Windows do; `doctor` checks it). Otherwise set `hyperframes.format=mov` (ProRes 4444, much bigger files).
- **Speed.** About 35-45 s of HyperFrames rendering per 30 s clip on an 8-core CPU (software GL), plus the normal render. An unchanged composition is cached (`hf/overlay.json`), so a re-render that only changed the narration audio or title is fast.
- **Offline.** The page uses the GSAP copy bundled inside the hyperframes package and local font files; it never loads anything from the network. The renderer serves the page on `localhost`, so loopback must work.
- **Fonts.** The `.ttf` files of `captions.font_en` / `captions.font_hi` are copied next to the page (from `assets/fonts`, then `fc-match`, then the Windows font folders); Devanagari is shaped by Chrome. `captions.font_size`, the colours, `words_per_line` and `position_pct` mean the same as for ASS: the text is drawn at the same visual size as the libass captions (the CSS size is scaled by the font's em/line-height ratio, as libass does) with the same 5 px outline.
- The composition stays in `clips/NN/<lang>/hf/index.html`; you can open that folder in the HyperFrames desktop app to tweak it.
- The HyperFrames CLI runs with its working directory in that `hf/` folder and with every `ANTHROPIC_*` variable removed from its environment, so it never sees your `.env`.

### 7. Vocal separation with Demucs (optional)

With `separation.background = "no_vocals"` the narration plays over the clip **with its own speech removed** (music, game sound and effects stay), instead of over a ducked copy of someone else talking. [Demucs](https://github.com/adefossez/demucs) (PyPI `demucs`) does the separation. It needs PyTorch, so it goes in its **own** virtual environment under `~/tools/demucs` and Auto Clipper runs it as a separate process (`separation.python`). Nothing is added to PATH or to Auto Clipper's own environment. CPU is enough.

Linux / macOS:

```bash
uv venv --python 3.11 ~/tools/demucs
# CPU-only PyTorch from the official PyTorch index (the default PyPI torch for Linux pulls ~2.5 GB of CUDA libraries)
uv pip install --python ~/tools/demucs/bin/python --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1
uv pip install --python ~/tools/demucs/bin/python demucs==4.0.1 soundfile
uv run autoclipper settings set separation.background=no_vocals
uv run autoclipper doctor          # "demucs  OK  demucs 4.0.1, torch 2.5.1+cpu ..."
```

Windows (PowerShell):

```powershell
uv venv --python 3.11 $env:USERPROFILE\tools\demucs
uv pip install --python $env:USERPROFILE\tools\demucs\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1
uv pip install --python $env:USERPROFILE\tools\demucs\Scripts\python.exe demucs==4.0.1 soundfile
uv run autoclipper settings set "separation.python=~/tools/demucs/Scripts/python.exe" separation.background=no_vocals
uv run autoclipper doctor
```

(With an NVIDIA GPU you may install the CUDA build of torch instead; Auto Clipper still runs Demucs with `-d cpu`, which is fast enough for short clips.) The `htdemucs` model (~80 MB) is downloaded on the first separation into `separation.models_dir` (`~/tools/demucs/models`).

How it is used:
- Once per clip: `clips/NN/bed_no_vocals.wav` is cached with `bed_no_vocals.json` (sha256 of `cut.mp4` + model), and every language and re-render reuses it. A new cut or another model separates again.
- Measured here (16-core CPU, `htdemucs`, `segment` 7, 4 threads): a 38 s clip takes about 22-25 s and peaks at about 1.4 GB RAM. Lower `separation.segment` to use less memory.
- Any failure (Demucs missing, an error, longer than `separation.timeout_seconds`) falls back to the original audio for that clip, with a warning on the job page; the render does not fail.
- Only used with the voice-over on. Captions-only clips always keep their original audio.

### 8. Check everything

```bash
uv run autoclipper doctor
```

`doctor` checks:
- ffmpeg and ffprobe are on PATH,
- ffmpeg has the subtitles, ass, amix and atempo filters,
- the fonts are installed, either in assets/fonts or found by `fc-list`,
- Voicebox is reachable and the profile ids are set (skipped when `voiceover` is `false`),
- the API key is present,
- HyperFrames (optional, warnings only): the Node version in `hyperframes.node_bin_dir`, the installed `hyperframes` version, its headless Chrome, FFmpeg's `libvpx-vp9` decoder (WebM alpha) and whether the caption engine will work or fall back to ASS,
- Demucs (optional, a warning only): with `separation.background = "no_vocals"`, that `separation.python` imports `demucs` and `torch`; otherwise just whether it is installed.

It exits with code 1 if anything required is missing. HyperFrames and Demucs rows are only warnings.

---

## CLI usage

```bash
uv run autoclipper run <file-or-url> [--clips N] [--min S] [--max S] [--langs en,hi] [--job ID] [--no-voiceover | --voiceover]
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

### Captions only (no voice-over)

Turn the narration off when you only want captions on the clip's own audio:

```bash
uv run autoclipper settings set voiceover=false                 # every new job (or Settings > General > Voice-over narration)
uv run autoclipper run "~/Videos/talk.mp4" --no-voiceover       # just this job (--voiceover forces it on)
uv run autoclipper run --job 20261122-101500-ab12cd --no-voiceover   # switch an existing job, then resume it
```

On the web, the **New job** form has a **Voice-over narration** checkbox (it starts at the setting).

With the voice-over off, for every clip and language:
- **Script:** Claude still writes the title, description and hashtags in that language; the narration stays empty. On the clip page the narration field is hidden and the rest stays editable.
- **Voice:** skipped (`voice-over off`). Voicebox is never called and does not need to run; `doctor` does not check it.
- **Captions:** made from the original speech. When the clip language is the video's spoken language (the language Whisper detected), the captions are Whisper's own words and timings. Otherwise Claude translates each spoken line of the clip once (cached in `caption_translation.json`, so a re-render does not ask again; if a line was left out, the next render asks again) and each translated line is shown while its original line is spoken (long pauses skipped; a very short line is held a little longer so it can be read). The word highlight follows the line, not each spoken word. Both caption engines (`ass`, `hyperframes`) and the Devanagari font for Hindi work as usual.
- **Audio:** the original audio at `audio.original_volume_after`, with no ducking, then loudness-normalised as usual.
- **Publishing:** unchanged; the title, description and hashtags come from `metadata.json`.

Each job keeps the mode it was created with (the setting at that moment, or `--no-voiceover` / `--voiceover` / the New job checkbox), so changing the setting later only affects new jobs; jobs made before this option existed are voice-over jobs. To switch an existing job, use `run --job ID --no-voiceover` (or `--voiceover`), or on the job page pick **Mode: Voice-over + captions / Original audio + captions** and press **Apply & re-render** (this queues a run of the job that re-renders every clip made in the other mode; on an unfinished job it also runs the remaining steps, i.e. scripts, voices and renders, and the confirm dialog says so). Switching the mode makes the affected stages stale: a resume or a re-render redoes the captions and the render (and, when turning the voice-over on, writes a narration for a script that has none, keeping your title, and makes the voice). An earlier voice-over take is kept on disk, so switching back does not regenerate it.

### Audio mix (voice-over)

`audio.mix = "loudness"` (the default) sets the levels from measurements, not from fixed factors, so a loud game stream and a quiet lecture come out the same:
1. The voice (`voice.wav`, up to the end of the speech) and the background (the clip's audio, or the Demucs `no_vocals` stem) are measured with EBU R128 (ffmpeg `ebur128`).
2. The voice is brought to `audio.voice_lufs` (-16 LUFS; `audio.voice_volume` is a trim on that, 1.0 = none).
3. While the narration plays, the background sits `audio.background_db_below_voice` (18 dB) under the voice, measured over that same stretch. Before and after the narration it is as loud as the voice times `audio.original_volume_after` (1.0 = as loud as the voice). The two levels are joined with `audio.duck_ramp_seconds` ramps. A quiet background is never lifted by more than 12 dB.
4. The whole mix is loudness-normalised to `audio.target_lufs` and limited, as before.

`audio.mix = "gain"` is the old behaviour: the original at `audio.original_volume` (a factor) under the voice and `audio.original_volume_after` outside it, the voice at `audio.voice_volume`. `audio.original_volume` is used only by the gain mix. Older `settings.json` files load unchanged and get the loudness mix.

**Short narration.** When the narration ends before `audio.short_voice_below` (0.6) of the clip, `audio.short_voice = "trim_clip"` (default) ends the clip `audio.short_voice_tail` (1 s) after the narration, never under 3 s: the background stays at its under-voice level and fades out, instead of the loud original coming back for the rest of the clip. The captions all fall inside the narration, so they still fit. `"keep"` keeps the whole clip. A trimmed render is recorded in `render.json` (`audio.trimmed_to`, `audio.clip_duration`) and shown on the job and clip pages ("trimmed to 11.0 s (short narration, clip 38.2 s)").

**Lengthen retry timeout.** A too-short take gets one "lengthen" retry; that generation has its own limit `voicebox.lengthen_timeout_seconds` (240 s, never above `voicebox.timeout_seconds`). When it runs out, Voicebox is asked to cancel it, the first take is kept and the clip gets a warning ("the lengthened voice took longer than 240s ... kept the original 10.1s voice (26% of the 38.2s clip)"), stored in `voice.json` `warnings` and shown on the job page.

What a render did is in `render.json` `"audio"`: `mix`, `background` (`original` / `no_vocals`), the measured `voice_lufs` / `background_lufs_under_voice` / `background_lufs`, the gains, `voice_end`, `trimmed_to` and any separation `warning`. It also records `background_note` (e.g. "nearly silent after vocal removal" for a speech-only clip) and, with vocal separation, `separation_seconds` / `separation_cached`.

The voice-over audio settings (`audio.*` and `separation.background` / `model`) **are** part of the "re-render to apply" check (render.json `audio_key`): a voice-over render made with other audio settings, or with the old mix from before this version (every older final), shows "re-render to apply" on the job page and is redone by a resume. To apply them to a whole job, pick the job's current mode under **Mode** and press **Apply & re-render**: it re-renders every out-of-date clip (render only; the narrations and voices are kept). Voice-over-off renders do not depend on these settings.

**Cut-short voices.** Chatterbox sometimes stops a long take early (e.g. a 10 s voice for a 115-word Hindi narration that needs about 34 s). A take shorter than half of what its words need at `script.wps_en` / `script.wps_hi` is generated once more and the longer take kept. If it is still cut short it is marked `truncated` in voice.json, the clip gets a warning, it is never "lengthened" (a longer text would be cut short too), and the captions show only the words that are actually spoken (estimated, then aligned to the voice) instead of squeezing the whole text into the short voice. Voices made before this check get the same caption handling on their next re-render. With `audio.short_voice = trim_clip` such a clip ends about 1 s after the voice.

### Hook text (first seconds)

Every clip can open with a short, punchy **hook line** shown big at the top for the first seconds, separate from the captions (for example "He did not see this coming"). Claude writes it with the script, in the clip's language (Hindi in Devanagari, at most about 8 words, from the moment's hook) and it is stored as `"hook"` in `clips/NN/<lang>/script.json`. It works in both audio modes (with the voice-over off Claude writes it with the title).

- **Edit it** on the clip page (field **On-screen hook**; empty = no hook for that clip; the **Moment** line in the side panel is the English reason the clip was picked, not the on-screen text) or in `script.json`, then re-render the clip. Scripts made before this version have no hook until you type one.
- **Settings › Hook text** (`hook.*`): on/off, `duration` (2.5 s), `top_pct` (the hook's **top edge**, 8 % from the top; it grows downwards, so it never leaves the frame), `style` (`box` = text on a filled box, `plain` = outlined text), `font_size` (112, bigger than every caption preset), `uppercase`, `text_color` / `box_color` / `outline_color` (ASS `&HAABBGGRR`, with a colour picker next to each field) and `animation`. A second preview frame sits under the hook settings.
- **Never over the captions:** a hook that would reach the caption line (a long line, a big font, a low top edge) is drawn smaller, down to 55 % of `font_size`, and moved up if even that is too tall; both engines use the same layout and wrap width. Saving refuses (422 / CLI error) only a hook that is on and would *start* at or below the captions (`errors["hook.top_pct"]`); a hook that is off is never checked.
- **Claude's hook is checked:** a Hindi hook that is not in Devanagari is dropped (no hook rather than a Latin-script one). The text you type yourself is used as it is.
- **Engines:** the ASS engine fades the hook in and out. The HyperFrames engine animates it in (`pop` scales in, `slide` drops in, `fade`) and fades it out; if HyperFrames fails the ASS fallback still shows the hook. A clip with no spoken words still gets its hook.
- Changing the hook text of a rendered clip, or a hook setting while a hook is shown, marks the clip "re-render to apply"; a resume or re-render redoes only the render.

### Caption style presets and keyword emphasis

**Presets.** Settings › Captions › **Style preset** fills the style fields with a named bundle; you can still change any field afterwards (the page then says "changed by hand"). The preview next to it shows one frame with the current values (English / हिन्दी). Fonts, the position and the engine are never changed by a preset.

| Preset | Look |
|---|---|
| `clean` | Regular weight, 4 words per line, thin outline, soft yellow highlight |
| `bold_pop` | Big upper-case words, 2 per line, thick outline; the spoken word pops in yellow |
| `karaoke` | 4 words per line that fill with gold as they are spoken |
| `boxed` | White text on a dark box behind each line |
| `gaming_neon` | Upper case, purple outline glow, neon-green pop, 2 words per line |

CLI: `uv run autoclipper settings presets` lists them; `uv run autoclipper settings set captions.preset=bold_pop` applies one (add more `key=value` pairs after it to override fields, e.g. `captions.preset=bold_pop captions.font_size=80`).

The style fields (`captions.*`): `bold`, `uppercase` (Latin text; Devanagari has no case), `outline_color` / `outline_px`, `shadow_px`, `box` / `box_color`, `highlight_style` (`color` = the spoken word changes colour, `pop` = and grows, `karaoke` = spoken words stay lit, `none`; `null` = the engine default: ASS `color`, HyperFrames `hyperframes.style`), plus the existing size, colours and words per line. The defaults are the look of earlier versions, so existing settings and renders are unchanged.

**Keyword emphasis.** Claude also picks 2-5 **keywords** per clip (the most important words of the narration, or of what is said when the voice-over is off) and may give each one emoji. They are stored as `"keywords": [{"word": "clutch", "emoji": "🔥"}]` in `script.json` and can be edited on the clip page (**Keywords**: `clutch 🔥, insane, final round`). In the captions every occurrence is drawn in `captions.emphasis_color`, `captions.emphasis_scale` times bigger (both engines, at most `captions.emphasis_max_per_line` per line). Matching ignores case and punctuation, allows a known inflection ending only (`clutch` also marks `clutched`, `steal` marks `steals`; `star` does not mark `start`) and folds Hindi spelling variants (nukta, chandrabindu/anusvara), so it works for Whisper-word captions, narration captions and translated captions. A keyword that does not appear in the captions is simply not shown; with the voice-over on, keywords Claude picks that are not in the final (possibly shortened) narration are dropped when the script is written, and with it off a Hindi keyword must be in Devanagari. While a keyword is being spoken it takes the highlight colour like any other word (karaoke: once spoken), so the "current word" cue stays visible; with `captions.box` the ASS engine colours keywords but does not scale them (libass would draw a taller box behind them). The emoji must be one single emoji. The emoji is drawn after the keyword's first occurrence by the **HyperFrames engine only** (`captions.emoji`; libass cannot draw colour emoji, so the ASS engine shows the coloured word without it). HyperFrames uses Noto Color Emoji (SIL Open Font License 1.1): on Linux `sudo apt install fonts-noto-color-emoji` (or put `NotoColorEmoji.ttf` into `assets/fonts/`); on Windows download `NotoColorEmoji.ttf` from https://github.com/googlefonts/noto-emoji/tree/main/fonts into `assets\fonts\` (without it the page falls back to the system emoji font). It is linked (or, across file systems, copied) into the clip's local HyperFrames folder only when an emoji is drawn, so rendering stays offline; the font is not shipped in this repository (10 MB).

Changing a caption style field that affects a clip, its keywords or `captions.emphasis` marks it "re-render to apply" (render.json `style_key`; the other language's font, emphasis settings of a clip without keywords and the emoji switch with the ASS engine do not). Renders from before this version count as made with the old look: they stay current until you change a new style field, turn on a hook that the clip has, or the clip gets keywords.

---

## Usage: Web UI

```bash
uv run autoclipper serve                 # http://127.0.0.1:8765/ , opens a browser tab
uv run autoclipper serve --no-browser --port 8765
```

The page lets you start a job from an uploaded file or a URL, follow its progress, review each clip in each language (the video plays in the page), edit the narration, title, description and hashtags, re-render one clip, change the settings and run the doctor checks. Jobs and re-renders run one at a time in a single background worker, in order. The CLI and the web UI share `data/`; a `job.lock` file stops both from running the same job at once.

The server binds to loopback only by default. The page gets an access token (stored in `data/web/token`) when it is loaded from the same machine; API calls without it get 401, and with a wrong one 403. Stop the server with Ctrl-C, or `kill -INT <pid>` when it runs in the background.

**Currently running (restarted by the team lead on 2026-11-23 for step 3):** `uv run autoclipper serve --host 127.0.0.1 --port 8765 --no-browser`, PID `3180498` (the Python server process; its `uv run` parent is 3180489; also in `/tmp/claude-1000/ac_serve.pid`), log in `~/.cache/ac-step3/serve.log`. Stop it with:

```bash
kill -INT 3180498      # or: pkill -INT -f "autoclipper serve --host 127.0.0.1 --port 8765"
```

---

## Publishing (step 4)

Auto Clipper can upload a finished clip to **YouTube Shorts**, **Facebook Page Reels** and **Instagram Reels**. It sends the clip's `final.mp4` together with the title, description and hashtags from its `metadata.json`.

### What is posted where

| Clip language | YouTube | Facebook | Instagram |
|---|---|---|---|
| English (`en`) | the channel connected as **English** | the Page connected as **English** | the Instagram account connected as **English** |
| Hindi (`hi`) | the channel connected as **Hindi** | the Page connected as **Hindi** | the Instagram account connected as **Hindi** |

- English clips only go to the English accounts, and Hindi clips only go to the Hindi accounts.
- Publishing is refused if both languages point at the same channel, Page or Instagram account. To allow that, tick "Allow English and Hindi on the same account" under Accounts -> Advanced.
- **Nothing is posted while dry run is on.** On a fresh install, `publish.dry_run` is `true`. In that mode, Publish only writes the exact request plan and the page shows it:
  - The plan goes to `data/jobs/<job>/clips/NN/<lang>/publish/<platform>.json`.
  - It lists the endpoints, the metadata and the file size.
  - Every token in it is shown as `***`.
- Every platform starts **off**; you enable each one per language.
- YouTube uploads are **private** by default, so only you see them until you change the privacy to unlisted or public.
- Facebook Reels and Instagram Reels are always public once they are published.
- A real upload happens only when all of these are true:
  - dry run is off;
  - the platform is enabled and connected for that language;
  - you tick the platform in the clip's Publish panel;
  - you confirm the dialog (in the CLI, you also pass `--yes`).
- Publishing is idempotent:
  - A clip that is already published on a platform is not sent again; the panel shows the existing link instead. To post it again, tick "publish again" and type the confirmation word.
  - An interrupted YouTube upload is resumed rather than started as a second video.
  - If Facebook or Instagram may have published the reel but the answer was lost (a timeout, a crash), the clip shows "failed" with a note that the post may already be live. Pressing **Publish** again checks the platform first: it records the existing reel, or refuses with "check the account". It never posts a second copy unless you tick "publish again".
  - A clip whose narration was edited but not re-rendered cannot be published until you re-render it, so the new title is never paired with the old voice.
- Facebook reels are sent with Meta's "AI-generated" label (`is_ai_generated`), the same way YouTube marks the AI narration as synthetic media. You can turn this off per language with `publish.<lang>.facebook.is_ai_generated` in settings.json.

**Web:** you set up publishing on the **Accounts** page (top bar). It is where you connect accounts, switch dry run on or off, and enable platforms per language. To preview or publish a clip, use the **Publish** panel at the bottom of the clip's review page.

**CLI:**
```bash
uv run autoclipper accounts                                   # who is connected, what is enabled
uv run autoclipper publish <job> --clip 1 --lang hi --platform youtube --dry-run
uv run autoclipper settings set publish.dry_run=false         # turn real publishing on
uv run autoclipper publish <job> --clip 1 --lang hi --platform all --yes   # real upload
```

### Credentials: YouTube (Google OAuth, Desktop app)

You do this once, with your own Google Cloud project. No Google library is installed.

1. Create a project at <https://console.cloud.google.com/> (for example "Auto Clipper").
2. Go to **APIs & Services -> Library**, open **YouTube Data API v3** and press **Enable**.
3. Set up the **OAuth consent screen** (Google Auth Platform):
   - Choose **External**, enter the app name and your e-mail, and add yourself under **Test users**.
   - While the app is in **Testing**, its refresh tokens expire after **7 days**, so you would have to reconnect every week. To avoid that, press **Publish app** on the Audience page. A personal app does not need verification; Google shows an "unverified app" warning that you click through.
4. Create the client:
   - Go to **Credentials -> Create credentials -> OAuth client ID**.
   - Choose application type **Desktop app**. A "Web application" client is rejected.
   - Download the JSON file (`client_secret_<...>.json`).
5. Give the file to Auto Clipper:
   - Web: on the Accounts page, use "Upload client_secret.json".
   - CLI, Linux:
     ```bash
     uv run autoclipper accounts google-client ~/Downloads/client_secret_XXXX.json
     ```
   - CLI, Windows (PowerShell):
     ```powershell
     uv run autoclipper accounts google-client "$env:USERPROFILE\Downloads\client_secret_XXXX.json"
     ```
   - You can delete the downloaded file afterwards.
6. Connect each language separately.
   - **Web:**
     - On the Accounts page, under **English clips -> YouTube**, press **Connect YouTube**. Google's sign-in opens in a new tab.
     - Pick the **English** channel (Brand Account channels appear in the account chooser) and allow access. The tab then says "YouTube connected".
     - Do the same under **Hindi clips** and pick the Hindi channel.
   - **CLI:**
     - Run `uv run autoclipper accounts connect youtube --lang en`, then again with `--lang hi`.
     - Each run opens the browser and waits once on `127.0.0.1` for Google's redirect.
     - With `--no-browser`, it prints the URL for you to open.
   - **Same computer:** connect on the computer that runs Auto Clipper, with the page opened at `http://127.0.0.1:<port>` or `http://localhost:<port>`. Google only redirects a Desktop client back to a loopback address.
   - **Scopes:** Auto Clipper asks for `youtube.upload` (to upload) and `youtube.readonly`, which is used only to show which channel you connected.
7. Quota: a project gets about 100 `videos.insert` uploads a day. The quota resets at midnight Pacific time. When it runs out, the panel says "YouTube daily upload quota used up".

### Credentials: Facebook Page and Instagram (long-lived Page token)

You need a Facebook **Page**. For Instagram you also need a **professional** account (Business or Creator) that is **linked to that Page**. You link it in Instagram -> Settings -> Account type and tools, or in Meta Business Suite. Use one Page and Instagram account for English and another pair for Hindi.

1. Create a Meta app:
   - At <https://developers.facebook.com/apps/>, choose **Create app**, type **Business**.
   - Add **Facebook Login for Business**.
   - The app can stay in development mode as long as you only post to Pages and accounts that you manage.
2. Get a user token in the **Graph API Explorer** (<https://developers.facebook.com/tools/explorer/>):
   - Select your app and choose **Get User Access Token**.
   - Tick `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic` and `instagram_content_publish`. Add `business_management` if a Business Manager owns the Page.
   - Press **Generate Access Token** and approve.
3. Exchange it for a long-lived user token, valid about 60 days. The app id and app secret are under App settings -> Basic.
   - Linux:
     ```bash
     curl -s "https://graph.facebook.com/v27.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_USER_TOKEN"
     ```
   - Windows (PowerShell):
     ```powershell
     Invoke-RestMethod "https://graph.facebook.com/v27.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_USER_TOKEN"
     ```
4. Get the Page id and Page token. A Page token taken from a long-lived user token does not expire.
   - Linux:
     ```bash
     curl -s -H "Authorization: Bearer LONG_USER_TOKEN" "https://graph.facebook.com/v27.0/me/accounts?fields=id,name,access_token"
     ```
   - Windows (PowerShell):
     ```powershell
     Invoke-RestMethod -Headers @{Authorization = "Bearer LONG_USER_TOKEN"} "https://graph.facebook.com/v27.0/me/accounts?fields=id,name,access_token"
     ```
   - In the reply, each Page's `id` is the **Page id** and its `access_token` is the **Page token**.
5. Get the Instagram account id linked to the Page:
   - Linux:
     ```bash
     curl -s -H "Authorization: Bearer PAGE_TOKEN" "https://graph.facebook.com/v27.0/PAGE_ID?fields=instagram_business_account"
     ```
   - Windows (PowerShell):
     ```powershell
     Invoke-RestMethod -Headers @{Authorization = "Bearer PAGE_TOKEN"} "https://graph.facebook.com/v27.0/PAGE_ID?fields=instagram_business_account"
     ```
   - `instagram_business_account.id` is the **Instagram account id**.
6. Enter the values in Auto Clipper.
   - **Web:**
     - On the Accounts page, under **English clips** or **Hindi clips**, open "Connect a Page and Instagram account".
     - Fill in the Page id, the Page token and the Instagram account id.
     - The Instagram token field is optional. Fill it in only if Instagram rejects the Page token.
   - **CLI:**
     - Run `uv run autoclipper accounts connect meta --lang hi --page-id PAGE_ID --ig-user-id IG_ID`.
     - The token is read from a hidden prompt, never from the command line.
     - You can also put it in `AUTOCLIPPER_PAGE_TOKEN` or `AUTOCLIPPER_IG_TOKEN` instead.
   - **Check:** Auto Clipper checks the token with Facebook once and stores the Page and Instagram names.
7. Limits:
   - **Facebook Reels:** 9:16, 3-90 s long, and at most 30 reels a Page can publish through the API in 24 h.
   - **Instagram Reels:** 3 s to 15 min long, at most 300 MB, and at most 100 posts through the API in 24 h. A caption can have up to 2200 characters and 30 hashtags.
   - A clip outside these limits shows up as a problem in the dry-run plan and is not sent.

The Graph API version is **v27.0**. You can change it under Accounts -> Advanced.

### Where the secrets live, and disconnecting

- Everything is stored in `data/secrets/`:
  - `google_client.json` holds the OAuth client;
  - `youtube_en.json` and `youtube_hi.json` hold the refresh tokens;
  - `meta_en.json` and `meta_hi.json` hold the Page and Instagram tokens.
- The directory is created with mode `0700` and every file with mode `0600`.
- On Windows, file modes do not restrict access, and the project folder (for example `C:\code\auto-clipper`) may be readable by every local account. So the app replaces the ACL of `data\secrets` with "full control for your user only" (`icacls data\secrets /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`) the first time it is used; new files inherit it. To check, run `icacls data\secrets`: only your account should be listed. If the app logs that it could not set the ACL, run that command yourself.
- A token or the client secret is never shown again on any page, API response, log, plan file or error message. You only see "connected", the account name and the id.
- To disconnect, press **Disconnect** on the Accounts page, or run `uv run autoclipper accounts disconnect youtube|facebook|instagram|meta --lang hi`.
  - For YouTube, the refresh token is also revoked at Google.
  - For Facebook and Instagram, the stored token is deleted. To remove the app's access entirely, go to Facebook -> Settings -> Business integrations.
- Do not share or commit `data/secrets/`.

---

### Scheduled publishing

Open a clip, tick the platforms in the **Publish** panel, pick a date and time under **Schedule for later**
(your browser's time zone is shown), and press **Schedule…**. The **Schedule** page lists upcoming, done,
missed, failed and cancelled schedules; you can cancel a schedule (or one platform of it) or change its
time until it starts.

- **Scheduling is your confirmation.** It needs dry run off both when you schedule and when the time comes,
  connected and enabled accounts, and passes the same checks as Publish now. With dry run on, **Preview
  schedule (dry run)** only shows (and saves) the request plans with the time; nothing is stored.
- **Who publishes it:**

  | Platform | How | Server must run |
  |---|---|---|
  | YouTube, privacy **public** | Uploaded as private up to `native_lead_minutes` (60) before; YouTube makes it public at the time (`status.publishAt`) | at the hand-off |
  | YouTube, private or unlisted | YouTube can only schedule the switch to public, so Auto Clipper uploads it at the time | at the time |
  | Facebook | Uploaded before the time and finished with `video_state=SCHEDULED` (allowed 10 min to 29 days ahead); closer than 15 min, Auto Clipper posts it at the time | at the hand-off |
  | Instagram | No scheduling in Instagram's API: Auto Clipper posts it at the time | at the time |

  **Once handed to YouTube or Facebook, the platform publishes it**: cancelling in Auto Clipper or turning
  dry run on no longer stops it; cancel or move it there (YouTube Studio / Meta Business Suite). The confirm
  dialog shows each hand-off time, and the Schedule page warns when dry run is on while posts are handed off.
  If dry run is on at the hand-off time (or the YouTube privacy is no longer public, or the hand-off fails
  before anything was sent), the post is not handed over: Auto Clipper posts it itself at the time if dry run
  is off by then.
- **Keep `autoclipper serve` running.** If the server was off when a schedule came due, it is still posted
  when the server starts within `grace_minutes` (30); later it is marked **missed** with the time and how
  late it was, and nothing is posted. A schedule never posts twice (a second fire finds the published
  record and stops). Only one server fires the schedules of a data folder; a second `serve` on the same
  folder shows "another Auto Clipper server … runs the schedules" on the Schedule page.
- **Daily limits** per account and local day (Accounts page, *Scheduling*): YouTube 5, Facebook 10 (Facebook
  allows 30 per 24 h), Instagram 10 (Instagram allows 100 per 24 h). Published and pending posts count;
  over the limit the schedule is refused with the day and the numbers. Days are counted in
  `publish.schedule.timezone` (empty = this computer's zone), and a post handed over the evening before
  counts on the day it goes public.
- CLI (stored in `data/schedules.json`; fired by the running server):

  ```bash
  autoclipper schedule add <job> --clip 0 --lang hi --platform all --at "2026-11-24 18:30" --tz Asia/Dhaka --yes
  autoclipper schedule list [--state upcoming|done|missed|failed|cancelled|all] [--json]
  autoclipper schedule edit s_0123456789ab --at "2026-11-25 09:00"
  autoclipper schedule cancel s_0123456789ab [--platform instagram]
  ```

  Without `--yes` (or with dry run on) `add` prints the plans and stores nothing. On Windows without the
  `tzdata` package, pass the time with its offset (`--at 2026-11-24T18:30+06:00`) instead of `--tz`.

### Cover (thumbnail) per clip

The clip page has a **Cover** card: drag the slider to pick a frame (preview thumbnail), optionally type a
title (burned in at the top, Devanagari shaped correctly), and press **Use this frame**; or upload a JPEG,
PNG or WebP (up to 10 MB, 64-8192 px a side). Auto Clipper writes `cover.jpg` (1080×1920, at most 2 MB)
per clip and language. Publishing uses it where the official API allows:

| Platform | How the cover is sent | Limits |
|---|---|---|
| YouTube | `thumbnails.set` after YouTube has processed the upload (the API says a Short's thumbnail only sticks after processing) | 50 quota units; the channel must be allowed custom thumbnails (verified at youtube.com/verify) |
| Facebook | `POST /{video-id}/thumbnails` with `is_preferred=true` before the reel is published | 10 MB; the Page token may need `pages_manage_engagement` and `pages_read_user_content` |
| Instagram | `thumb_offset` = the picked frame, in milliseconds | No title text, no uploaded image: the API's `cover_url` must be a public URL, and Auto Clipper runs only on this computer |

A failing cover never fails the post: the status says "cover not set: …" and you can set it on the platform.
The dry-run plan shows the cover and how each platform will use it. Re-rendering the clip marks a frame
cover as made from an earlier render; pick it again (an uploaded image stays valid). Not verified against
the real Graph API: Facebook's docs give no order for the thumbnail and finish calls; Auto Clipper sends the
thumbnail first, and a failure there never stops the reel.

## Data layout

Everything is stored under `data/`, or under `$AUTOCLIPPER_DATA_DIR` if that is set:

```
data/
  settings.json                     all settings (see below)
  schedules.json                    scheduled publishing (G6); schedules.lock guards it
  jobs/<job_id>/
    job.json                        status + per-stage progress (job and per clip per language)
    source.mp4                      copied or downloaded source
    audio.wav                       16 kHz mono for ASR
    transcript.json                 segments + word timestamps
    moments.json                    chosen moments (start, end, hook, reason, score)
    clips/00/
      cut.mp4                       1080x1920 cut with original audio
      bed_no_vocals.wav / .json     Demucs no_vocals stem + its key (separation.background=no_vocals only)
      en/
        script.json                 narration, title, description, hashtags  <- editable
        voice_raw.wav               Voicebox output (not made with the voice-over off)
        voice.wav                   fitted voice (<= clip length)
        voice.json                  durations, tempo, narration_hash, warnings
        words.json                  timed caption words
        captions.json               voice-over off only: captions came from the original speech ("transcript" or "translation")
        caption_translation.json    voice-over off, other language: the translated caption lines (cache)
        captions.ass                word-by-word ASS subtitles (also the HyperFrames fallback)
        hf/                         HyperFrames composition (captions.engine=hyperframes): index.html, assets/, overlay.json
        captions_overlay.webm       transparent caption overlay rendered by HyperFrames (.mov with hyperframes.format=mov)
        final.mp4                   the short
        render.json                 how final.mp4 was rendered: caption engine/style/format/fps, fallback warning,
                                    "audio": mix, background, measured loudness, trim
        metadata.json               {"language","title","description","hashtags"}
        cover.jpg / cover.json      picked cover (1080x1920) and how it was made (G7)
        cover_upload.<ext>          uploaded cover source image, if any
      hi/ ...
    clips/01/ ...
```

---

## Settings reference

Change settings with `autoclipper settings set key.path=value`. The value is parsed as JSON when it can be, so `3`, `true`, `null` and `["en","hi"]` all work; anything else is stored as a string. Every change is validated before it is saved, and unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `languages` | `["en","hi"]` | Output languages (`en`, `hi`). `languages=en` also works. |
| `voiceover` | `true` | Voice-over narration. `false` = captions only: no narration or Voicebox, the original audio is kept and the captions come from the original speech (see [Captions only](#captions-only-no-voice-over)). Used for new jobs; each job keeps its own mode. `run --no-voiceover` / `--voiceover` override it per job |
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
| `voicebox.lengthen_timeout_seconds` | `240` | Max wait for the optional "lengthen" retry (10-7200, capped at `timeout_seconds`); the first take is kept with a warning when it runs out |
| `voicebox.english.profile_id` | `""` | English voice profile id (see `profiles`) |
| `voicebox.english.engine` | `chatterbox` | English TTS engine |
| `voicebox.hindi.profile_id` | `""` | Hindi voice profile id |
| `voicebox.hindi.engine` | `chatterbox` | Always forced to `chatterbox` for Hindi |
| `clips.count` | `5` | Clips per video (1-30) |
| `clips.min_seconds` / `clips.max_seconds` | `20` / `60` | Clip length bounds |
| `clips.width` / `clips.height` | `1080` / `1920` | Output size (240-3840, even numbers) |
| `clips.reframe` | `crop` | `crop` = center crop; `blur` = whole frame over a blurred fill |
| `audio.mix` | `loudness` | `loudness` = measured levels (see [Audio mix](#audio-mix-voice-over)); `gain` = the old fixed factors |
| `audio.voice_lufs` | `-16` | Loudness mix: voice level before the final loudnorm (-30 to -8 LUFS) |
| `audio.background_db_below_voice` | `18` | Loudness mix: background level under the narration, in dB below the voice (0-40) |
| `audio.original_volume` | `0.15` | Gain mix only: original audio level WHILE the voice speaks (ducked, 0-1) |
| `audio.original_volume_after` | `1.0` | Original audio level before and after the narration (0-2). Loudness mix: relative to the voice (1 = as loud as the voice); gain mix: a factor. Voice-over off: the whole clip |
| `audio.duck_ramp_seconds` | `0.4` | Fade time between the two levels (0-2 s) |
| `audio.voice_volume` | `1.0` | Voice level (0-2); a trim on `voice_lufs` in the loudness mix |
| `audio.short_voice` | `trim_clip` | A narration that ends early: `trim_clip` ends the clip shortly after it, `keep` keeps the whole clip |
| `audio.short_voice_below` | `0.6` | "Short" = the narration ends before this share of the clip (0.1-0.95) |
| `audio.short_voice_tail` | `1.0` | `trim_clip`: seconds the clip runs on after the narration (0-5; the clip is never under 3 s) |
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
| `captions.preset` | `custom` | Last style preset applied (`clean`, `bold_pop`, `karaoke`, `boxed`, `gaming_neon`); `settings set captions.preset=<name>` fills the style fields. See [presets](#caption-style-presets-and-keyword-emphasis) |
| `captions.bold` / `captions.uppercase` | `true` / `false` | Bold weight; upper-case text (Latin) |
| `captions.outline_color` / `captions.outline_px` / `captions.shadow_px` | black / `5` / `2` | Text outline colour (ASS) and width, shadow size (0-20 px) |
| `captions.box` / `captions.box_color` | `false` / `&H40000000` | A filled box behind each line instead of the outline |
| `captions.highlight_style` | `null` | `color` / `pop` / `karaoke` / `none`; `null` = ASS `color`, HyperFrames `hyperframes.style` |
| `captions.emphasis` | `true` | Draw the clip keywords (script.json `keywords`) in the emphasis colour |
| `captions.emphasis_color` / `captions.emphasis_scale` | orange / `1.2` | Keyword colour (ASS) and size (1-2 x) |
| `captions.emphasis_max_per_line` | `2` | Keywords emphasised per caption line (1-3) |
| `captions.emoji` | `true` | HyperFrames engine: the keyword's emoji after it |
| `hook.enabled` | `true` | Show the clip's hook line (script.json `hook`); see [Hook text](#hook-text-first-seconds) |
| `hook.duration` | `2.5` | Seconds from the start (1-5) |
| `hook.top_pct` | `8` | Top edge of the hook, % from the top (2-40; must start above the captions while the hook is on) |
| `hook.style` | `box` | `box` (filled box) or `plain` (outlined text) |
| `hook.font_size` / `hook.uppercase` | `112` / `false` | Largest hook size (30-200 px at 1080x1920; a long hook is drawn smaller to fit); upper-case |
| `hook.text_color` / `hook.box_color` / `hook.outline_color` | white / red / black | ASS `&HAABBGGRR` colours |
| `hook.animation` | `pop` | HyperFrames engine: `pop`, `slide` or `fade` in (ASS always fades) |
| `captions.engine` | `ass` | `ass` = libass burn-in; `hyperframes` = animated HyperFrames captions (optional install, see [6](#6-hyperframes-animated-captions-optional)); falls back to `ass` per clip |
| `hyperframes.node_bin_dir` | `~/tools/node22/bin` | Folder with `node` / `node.exe` (Node 22+); `<dir>/bin/node` is also accepted; `""` = the `node` on PATH. `~` and `%VAR%`/`$VAR` are expanded |
| `hyperframes.package_dir` | `~/tools/hyperframes` | Folder given to `npm --prefix` when installing `hyperframes` |
| `hyperframes.style` | `pop` | `pop` (the spoken word pops in the highlight colour) or `karaoke` (spoken words stay highlighted) |
| `hyperframes.fps` | `null` | Overlay frame rate (12-60); `null` = the clip's frame rate |
| `hyperframes.format` | `webm` | Overlay codec: `webm` (VP9 + alpha, small, needs FFmpeg's libvpx decoder) or `mov` (ProRes 4444) |
| `hyperframes.workers` | `0` | Parallel Chrome workers (0 = auto, max 8) |
| `hyperframes.timeout_seconds` | `300` | Max HyperFrames render time per clip-language (30-3600 s); longer falls back to ASS |
| `separation.background` | `original` | Background under the narration: `original` or `no_vocals` (speech removed with Demucs, see [7](#7-vocal-separation-with-demucs-optional)) |
| `separation.python` | `~/tools/demucs/bin/python` | Python of the Demucs venv (Windows: `~/tools/demucs/Scripts/python.exe`) |
| `separation.model` | `htdemucs` | Demucs model name |
| `separation.segment` | `7` | Seconds processed at once (1-60; the transformer models `htdemucs*` allow at most 7 and a larger value is rejected); lower = less memory |
| `separation.threads` | `4` | CPU threads for torch (0 = all cores, max 64) |
| `separation.timeout_seconds` | `900` | Max separation time per clip (30-7200 s); longer falls back to the original audio |
| `separation.models_dir` | `~/tools/demucs/models` | Where the model is downloaded (`TORCH_HOME`); `""` = torch's default |
| `web.max_upload_mb` | `4096` | Largest upload the web UI accepts, in MB (1-102400) |
| `web.allowed_extensions` | `[".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ogv"]` | Video file types the web UI accepts (lower-case, with the dot) |

`--clips`, `--min`, `--max` and `--langs` on `run` override these settings for that run only.

Changing `captions.engine` or `hyperframes.style` / `format` / `fps` makes existing renders stale: a resume (`run --job`) or a re-render redoes only the render stage of the affected clips (no new voice or captions). Renders from before step 3 count as ASS renders, so the caption engine does not make them stale (voice-over renders from before the loudness mix are stale for the audio, see "Audio mix").

---

## Development

```bash
uv run pytest -q        # fast suite; tests marked slow are skipped by default
uv run pytest -m slow   # only the slow tests (real models or network)
uv run pytest -m "hyperframes"   # the real HyperFrames render test (skipped when Node 22 / hyperframes is missing)
```

The default suite never needs Node: the HyperFrames subprocess is faked (`tests/fakes.py: FakeHFRunner`).

Tests never call the real Anthropic API, a real Voicebox server, or download whisper models or videos. They use the fakes in `tests/fakes.py` and a synthetic video made by ffmpeg. Each test gets its own data dir through `AUTOCLIPPER_DATA_DIR`, set in `tests/conftest.py`. The same conftest also guards the whole suite. It removes `ANTHROPIC_*` variables and points `HOME` at a temp dir. It makes `pipeline._make_llm/_make_voicebox/_make_transcriber` raise unless a test has `@pytest.mark.real_clients`, and it blocks socket connections to anything but loopback. Network or model tests must be marked `@pytest.mark.slow`.

The pipeline gets its LLM, Voicebox client and transcriber injected (`autoclipper.pipeline.Deps`). When none are passed, the real clients are built lazily, and only when a stage actually needs them.

---

## Roadmap

- **Step 1 (done): CLI pipeline end to end.**
- **Step 2 (done): web UI.** A FastAPI app with one background worker thread (no Redis) that runs `pipeline.run_job`. The UI reads progress from `job.json` and lets you edit `script.json` and settings in the browser, then re-render.
- **Step 3 (done): HyperFrames animated captions.** An optional caption engine (`captions.engine=hyperframes`) built from the same `words.json`, with a per-clip fallback to ASS. The default stays `ass` (lead sign-off 2026-11-23; see [6](#6-hyperframes-animated-captions-optional)).
- **Step 4: auto-publish.** English clips go to one channel and Hindi clips to another, on YouTube, Facebook and Instagram Reels, using `metadata.json`.
