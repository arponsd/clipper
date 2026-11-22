# Auto Clipper - Build Contract (Step 1: CLI pipeline)

Owner: Team Lead. Builders implement exactly these names/signatures. Additive extras are fine;
renames/removals need a TEAM_LOG note and lead sign-off. Architect files (`config.py`, `models.py`,
`jobstore.py`) keep their APIs.

## 0. Ground rules

- Python >=3.10, run everything with `uv run ...` from the project root. `uv sync` works (fixed: added
  `[tool.hatch.build.targets.wheel] packages = ["autoclipper"]`).
- **Cross-platform (Linux now, Windows later):** `pathlib` only; `subprocess.run([...])` with list args,
  never `shell=True`; find binaries with `shutil.which`; every text file read/written with
  `encoding="utf-8"`; JSON with `ensure_ascii=False` (Hindi). No `/tmp` hardcoding - use `tmp_path` / job dir.
- **Tests never hit the network**: no real Anthropic API, no real Voicebox, no yt-dlp downloads, no
  faster-whisper model download. Use `tests/fakes.py` (lead-owned): `FakeLLM`, `FakeVoicebox`,
  `FakeTranscriber`, `fake_transcript()`, `make_video()`, `make_tone()` (`from fakes import ...`).
  Tests that need real models/network must be marked `@pytest.mark.slow` and skipped by default.
- Shared interfaces live in `autoclipper/interfaces.py` (lead-owned): `LLM`, `VoiceboxAPI`,
  `Transcriber` Protocols and `ProgressFn = Callable[[float, str], None]`, `ShortenFn =
  Callable[[str, int], str]`, `LogFn = Callable[[str], None]`.
- Every stage function takes optional `on_progress: ProgressFn | None` and calls it with 0..1.
- Errors: each module raises its own `XxxError(RuntimeError)` with an actionable message. The pipeline
  catches and records them; the CLI prints `error: <message>` (no traceback unless `--debug`).
- **Clip indexes are 0-based everywhere** and equal the position in `job.clips` (that is what
  `JobStore.set_stage(clip=...)` indexes). Dirs: `clips/00`, `clips/01`, ... via `JobStore.clip_dir(i, lang)`.
- Languages: `"en"`, `"hi"`. Hindi text is Devanagari script.

## 1. Job directory layout (`data/jobs/<job_id>/`)

| Path | Written by | Content |
|---|---|---|
| `job.json` | `JobStore` (pipeline) | `models.Job` |
| `source.<ext>` | `stages.ingest.ingest` | copied file or yt-dlp download (mp4 preferred) |
| `audio.wav` | `stages.ingest.extract_audio_for_asr` | 16 kHz mono pcm_s16le |
| `transcript.json` | `stages.transcribe.run_transcribe` | `models.Transcript` JSON |
| `moments.json` | `stages.moments.run_moments` | JSON list of `models.Moment` dicts (validated, sorted by score desc) |
| `clips/NN/cut.mp4` | `stages.cut.cut_clip` | 1080x1920 h264/aac, original audio (silent track added if source has none) |
| `clips/NN/<lang>/script.json` | `stages.script.write_script` (and pipeline when voice shortens) | `models.ClipScript` JSON - **user-editable** |
| `clips/NN/<lang>/voice_raw.<ext>` | `stages.voice.make_voice` | bytes from Voicebox `/audio/{id}` |
| `clips/NN/<lang>/voice.wav` | `stages.voice.make_voice` | fitted voice, 44.1 kHz mono pcm_s16le, duration <= clip |
| `clips/NN/<lang>/voice.json` | `stages.voice.make_voice` | `VoiceResult` JSON (see 5.2) incl. `narration_hash` |
| `clips/NN/<lang>/words.json` | `stages.captions.make_captions` | JSON list of `models.Word` (timed script words; reused by HyperFrames in step 3) |
| `clips/NN/<lang>/captions.ass` | `stages.captions.make_captions` | ASS, PlayResX 1080 / PlayResY 1920 |
| `clips/NN/<lang>/final.mp4` | `stages.render.render_final` | final short |
| `clips/NN/<lang>/metadata.json` | `stages.render.write_metadata` | `{"language","title","description","hashtags"}` |

"Output exists" for idempotency = file exists and size > 0.

## 2. media builder

### `autoclipper/ffmpeg.py`
```python
class FFmpegError(RuntimeError): ...          # message includes the command summary + last ~20 stderr lines

@dataclass
class MediaInfo:
    duration: float; width: int | None; height: int | None; has_video: bool; has_audio: bool

def ffmpeg_bin() -> str                         # shutil.which("ffmpeg") or raise FFmpegError("ffmpeg not found on PATH ...install hint")
def ffprobe_bin() -> str
def run(args: list[str], *, desc: str = "ffmpeg") -> subprocess.CompletedProcess
    # args WITHOUT the binary; prepends ffmpeg_bin() -hide_banner -y -loglevel error; raises FFmpegError
def probe(path: Path) -> MediaInfo             # ffprobe -show_format -show_streams -of json
def duration(path: Path) -> float
def to_wav(src: Path, dst: Path, *, sample_rate: int = 16000, channels: int = 1) -> Path   # pcm_s16le
def atempo_chain(factor: float) -> str         # any factor>0 -> "atempo=2.0,atempo=1.25" (each link in [0.5, 2.0]); factor==1 -> "anull"
def change_tempo(src: Path, dst: Path, factor: float) -> Path
def trim_with_fade(src: Path, dst: Path, duration: float, fade: float = 0.3) -> Path   # audio: cut to duration, afade out last `fade` s
def reframe_filter(mode: Literal["crop", "blur"], width: int, height: int) -> str
    # filter_complex text reading [0:v] and producing [v]; crop = scale-to-cover + center crop;
    # blur = blurred cover background + fitted foreground overlaid center; ends with setsar=1,format=yuv420p
def cut_vertical(src: Path, dst: Path, start: float, end: float, *, width: int = 1080, height: int = 1920,
                 mode: str = "crop", on_progress: ProgressFn | None = None) -> Path
    # accurate seek, re-encode libx264 -crf 20 -preset veryfast, aac 192k 48 kHz stereo, +faststart;
    # if source has no audio, add anullsrc so cut.mp4 ALWAYS has an audio stream
def ass_filter(ass_path: Path, fonts_dir: Path | None) -> str
    # "subtitles=filename='<esc>':fontsdir='<esc>'" with filtergraph escaping that works for Windows
    # paths (C:\ -> C\:/ ...) and spaces/quotes. Test with a path containing a space.
def mix_burn(video: Path, dst: Path, *, voice: Path | None, ass: Path | None,
             original_volume: float, voice_volume: float, fonts_dir: Path | None,
             duration: float | None = None, on_progress: ProgressFn | None = None) -> Path
    # [0:a]volume=orig[a0];[1:a]volume=voice,apad[a1];[a0][a1]amix=inputs=2:normalize=0:duration=first[a]
    # video: subtitles burned via ass_filter (skip when ass is None); libx264 crf 20, aac 192k, +faststart;
    # output duration == video duration (-t duration if given). voice None -> original audio only at original_volume.
def synth_video(dst, seconds, width=1280, height=720, audio=True) -> Path   # optional; tests may use fakes.make_video
```

### `autoclipper/stages/ingest.py`
```python
class IngestError(RuntimeError): ...
def is_url(source: str) -> bool                  # http:// or https://
def find_source(job_dir: Path) -> Path | None    # existing source.* (ignore .part/.ytdl/.tmp)
def ingest(source: str, job_dir: Path, *, on_progress: ProgressFn | None = None) -> Path
    # idempotent: returns find_source() if present. Local: copy2 -> job_dir/f"source{suffix.lower()}"
    # (missing -> IngestError). URL: yt_dlp.YoutubeDL({"outtmpl": str(job_dir/"source.%(ext)s"),
    # "format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
    # "noplaylist": True, "quiet": True, "progress_hooks": [...]}) ; hook maps downloaded/total -> on_progress
def extract_audio_for_asr(source: Path, job_dir: Path) -> Path   # -> job_dir/"audio.wav" via ffmpeg.to_wav (16k mono); idempotent
```
Tests: monkeypatch `yt_dlp.YoutubeDL` with a fake that writes a file and calls the hooks.

### `autoclipper/stages/cut.py`
```python
def cut_clip(source: Path, moment: Moment, dst: Path, settings: ClipSettings,
             on_progress: ProgressFn | None = None) -> Path
    # ffmpeg.cut_vertical(source, dst, moment.start, moment.end, width=settings.width,
    #                     height=settings.height, mode=settings.reframe); dst = clip_dir(i)/"cut.mp4"
```

## 3. ai builder

### `autoclipper/llm.py`
```python
class LLMError(RuntimeError): ...
class LLMRefusal(LLMError): ...        # stop_reason == "refusal"; include stop_details.category / explanation
class LLMTruncated(LLMError): ...      # stop_reason == "max_tokens"

class AnthropicLLM:                    # implements interfaces.LLM
    def __init__(self, settings: LLMSettings, client: Any | None = None): ...
        # client None -> anthropic.Anthropic(api_key=os.environ[settings.api_key_env]);
        # missing/empty key -> LLMError("ANTHROPIC_API_KEY is not set (put it in .env)")
    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict
    def pick_moments(...) / write_script(...) / shorten(...)   # signatures exactly as interfaces.LLM
```
**Verified call shape on installed `anthropic==2.12.0`** (wire body checked offline with an
`httpx2.MockTransport`; the SDK uses `httpx2` internally, not `httpx`):
```python
kwargs = dict(model=settings.model, max_tokens=16000, system=system,
              messages=[{"role": "user", "content": user}],
              output_config={"effort": settings.effort,
                             "format": {"type": "json_schema", "schema": schema}})
# do NOT pass `thinking` (Opus 5.5 is adaptive by default; {"type":"disabled"} / budget_tokens -> 400)
# do NOT pass temperature/top_p/top_k, do NOT prefill an assistant turn (400)
if settings.use_fallbacks:
    resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
else:
    resp = client.messages.create(**kwargs)
```
- `client.messages.create` has `output_config` but NO `betas`/`fallbacks`; `client.beta.messages.create`
  has all three. `fallbacks` type: `"default"` or `[{"model": ...}]`.
- Check `resp.stop_reason` BEFORE reading content: `"refusal"` -> `LLMRefusal` (guard `resp.stop_details`
  may be None), `"max_tokens"` -> `LLMTruncated`.
- `resp.content` may contain `thinking` and (beta) `fallback` blocks: join only blocks with
  `block.type == "text"`, then `json.loads`; bad JSON -> `LLMError` with first 200 chars.
- Schemas for structured output: every object needs `"additionalProperties": False` and `required`.
- Catch SDK errors most-specific first (`anthropic.AuthenticationError`, `RateLimitError`,
  `APIStatusError`, `APIConnectionError`) and re-raise as `LLMError` with a clear message.
- Tests: inject a fake `client` object exposing `.messages.create` / `.beta.messages.create`
  returning `SimpleNamespace(stop_reason=..., stop_details=..., content=[SimpleNamespace(type="text", text=...)])`,
  or use `httpx2.MockTransport` via `anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=...), max_retries=0)`.

### `autoclipper/stages/transcribe.py`
```python
class TranscribeError(RuntimeError): ...
def resolve_device(settings: WhisperSettings) -> tuple[str, str]
    # device "auto": "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu";
    # compute_type "auto": "float16" on cuda, "int8" on cpu. (This machine: CUDA count 0 -> cpu/int8.)
class WhisperTranscriber:              # implements interfaces.Transcriber
    def __init__(self, settings: WhisperSettings): ...   # lazy model load on first transcribe()
    def transcribe(self, audio: Path, language: str | None = None,
                   on_progress: ProgressFn | None = None) -> Transcript
        # word_timestamps=True, vad_filter=True; language falls back to settings.language;
        # progress = segment.end / info.duration
def run_transcribe(audio: Path, out_json: Path, transcriber: Transcriber, *,
                   language: str | None = None, on_progress: ProgressFn | None = None) -> Transcript
    # idempotent: load out_json if it exists; else transcribe and write Transcript JSON
def transcribe_voice(voice_wav: Path, lang: str, transcriber: Transcriber) -> list[Word]
    # flat list of words for caption timing (language forced to lang)
```

### `autoclipper/stages/moments.py`
```python
def format_transcript(t: Transcript, start: float | None = None, end: float | None = None) -> str
    # one line per segment: "[12.3-15.8] text" (segments overlapping [start,end] when given)
def validate_moments(raw: list[Moment], t: Transcript, *, count: int, min_seconds: float,
                     max_seconds: float) -> list[Moment]
    # clamp to [0, t.duration]; snap start/end to nearest segment boundaries; enforce min/max
    # (extend/shrink within video); drop invalid and overlapping (keep higher score);
    # sort by score desc; keep top `count`
def run_moments(t: Transcript, llm: LLM, settings: ClipSettings, out_json: Path) -> list[Moment]
    # idempotent (load out_json if exists); uses settings.count/min_seconds/max_seconds
```

### `autoclipper/stages/script.py`
```python
WORDS_PER_SECOND = {"en": 2.6, "hi": 2.2}; FILL_RATIO = 0.9
def count_words(text: str) -> int                                # whitespace split
def target_words(duration: float, lang: str) -> int              # round(duration*wps*0.9), >= 5
def normalize_script(s: ClipScript) -> ClipScript                # title <= 70 chars; hashtags "#x", no spaces, dedupe, max 8
def write_script(*, llm: LLM, transcript: Transcript, moment: Moment, lang: str, out_json: Path) -> ClipScript
    # clip_transcript = format_transcript(transcript, moment.start, moment.end);
    # if count_words(narration) > target*1.15 -> llm.shorten once; normalize; write out_json
def load_script(path: Path) -> ClipScript
def make_shorten_fn(llm: LLM, lang: str) -> ShortenFn            # lambda narration, max_words: llm.shorten(lang=..., ...)
```
Prompting: narration is NEW commentary over the clip (not a translation/dub of what is said);
`hi` = natural conversational Hindi in Devanagari (English loanwords ok, no romanized Hindi).

## 4. voice_captions builder

### `autoclipper/voicebox.py` (httpx - the plain `httpx` package)
```python
class VoiceboxError(RuntimeError): ...
class VoiceboxClient:                  # implements interfaces.VoiceboxAPI
    def __init__(self, url: str = "http://127.0.0.1:17493", *, poll_interval: float = 1.0,
                 timeout: float = 600.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic): ...
    @classmethod
    def from_settings(cls, s: VoiceboxSettings, **kw) -> "VoiceboxClient"
    def health(self) -> None              # GET /profiles; ConnectError -> "Voicebox is not reachable at <url> - is it running?"
    def list_profiles(self) -> list[dict] # accept list or {"profiles": [...]}
    def generate(self, text: str, *, profile_id: str, language: str, engine: str) -> str
        # POST /generate json {text, profile_id, language, engine}; id from "id" or "generation_id"
    def wait(self, gen_id: str, on_progress: ProgressFn | None = None) -> dict
        # GET /history/{id} until status.lower() == "completed"; "failed"/"error" -> VoiceboxError(with message/error field);
        # overall timeout -> VoiceboxError
    def download(self, gen_id: str, dst: Path) -> Path   # GET /audio/{id} bytes
    def synthesize(self, text, *, profile_id, language, engine, dst, on_progress=None) -> Path
```
Empty `profile_id` -> `VoiceboxError("no Voicebox profile set for <lang>: run `autoclipper profiles` then `autoclipper settings set voicebox.english.profile_id=<id>`")` (raise in voice stage).

### `autoclipper/stages/voice.py`
```python
class VoiceResult(BaseModel):
    path: str; narration: str; narration_hash: str; raw_duration: float; final_duration: float
    clip_duration: float; tempo: float; shortened: bool; trimmed: bool
def narration_hash(text: str) -> str                 # sha256 of text.strip() (utf-8), hex
def engine_for(lang: str, voice: VoiceProfile) -> str   # "hi" -> always "chatterbox"; else voice.engine
def make_voice(*, client: VoiceboxAPI, narration: str, lang: str, voice: VoiceProfile,
               clip_duration: float, out_dir: Path, max_speedup: float,
               shorten: ShortenFn | None = None, on_progress: ProgressFn | None = None) -> VoiceResult
    # synthesize -> out_dir/"voice_raw.wav" -> ffmpeg.to_wav(44100, mono) -> measure.
    # d <= clip: tempo 1.0. d/clip <= max_speedup: atempo d/clip. Else if shorten and not yet shortened:
    # max_words = floor(count_words * clip*max_speedup/d * 0.95), shorten, regenerate, re-evaluate once.
    # Still too long: atempo max_speedup then trim_with_fade(clip_duration, 0.3), trimmed=True.
    # Writes out_dir/"voice.wav" and out_dir/"voice.json" (VoiceResult). Returns result; result.narration
    # is the text actually spoken (pipeline writes it back into script.json if it changed).
def load_voice_result(out_dir: Path) -> VoiceResult | None
```

### `autoclipper/stages/captions.py`
```python
def split_words(text: str) -> list[str]
def distribute_words(words: list[str], duration: float, start: float = 0.0) -> list[Word]   # by character length
def align_words(script_words: list[str], recognized: list[Word], duration: float) -> list[Word]
    # monotonic match (normalize case/punctuation; difflib-style), interpolate unmatched between
    # neighbours; < 30% matched or no recognized words -> distribute_words fallback
def build_ass(words: list[Word], *, lang: str, settings: CaptionSettings, width: int = 1080, height: int = 1920) -> str
    # [Script Info] PlayResX/PlayResY, WrapStyle 2; style font = settings.font_hi if lang=="hi" else font_en;
    # group into lines of settings.words_per_line; one Dialogue per word, showing the whole line with
    # the active word wrapped in {\c<highlight_color>}...{\c<primary_color>}; Alignment 2, MarginV=margin_v;
    # escape { } \ in text; times H:MM:SS.cc
def make_captions(*, narration: str, voice_wav: Path, lang: str, transcriber: Transcriber,
                  settings: Settings, out_dir: Path) -> Path
    # words = align_words(split_words(narration), transcribe_voice(voice_wav, lang, transcriber), duration(voice_wav))
    # writes out_dir/"words.json" + out_dir/"captions.ass"; returns captions.ass path
```

### `autoclipper/stages/render.py`
```python
def resolve_fonts_dir() -> Path | None     # config.FONTS_DIR if it holds any .ttf/.otf, else None (system fontconfig)
def render_final(*, cut: Path, voice: Path | None, ass: Path | None, dst: Path, audio: AudioSettings,
                 on_progress: ProgressFn | None = None) -> Path
    # ffmpeg.mix_burn(cut, dst, voice=voice, ass=ass, original_volume=audio.original_volume,
    #                 voice_volume=audio.voice_volume, fonts_dir=resolve_fonts_dir(), duration=probe(cut).duration)
def write_metadata(script: ClipScript, lang: str, dst: Path) -> Path
    # {"language": lang, "title": ..., "description": ..., "hashtags": [...]} utf-8, ensure_ascii=False
```

## 5. orchestration builder

### `autoclipper/pipeline.py`
```python
@dataclass
class Deps:
    llm: LLM; voicebox: VoiceboxAPI; transcriber: Transcriber
def default_deps(settings: Settings) -> Deps   # AnthropicLLM(settings.llm), VoiceboxClient.from_settings(...), WhisperTranscriber(settings.whisper)
def effective_settings(settings: Settings, *, langs=None, count=None, min_seconds=None, max_seconds=None) -> Settings  # model_copy w/ overrides
def run_job(source: str, *, settings: Settings | None = None, deps: Deps | None = None,
            langs: list[str] | None = None, count: int | None = None, min_seconds: float | None = None,
            max_seconds: float | None = None, job_id: str | None = None, log: LogFn = print) -> JobStore
    # job_id given -> JobStore.load (resume); else JobStore.create(source).
    # ingest(+extract_audio_for_asr) -> transcribe -> moments -> (populate job.clips: ClipState(index=i,
    # moment=m, langs={l: ClipLangState(stages={s: StageState() for s in CLIP_STAGES})})) -> cut (per clip)
    # then for each clip, each lang: script -> voice -> captions -> render (+metadata).
    # Skip any stage whose output exists (mark "skipped" only if never run; else keep "done").
    # Per clip-lang exceptions: set that stage "failed", ClipLangState.error = str(e), continue others.
    # Job-level stage failure: job.status="failed", job.error, re-raise PipelineError.
    # End: job.status = "failed" if every clip-lang failed, else "done". ClipLangState.final = "clips/NN/<lang>/final.mp4".
    # Deps are built lazily: default_deps only if deps is None (and only the parts needed).
def rerender(job_id: str, clip: int, lang: str, *, settings: Settings | None = None,
             deps: Deps | None = None, log: LogFn = print) -> Path
    # load script.json (user edits); regenerate voice only if narration_hash(script.narration) != voice.json.narration_hash
    # (or voice missing); always rebuild captions + final + metadata; update job.json stages.
class PipelineError(RuntimeError): ...
```
Log line format: `[<job_id>] <stage> <clip>/<lang> <pct>% <message>` (clip/lang omitted for job stages).

### `autoclipper/cli.py` - `main(argv: list[str] | None = None) -> int`
`run <file-or-url> [--clips N --min S --max S --langs en,hi --job ID]`, `rerender <job> <clip> <lang>`,
`status <job>`, `jobs`, `profiles`, `settings show`, `settings set key.path=value` (value parsed as JSON,
else string; re-validated via `Settings.model_validate`), `doctor`. Exit 0 ok / 1 error / 2 usage.
`doctor` checks: ffmpeg+ffprobe on PATH, libass (`ffmpeg -filters` has `subtitles`), fonts (`fc-list`
or assets/fonts has Noto Sans + Noto Sans Devanagari), Voicebox reachable + profile ids set,
API key env present, `node --version` >= 22 (warn only; this machine has v16.20.2).

## 5a. Approved additive APIs (recorded by Fixer after lead triage; additive, nothing above changes)
- `ffmpeg.run(args, *, desc="ffmpeg", on_progress=None, total=None)`: with both `on_progress` and
  `total > 0` (output seconds) progress comes from `-progress pipe:1` (capped at 0.99, then 1.0). If
  `on_progress` raises (any BaseException), the ffmpeg child is killed and reaped, then the exception
  propagates; the `.part` output is removed by the atomic-output wrapper.
- `ffmpeg.escape_filter_path(path) -> str` (quoted filtergraph value, Windows `C\:/...`) and
  `ffmpeg.synth_video(dst, seconds, width=1280, height=720, audio=True) -> Path`.
- Error types, all `RuntimeError` subclasses: `stages.moments.MomentsError`, `stages.script.ScriptError`,
  `stages.voice.VoiceError`, `stages.captions.CaptionsError`, `stages.render.RenderError`.
- `stages.moments.SNAP_TOLERANCE = 5.0` (seconds; approved).
- Load/save helpers: `stages.transcribe.load_transcript/save_transcript`, `stages.moments.load_moments/
  save_moments`, `stages.script.load_script/save_script`. LLM schema constants: `llm.MOMENTS_SCHEMA`,
  `llm.SCRIPT_SCHEMA`, `llm.SHORTEN_SCHEMA`.
- `pipeline.run_job(..., debug=False)`, `pipeline.rerender(..., debug=False)`,
  `pipeline.parse_langs(value) -> list[str] | None`, `pipeline.output_exists(path) -> bool`.
- `stages.voice.MIN_VOICE_SECONDS = 0.2`: a take shorter than this raises `VoiceError` (no voice.json).
- `VoiceResult.path` is the ABSOLUTE path of voice.wav at generation time; consumers should prefer
  `out_dir / "voice.wav"` (the job dir may have moved).
- Pipeline staleness is persistent: before voice regenerates, `words.json`, `captions.ass`, `final.mp4`
  are deleted; before captions regenerate, `final.mp4` is deleted. When moments.json is regenerated on
  resume, `job.clips` is rebuilt from it and changed/dropped `clips/NN` dirs are renamed to
  `clips/NN.stale-<n>` (never deleted). KeyboardInterrupt (any BaseException) marks the running stage
  `failed`/"interrupted", sets `job.status="failed"`, `job.error="interrupted"`, saves, re-raises.
- `cli settings set`: a non-quoted numeric-looking value for a str field (e.g. `profile_id=12345`,
  `primary_color=1e5`) is kept as the string typed; `null` still means None.

## 6. Environment facts (checked by lead)
- anthropic 2.12.0 (uses httpx2), faster-whisper 1.2.1, yt-dlp 2026.10.26, httpx 0.28.1, pytest 9.2.0.
- ffmpeg 7.1.1 at /usr/bin/ffmpeg. No CUDA (ctranslate2 cuda devices = 0).
- System fonts present: Noto Sans, Noto Sans Devanagari (/usr/share/fonts/truetype/noto). assets/fonts empty.
- node v16.20.2 (HyperFrames later needs 22+; doctor warns).
