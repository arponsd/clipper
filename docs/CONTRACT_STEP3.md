# Auto Clipper - Build Contract (Step 3: HyperFrames animated captions)

Owner: Team Lead. Builders implement exactly these names, signatures, files and keys. Additive extras are
fine; renames/removals need a TEAM_LOG note and lead sign-off. Everything in `docs/CONTRACT.md` and
`docs/CONTRACT_WEB.md` still holds: cross-platform rules (`pathlib`, list-args `subprocess`, no
`shell=True`, `encoding="utf-8"`, `ensure_ascii=False`), the single writer of job.json, tests never hit the
network or real services.

**Goal.** `captions.engine = "ass" | "hyperframes"`. With `hyperframes`, each clip+language gets a
HyperFrames composition (HTML/CSS/GSAP) built from the same `words.json` the ASS path uses. It is rendered
by the HyperFrames CLI to a transparent overlay video and composited over `cut.mp4` by the existing
FFmpeg render, so the audio mix (ducking, ramp-back, loudnorm, limiter) is unchanged. If HyperFrames is
missing, fails or times out for a clip, that clip falls back to the ASS path with a clear warning. The job
never fails because of HyperFrames.

---

## 0. Smoke-render findings (lead, 2026-11-22; these are the facts the design rests on)

Install (done, user-local, nothing global):
```bash
mkdir -p ~/tools/hyperframes
PATH=$HOME/tools/node22/bin:$PATH TMPDIR=$HOME/.cache/ac-step3/tmp \
  npm --prefix ~/tools/hyperframes install --save-exact --no-fund --no-audit hyperframes@0.9.52
# -> ~/tools/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs  (CLI entry; `hyperframes --version` = 0.9.52)
```
- **The version is pinned** to `hyperframes@0.9.52`, the version of the installed HyperFrames skill
  plugin. npm is from the official registry (repository github.com/heygen-com/hyperframes).
- **Chrome.** The renderer uses chrome-headless-shell. It was already cached at
  `~/.cache/hyperframes/chrome/chrome-headless-shell/linux-152.0.7977.30/...`. On a fresh machine,
  `hyperframes browser ensure` downloads it (one time, about 100 MB). `hyperframes browser path` prints
  the path and never downloads anything.
- **Node.** Node is `~/tools/node22/bin/node` v22.23.5. The CLI works when it is started as
  `<node> <hyperframes.mjs> ...`, even with the system node v16 first on PATH. We still put
  `node_bin_dir` first on the **child's** PATH.

**Exact working commands** (all from the composition dir; the env vars are listed in 3.3):
```bash
NODE=~/tools/node22/bin/node; HF=~/tools/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs
$NODE $HF --version                                    # 0.9.52, 35 ms
$NODE $HF browser path                                 # chrome path, 0.12 s (doctor)
$NODE $HF lint                                         # 0 errors for our template
$NODE $HF render . --format webm --fps 30 -o out/cap.webm          # VP9 + alpha
$NODE $HF render . --format mov  -o out/cap.mov                     # ProRes 4444 + alpha
$NODE $HF render . --format png-sequence -o out/frames              # RGBA PNGs frame_000001.png...
```

**Output formats with alpha** (2 s smoke: 1080x1920, 30 fps, one animated word):

| `--format` | Result (ffprobe) | Size (2 s / 28.8 s) | Render wall time (2 s / 28.8 s) |
|---|---|---|---|
| `webm` | vp9, `pix_fmt=yuv420p` + tag `ALPHA_MODE=1` (alpha in a side channel) | 114 KB / 96 KB | 8.7 s / 34-45 s |
| `mov` | prores, `yuva444p12le` | 6 MB / 27 MB | 8.2 s / 37-41 s |
| `png-sequence` | directory of RGBA PNGs | 1.8 MB / 14 MB | 6.8 s / 22.7 s |

- **WebM alpha needs the libvpx decoder.** FFmpeg's native `vp9` decoder **drops the alpha**: I checked
  this, and the caption composited onto a black box. The input must be opened with
  `-c:v libvpx-vp9 -i overlay.webm`. ProRes 4444 needs no decoder flag. The local ffmpeg 7.1.1 has the
  `libvpx-vp9` decoder, and so does the gyan.dev full build on Windows.
- **Real clip timing** (sample job clip 01/hi, 28.84 s, 60 words, 20 groups, i5-13400 16 threads, software
  GL, `--workers auto`, which picked 2-3):
  - The HyperFrames render took **34-45 s**.
  - The FFmpeg composite took **4.4 s**.
  - `--workers 1` took 42.7 s, the same.
  - Peak RSS was about 1.1-1.3 GB.
  - This is far under the 3 min budget.
- **Offline works.** The same render ran inside a network namespace with only loopback up (`unshare -rn`,
  `ip link set lo up`): rc 0. This held because GSAP and the fonts are local files (3.2). The renderer
  serves the page from `http://localhost:<port>`, so loopback must work.
- **Progress** goes to stdout as lines `@hf-progress {"code":"capture","done":35,"total":90,"pct":43}`
  (codes `compile`, `extract_video`, `process_audio`, `start_browsers`, `capture`, `encode`; `pct` 0-100).
  They are suppressed by `--quiet`, so do **not** pass `--quiet`. Success ends with `@hf-result {...}` and
  rc 0. Failure prints `✗  Render failed` with the reason on stdout and exits non-zero.
- **The CLI reads `.env` from its cwd.** It warned "ignoring ANTHROPIC_API_KEY" when started from the
  project root. Always run it with `cwd=<composition dir>` and strip secrets from the child env (3.3).
- **Devanagari shaping in Chrome is correct.** These came out right: नमस्ते (स्त), तिहत्तर (त्त), छियासी,
  चैलेंजर (anusvara + e-matra). They used Noto Sans Devanagari Bold loaded by `@font-face` from a copied TTF.
- **Gotchas found (and the template rules that avoid them):**
  1. A caption group styled `height: 0` (to centre on a line) rendered **nothing**: the frames were fully
     transparent. Use `top: Ypx; transform: translateY(-50%)` on the group instead. GSAP only tweens its
     `opacity`.
  2. In the **rendered** output, whitespace text nodes between inline-block words and flex `gap` were lost
     ("सातहीरो"). They were present in `snapshot`. Space the words with `margin: 0 0.13em` on each word span (lead triage: a 0.26em gap, about one space).
  3. Lint `gsap_animates_clip_element`: never set or tween `visibility`/`display` on a `.clip`. The runtime
     hides a clip outside `[data-start, data-start+data-duration)`. Fade only `opacity`, or tween children.
  4. The lint warning `timeline_track_too_dense` (20+ groups on one track) is harmless; ignore it.
  5. Render prints `screenshot capture · software gpu` (BeginFrame did not run). That is the slower path,
     but the timing above already includes it.
- **The Chrome profile is left behind in `$TMPDIR`** (`puppeteer_dev_chrome_profile-*`). We point `TMPDIR`
  at a per-render dir and delete it afterwards. /tmp is 98 % full, so we never use it.

**Lead decision on the default:** `captions.engine` stays **`"ass"`** by default. HyperFrames needs an
optional install of Node 22, the npm package and Chrome. With `hyperframes` as the default, every user
without that install would get a fallback warning on every clip. The measured speed (about 45 s per 30 s
clip) would allow it as a default, and the lead will re-check that at sign-off after a real end-to-end run.
Users turn it on with `autoclipper settings set captions.engine=hyperframes`.

**Sign-off (2026-11-23): confirmed `"ass"`.** The real end-to-end run on a copy of the sample job passed the gate (clip 01, 28.8 s: about 44 s render stage for en and for hi, correct frames, audio bit-identical to ASS and to the step-2 code). The default stays `ass` for the install and fallback-noise reasons above, and because switching it would make every existing final stale. See TEAM_LOG.

---

## 1. File ownership (no overlaps)

| Builder | Files |
|---|---|
| **engine** | NEW `autoclipper/stages/hyperframes.py`, NEW `autoclipper/stages/hf_template.py`; `tests/fakes.py` (ADD only: `make_alpha_overlay`, `FakeHFRunner`); NEW `tests/test_hyperframes.py`; `pyproject.toml` (marker `hyperframes` only) |
| **integration** | `autoclipper/config.py` (section 2 fields), `autoclipper/models.py` (`ClipLangState.warning`, additive), `autoclipper/stages/render.py`, `autoclipper/pipeline.py`, `autoclipper/cli.py` (doctor only); NEW `tests/test_step3_integration.py` |
| **web_docs** | `autoclipper/web/server.py` (clip detail fields, section 7), `autoclipper/web/static/app.js`, `styles.css`, `index.html` (labels, warning badge, render_stale hint); `README.md`; NEW `tests/test_web_step3.py` |
| **lead** | this contract, TEAM_LOG, final review, real end-to-end run, default-engine decision |

- `ffmpeg.py`, `jobstore.py`, `voicebox.py`, `llm.py` and `stages/{ingest,cut,transcribe,moments,script,voice,captions}.py`
  are **not** edited.
- `render.py` may keep importing the private ffmpeg helpers, as in step 2.
- Integration imports only the public API of section 3. Until engine lands, integration tests monkeypatch
  `autoclipper.stages.hyperframes.make_overlay`.

---

## 2. Settings (integration adds them to `config.py`; all have defaults, so old settings.json still loads)

```python
class CaptionSettings(BaseModel):            # existing fields unchanged
    engine: Literal["ass", "hyperframes"] = "ass"          # NEW

class HyperFramesSettings(BaseModel):        # NEW section Settings.hyperframes
    node_bin_dir: str = "~/tools/node22/bin"   # dir holding node / node.exe; "" -> shutil.which("node")
    package_dir: str = "~/tools/hyperframes"   # npm --prefix dir; CLI = <dir>/node_modules/hyperframes/bin/hyperframes.mjs
    style: Literal["pop", "karaoke"] = "pop"
    fps: int | None = Field(None, ge=12, le=60)  # None = cut.mp4 frame rate (rounded, clamped 12..60)
    format: Literal["webm", "mov"] = "webm"      # overlay codec: VP9+alpha (small) or ProRes 4444 (big)
    workers: int = Field(0, ge=0, le=8)          # 0 = HyperFrames "auto"
    timeout_seconds: float = Field(300.0, ge=30, le=3600)   # whole render of ONE clip-lang overlay
    # validators: node_bin_dir / package_dir: <= 500 chars, no NUL / newline

class Settings(BaseModel):
    ...
    hyperframes: HyperFramesSettings = Field(default_factory=HyperFramesSettings)
```
- Paths go through `os.path.expandvars` then `Path(...).expanduser()` when used, never at validation time,
  so the stored string stays as the user typed it. On Windows, `~` expands to `%USERPROFILE%`.
- They are editable with `autoclipper settings set captions.engine=hyperframes hyperframes.style=karaoke`.
  `hyperframes.fps=null` works.
- They are editable in the web Settings page through the schema: enums become `<select>`, and
  `fps` `null` becomes an empty input. The frontend adds labels and help text (section 7).
- The existing caption fields drive both engines: `font_en`, `font_hi`, `font_size`, `words_per_line`,
  `primary_color`, `highlight_color`, `position_pct`, `margin_v`.

---

## 3. Engine module - `autoclipper/stages/hyperframes.py` (engine builder)

```python
HF_PACKAGE = "hyperframes"; HF_VERSION_PIN = "0.9.52"; MIN_NODE_MAJOR = 22

class HyperFramesError(RuntimeError): ...              # any failure -> caller falls back to ASS
class HyperFramesUnavailable(HyperFramesError): ...    # node/CLI/gsap missing or node < 22 (no render attempted)
class HyperFramesTimeout(HyperFramesError): ...

@dataclass(frozen=True)
class HFEnv:
    node: Path; cli: Path; node_version: str; hf_version: str | None
    gsap: Path  # vendored gsap.min.js inside the installed package

def resolve_node(s: HyperFramesSettings, which=shutil.which) -> Path | None
    # node_bin_dir non-empty: try <dir>/node.exe (Windows) / <dir>/node, then <dir>/bin/node[.exe]; "" -> which("node")
def node_version(node: Path, run=_run) -> str            # "v22.23.5"; raises HyperFramesUnavailable
def resolve_cli(s: HyperFramesSettings) -> Path | None   # <package_dir>/node_modules/hyperframes/bin/hyperframes.mjs if is_file()
def hf_version(node: Path, cli: Path, run=_run) -> str | None   # `<node> <cli> --version`, timeout 30 s
def find_gsap(cli: Path) -> Path | None
    # cli.parent.parent / "dist/vendor/npm/gsap@*/dist/gsap.min.js", highest version; NEVER a CDN URL
def probe_env(s: HyperFramesSettings) -> HFEnv            # raises HyperFramesUnavailable with an actionable message:
    # "HyperFrames not installed at <path> - see README 'HyperFrames captions'" / "node not found in <dir>" /
    # "node v16.20.2 is too old - HyperFrames needs Node 22+"
def child_env(s: HyperFramesSettings, tmp_dir: Path, base: Mapping[str,str] | None = None) -> dict[str,str]   # 3.3

def ass_color_to_css(color: str) -> str                   # "&H0000E5FF" -> "rgba(255,229,0,1)"  (AABBGGRR, AA 00 = opaque)
def find_font_file(family: str, *, bold: bool = True) -> Path | None    # 3.2
def build_composition(words: list[Word], *, lang: str, duration: float, settings: Settings,
                      out_dir: Path, gsap: Path, fps: int, width: int = 1080, height: int = 1920) -> Path
    # writes out_dir/index.html + out_dir/assets/{fonts,vendor}; returns index.html. Pure, no subprocess.
def composition_hash(out_dir: Path) -> str                # sha256 over index.html bytes + names/sizes of assets files
def render_overlay(comp_dir: Path, dst: Path, *, env: HFEnv, s: HyperFramesSettings, fps: int,
                   duration: float, on_progress: ProgressFn | None = None, runner: Runner | None = None) -> Path
def verify_overlay(path: Path, *, fmt: str, width: int, height: int, duration: float) -> None   # 3.5; raises HyperFramesError
def make_overlay(*, words_json: Path, cut: Path, lang: str, settings: Settings, out_dir: Path,
                 on_progress: ProgressFn | None = None, runner: Runner | None = None) -> Path
    # the one entry point render.py calls; out_dir = clips/NN/<lang>; returns out_dir/f"captions_overlay.{fmt}"
```
`Runner` is the subprocess seam that tests fake:
`Runner = Callable[[list[str], Path, dict[str, str], float, Callable[[str], None]], int]`. The arguments
are `(args, cwd, env, timeout, on_line)`, and it returns the return code. The default `_popen_runner`:
- starts `subprocess.Popen(args, cwd=cwd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True,
  encoding="utf-8", errors="replace")`,
- on POSIX with `start_new_session=True`, on Windows with `creationflags=CREATE_NEW_PROCESS_GROUP`,
- reads lines, calls `on_line(line)`, and enforces `timeout` (wall clock).

On timeout, or when `on_line` raises (cooperative cancel or Ctrl-C, i.e. any `BaseException`), it **kills
the whole tree**, because Chrome children must not be left behind:
- POSIX: `os.killpg(pid, SIGTERM)`, wait 5 s, then `SIGKILL`.
- Windows: `taskkill /T /F /PID <pid>`.

It reaps the child, then raises `HyperFramesTimeout` (timeout) or re-raises the exception.

### 3.1 make_overlay flow (cached, atomic)
1. `env = probe_env(settings.hyperframes)`. `HyperFramesUnavailable` propagates.
2. `words = [Word(**w) for w in json.load(words_json)]`. A missing, empty or bad words.json raises
   `HyperFramesError`. `info = ffmpeg.probe(cut)`, so `duration = info.duration` and width and height
   come from the cut. `fps = settings.hyperframes.fps or round(cut fps)` clamped to 12..60. Use the
   `r_frame_rate` of the cut, read with ffprobe; if it is unreadable, use 30.
3. `comp = out_dir / "hf"`. Rebuild it with `build_composition` each time (it is cheap), so the folder
   always matches the current words and settings.
4. **Cache.** `out_dir/"hf"/"overlay.json"` holds `{"composition_sha256", "format", "fps", "duration",
   "hf_version"}`. If it matches the current values and the overlay file exists (size > 0) and passes
   `verify_overlay`, return it without rendering. This saves about 40 s when only the audio or the script
   title changed.
5. **Render.** Write to `comp/"out"/f"overlay.{fmt}"`. Then `verify_overlay`, then `os.replace` it to
   `out_dir/f"captions_overlay.{fmt}"` and write overlay.json atomically (`_atomic.write_text_atomic`).
6. Always delete `comp/".tmp"` and `comp/"out"` in a `finally`. Keep `hf/index.html` and `hf/assets/`:
   the user can open them in the HyperFrames desktop app. Delete any old
   `captions_overlay.<other fmt>` file.
7. Progress: `@hf-progress` `pct` / 100 maps onto `on_progress(f, f"hyperframes {code}")`, monotonic and
   capped at 0.99. Lines that do not parse are kept in a deque of 30 lines for the error message.
8. Errors carry the last 15 output lines. Example: `HyperFramesError("hyperframes render failed (rc 1):
   ... net::ERR_...")`.

### 3.2 Composition template (`hf_template.py`; one standalone `index.html`, no sub-compositions)
Rules (all checked by `hyperframes lint` = 0 errors in the smoke; `test_hyperframes.py` asserts them on
the string):
- `<html lang="{lang}">`, `<meta charset="UTF-8">`, `<meta name="viewport" content="width={W}, height={H}">`.
- `<script src="assets/vendor/gsap.min.js"></script>`. This is a copy of `HFEnv.gsap`. There is **no
  network URL anywhere** in the file, and a test asserts that `http` is absent.
- Fonts: `@font-face { font-family: "AC Caption EN" | "AC Caption HI"; src: url("assets/fonts/<file>");
  font-weight: 700; }`. The files are copied from `find_font_file(settings.captions.font_en / font_hi)`.
  The lookup order is:
  1. `config.FONTS_DIR` (stems that match the family with spaces removed, e.g. `NotoSansDevanagari-Bold`,
     then `-Regular`),
  2. Linux/macOS: `fc-match -f "%{file}" "<family>:bold"`, accepted only if the returned file's family
     matches; otherwise
  3. Windows: `cli.windows_font_dirs()`, by the same stem rule.

  If nothing is found, fall back to `src: local("<family>")` and log a warning; Chrome on Linux then
  finds system fonts through fontconfig. The font stack is
  `"AC Caption HI", "AC Caption EN", sans-serif` for `hi`, and EN first for `en`. Bold Devanagari is
  `NotoSansDevanagari-Bold.ttf`.
- `html, body, #root`: exact `W`x`H` px, `background: transparent`, `overflow: hidden`. The root is
  `<div id="root" data-composition-id="main" data-start="0" data-duration="{duration:.3f}"
  data-width="{W}" data-height="{H}" data-fps="{fps}">`.
- **Groups.** Words are grouped exactly as the ASS path does: consecutive chunks of
  `captions.words_per_line`. Reuse the chunking helper from `captions.py` if one exists; otherwise
  `words[i:i+n]`. One `<div id="g{i}" class="cap clip" data-start data-duration data-track-index="1">`
  is used per group.
  - The group's `start` is its first word's `start`.
  - Its `end` is the next group's start, or `min(duration, last.end + 0.6)` for the last group, and never
    before `last.end`.
  - `data-duration` is at least 0.05.
  - Groups never overlap, so only one is visible at a time.
- **Group CSS** (the gotcha-1 fix; the ASS path in `captions.build_ass` chunks inline as `words[k:k+per_line]`, so do the same):
  ```css
  .cap { position: absolute; left: 5%; width: 90%; top: Ypx; transform: translateY(-50%);
         display: flex; flex-wrap: wrap; justify-content: center; overflow: visible; }
  ```
  `Y = position_pct/100*H`. When `position_pct` is null, use `bottom: margin_v px` and no translate.
- **Word CSS** (the gotcha-2 fix): each word is
  `<span id="w{i}_{k}" class="w">{html.escape(text)}</span>`, styled
  `display:inline-block; margin: 0 0.13em; font-size: {font_size*1.08:.0f}px; font-weight:700;
  line-height:1.25; color: {primary}; -webkit-text-stroke: {max(4, font_size//11)}px #000;
  paint-order: stroke fill; text-shadow: 0 4px 14px rgba(0,0,0,.55)`.
  - The text comes from words.json only; the ids are generated.
  - The script holds **no text**, only ids and numbers: a JSON array `const W = [[id, start, end], ...]`
    and `const G = [[id, start, fade], ...]` (plus the font family aliases `const F`), emitted with `json.dumps(...).replace("</", "<\\/")`.
- **Timeline.** `const tl = gsap.timeline({ paused: true }); ...; window.__timelines["main"] = tl;` The
  timeline is registered last. It is built in `build()`, which runs after `document.fonts.load(...)` for
  every caption family and `document.fonts.ready` (so word widths are measured with the real fonts). It uses `fromTo` only, and has no
  `Date.now`/`Math.random`/timers/`repeat:-1`.
  - **pop** (default):
    - Each word does `fromTo(w, {scale:1, color:primary}, {scale:k, color:highlight, duration:0.08,
      ease:"back.out(3)"}, start)`, then `to(w, {scale:1, color:primary, duration:0.08}, max(start+0.08, end))`.
      `k = max(1, min(1.18, 1 + 2*margin/offsetWidth, 972/offsetWidth))` per word: the pop uses at most
      the word's own side margin and never exceeds the 90 % box (lead triage of the visual review).
    - A group fades in (`fromTo(g, {opacity:0}, {opacity:1, duration:0.06}, gstart)`) only when no group
      was on screen right before it (`fade` = 1). A group that replaces another one switches at once, like
      ASS; the old fade-in left a blank frame at every line change.
  - **karaoke**:
    - A word turns `highlight` at its `start` (`duration:0.05`) and **stays** highlighted until its group
      ends. It does not scale.
    - Groups fade in as in pop.
  - Never tween or set `visibility`/`display` on a `.clip`; the runtime ends the group at its
    `data-duration`.
- **Determinism.** The same inputs give a byte-identical `index.html`, and a test checks it. That is what
  makes the cache in 3.1 step 4 valid.

### 3.3 CLI invocation
```python
args = [str(env.node), str(env.cli), "render", str(comp_dir), "--format", fmt, "--fps", str(fps),
        "--quality", "looks", "-o", str(comp_dir / "out" / f"overlay.{fmt}")]
if s.workers: args += ["--workers", str(s.workers)]
runner(args, comp_dir, child_env(s, comp_dir / ".tmp"), s.timeout_seconds, on_line)
```
`child_env`:
- starts from `os.environ`;
- **removes** every `ANTHROPIC_*` variable and the variable named by `settings.llm.api_key_env`;
- sets `PATH = <node dir> + os.pathsep + PATH`;
- sets `TMPDIR` = `TEMP` = `TMP` = `<comp>/.tmp`, created first;
- sets `HYPERFRAMES_NO_UPDATE_CHECK=1`, `HYPERFRAMES_SKIP_SKILLS=1`, `HYPERFRAMES_NO_TELEMETRY=1`,
  `DO_NOT_TRACK=1`, `HYPERFRAMES_NO_AUTO_INSTALL=1` and `HYPERFRAMES_NO_OPTIONAL_INSTALL=1`.

The cwd is always `comp_dir`, because the CLI loads `.env` from its cwd.

### 3.4 Fallback rules (implemented in render.py, section 4)
Any `HyperFramesError` falls back to ASS **for that clip-lang only**: unavailable, render failure,
timeout, verify failure, or missing or bad words.json. So does any unexpected `Exception` raised inside
`make_overlay`. `KeyboardInterrupt` and cooperative cancel are **not** fallbacks: they propagate after
the process tree is killed. Fallback means:
- `render_final(..., ass=captions.ass, overlay=None)`, exactly the step-2 path;
- the warning text `"HyperFrames captions failed (<short reason, <= 200 chars>); used ASS captions"`
  goes to:
  1. the job log as a `render` line, prefixed `WARNING`;
  2. the render `StageState.message`, as `"final.mp4 (ASS fallback: <short reason>)"`;
  3. `ClipLangState.warning`, a new field of type `str | None`, reset to `None` at the start of each
     render.
- The job and the clip-lang still end `done`, and `error` stays `None`.

`HyperFramesUnavailable` is checked **once per run_job/rerender**: the result of `probe_env` is memoised
in the pipeline for that call, so 10 clips do not run `node --version` 10 times. Each clip still gets its
own warning line.

### 3.5 verify_overlay
`ffmpeg.probe` checks that there is a video stream, that width and height equal the cut's, and that
`|duration - cut duration| <= max(0.25, 2/fps)`. Alpha must be present:
- webm: ffprobe `stream_tags.alpha_mode == "1"` (case-insensitive key);
- mov: `pix_fmt` starts with `yuva` or contains `rgba`/`argb`.

Read these with one `ffprobe -show_streams -of json` call; `ffmpeg.probe` does not expose the tags, so
the engine calls `ffmpeg.ffprobe_bin()` itself. Any mismatch raises `HyperFramesError("overlay check
failed: ...")`.

---

## 4. Render stage changes - `autoclipper/stages/render.py` (integration)

```python
def render_final(*, cut, voice, ass, dst, audio, on_progress=None, words_json=None,
                 overlay: Path | None = None) -> Path      # NEW kwarg; ass and overlay are mutually exclusive (RenderError)

@dataclass
class RenderOutcome:
    path: Path; engine_used: Literal["ass", "hyperframes"]; warning: str | None

def render_clip(*, cut: Path, out_dir: Path, settings: Settings, on_progress=None,
                hf_env_cache: dict | None = None) -> RenderOutcome
    # engine "ass" -> render_final(ass=out_dir/"captions.ass") (unchanged behaviour)
    # engine "hyperframes" -> overlay = hyperframes.make_overlay(...) with progress 0..0.7, then
    #   render_final(overlay=overlay, ass=None) with progress 0.7..1; on fallback (3.4) -> ASS render 0.7..1
def render_key(settings: Settings) -> dict
def write_render_stamp(out_dir: Path, settings: Settings, outcome: RenderOutcome) -> Path
def render_is_current(out_dir: Path, settings: Settings) -> bool
```
**FFmpeg composite.** Only the video branch of `render_final` changes. The audio graph, `-t`, codecs and
`+faststart` stay byte-for-byte the same. The overlay is the **last** input:
```text
... -i cut.mp4 [-i voice.wav] [-f lavfi -i anullsrc...] [-c:v libvpx-vp9] -i captions_overlay.webm
-filter_complex "[0:v][K:v]overlay=0:0:format=auto:eof_action=pass,format=yuv420p[v];<audio graph unchanged>"
-map [v] -map [a] <_X264> <_AAC> -t <cut duration> -movflags +faststart final.mp4
```
- `-c:v libvpx-vp9` goes right before `-i overlay` **only for `.webm`**. It is an input option.
- `K` is the overlay's input index, computed the same way the other indexes are.
- The command was verified on the sample clip: 28.84 s took 4.4 s, the captions showed at 72 %, and the
  Devanagari was correct.
- If the `libvpx-vp9` decoder is missing (checked once with `ffmpeg -hide_banner -decoders`, cached), a
  webm overlay is treated as `HyperFramesError("ffmpeg has no libvpx-vp9 decoder (needed for WebM alpha);
  set hyperframes.format=mov")` **before** the HyperFrames render runs, so it falls back quickly.

---

## 5. Pipeline changes - `autoclipper/pipeline.py` (integration)

- `_run_render` calls `render.render_clip(...)`. Then:
  - it writes `render.json`, the stamp;
  - it sets `state.warning = outcome.warning`;
  - on a warning it logs `rep.line("render", 1.0, "WARNING " + warning, ...)` and calls
    `rep.done("render", ..., "final.mp4 (ASS fallback: ...)")`.
- The captions stage is **unchanged** and always writes `words.json` and `captions.ass`. The ASS file is
  the fallback, and it is still needed when the engine is hyperframes.
- `_unlink_outputs` lists add `"render.json"` wherever `"final.mp4"` is deleted. `captions_overlay.*` and
  `hf/` are **not** deleted on voice or captions regeneration: the composition hash cache in 3.1 makes
  them self-invalidating.
- On re-cut (`_CUT_DEPENDENT_OUTPUTS`), also delete `captions_overlay.webm`, `captions_overlay.mov` and
  `render.json`.

### 5.1 Staleness keys (`clips/NN/<lang>/render.json`)
```json
{"caption_engine": "hyperframes", "engine_used": "hyperframes", "style": "pop", "fps": 25,
 "format": "webm", "hf_version": "0.9.52", "warning": null}
```
`render_key(settings)`:
- `{"caption_engine": "ass"}` for ass;
- `{"caption_engine": "hyperframes", "style": ..., "format": ..., "fps": settings.hyperframes.fps}` for
  hyperframes. `fps` here is the **setting** (null = auto), not the resolved value.

`render_is_current(out_dir, settings)` is true only when all of these hold:
- `final.mp4` exists (size > 0);
- the stamp's fields for those keys equal `render_key(settings)`;
- `stamp["engine_used"] == stamp["caption_engine"]`. A fallback render is **stale**, so a resume retries
  HyperFrames, for example after the user installs it.

A missing `render.json` counts as `{"caption_engine": "ass", "engine_used": "ass"}`. So step-2 jobs stay
current with the default engine and are **not** re-rendered.

- `run_job`: the render stage is skipped only if `not regenerated and render_is_current(...)`. Otherwise
  it re-renders. Switching the engine or style in Settings and then resuming re-renders exactly the
  affected clip-langs, with no voice or captions work.
- `rerender`: unchanged semantics (always rebuilds captions and final), now through `render_clip`, so it
  works with either engine. The overlay cache keeps it fast when only the audio changed.
- `_job_complete` is unchanged. A fallback is `done`, not an error.

---

## 6. Doctor - `autoclipper/cli.py` (integration; the web `/api/doctor` reuses `run_doctor`, so it gets this for free)

Replace `check_node()` with `check_hyperframes(settings, run=_run_cmd) -> list[Check]`, which returns
these checks in this order:

| name | ok | warn |
|---|---|---|
| `node` | `v22.23.5 (/home/u/tools/node22/bin/node)`, major >= 22 | not found in node_bin_dir / older than 22 / cannot run |
| `hyperframes` | `0.9.52 (<package_dir>)` and the CLI runs `--version` | not installed (with the README install command), or the version differs from `HF_VERSION_PIN` (still usable) |
| `hyperframes chrome` | path from `<node> <cli> browser path` | missing -> hint `<node> <cli> browser ensure` |
| `ffmpeg vp9 alpha` | `libvpx-vp9 decoder` in `ffmpeg -decoders` | missing -> "set hyperframes.format=mov" (only checked when format is webm) |
| `caption engine` | `ass`, or `hyperframes` with all of the above ok | `hyperframes` set but a prerequisite is missing -> "clips will fall back to ASS captions" |

- Every check above is `warn`, never `fail`, because HyperFrames is optional and falls back. The doctor's
  exit code is unchanged.
- No check touches the network: `--version` and `browser path` are local.
- Every subprocess runs with `child_env` and `cwd=` the package dir, with a timeout of 30 s.
- The tests fake `run` and the filesystem.

---

## 7. Web (web_docs)

- **Clip detail** (`GET /api/jobs/{id}`, per clip-lang) gets three **additive** fields:
  - `"warning": str | null` (from `ClipLangState.warning`);
  - `"caption_engine": "ass" | "hyperframes" | null` (`engine_used` from render.json; `"ass"` when
    final.mp4 exists without a stamp, `null` when there is no final);
  - `"render_stale": bool` (final exists and `render_settings_changed(out_dir, load_settings())`, i.e. the
    render_key differs from the stamp; an ASS fallback with unchanged settings is not stale for the UI,
    although `render_is_current` stays False so resume retries it - lead triage, step 3).
- **Review page.**
  - A warning shows as a yellow banner with the warning text as is ("HyperFrames captions failed (...); used
    ASS captions"), with no extra prefix.
  - `render_stale` shows a hint: "Caption engine/style changed - re-render to apply".
  - A small "Captions: HyperFrames (pop)" / "Captions: ASS" label is shown.
- **Settings page.**
  - The schema already includes `captions.engine` and the `hyperframes.*` section.
  - The frontend adds labels and help text:
    - node_bin_dir: "Folder with node (Node 22+)";
    - package_dir: "Folder where `npm --prefix` installed hyperframes";
    - fps empty = "same as the clip".
  - `hyperframes.*` fields are greyed (still editable) while engine is `ass`.
  - The CSP is unchanged.
- Rerender from the UI needs no backend change: the worker calls `pipeline.rerender`, which uses
  `render_clip`.
- Tests: the fields are present and correct for three cases: no stamp, a stamp with a fallback, and a
  stale engine. The settings PUT round-trips `captions.engine` and `hyperframes.fps: null`. A 422 is
  returned for `hyperframes.style="x"`, `fps=5`, `timeout_seconds=1` and `workers=99`.

## 8. README (web_docs)

There is a new section **"HyperFrames animated captions (optional)"**. It replaces "6. Node 22+ (only for
step 3, later)".

- **Linux:**
  - download the Node 22 LTS `linux-x64` tarball from nodejs.org and extract it to `~/tools/node22`, so
    that `~/tools/node22/bin/node` exists; the system node can stay as it is;
  - then `PATH=$HOME/tools/node22/bin:$PATH npm --prefix ~/tools/hyperframes install --save-exact
    hyperframes@0.9.52`;
  - then `~/tools/node22/bin/node ~/tools/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs browser
    ensure`, which is a one-time Chrome download.
- **Windows (PowerShell):**
  - either the Node 22 zip (`win-x64`) extracted to `%USERPROFILE%\tools\node22` (node.exe at the top
    level), or `winget install OpenJS.NodeJS.LTS`;
  - then `& "$env:USERPROFILE\tools\node22\npm.cmd" --prefix "$env:USERPROFILE\tools\hyperframes" install
    --save-exact hyperframes@0.9.52`;
  - then `browser ensure` as on Linux, with `node.exe`;
  - set `hyperframes.node_bin_dir` to `%USERPROFILE%\tools\node22` (Settings page, or `settings set`).
    The resolver also accepts a dir without `bin/`.
- **Enable it:**
  - `uv run autoclipper settings set captions.engine=hyperframes`, optionally with
    `hyperframes.style=karaoke`;
  - then run `uv run autoclipper doctor`;
  - then re-render a clip, or resume a job: stale renders are redone.
- The settings reference gets rows for `captions.engine` and every `hyperframes.*` key.
- Notes to include:
  - the WebM alpha needs the FFmpeg libvpx decoder (the gyan full build has it); otherwise use `mov`;
  - expected speed is about 45 s per 30 s clip on an 8-core CPU, plus the normal render;
  - the fallback behaviour;
  - `hf/index.html` can be opened in the HyperFrames desktop app.
- Update the Roadmap and the data layout: `render.json`, `captions_overlay.webm` and `hf/` are added.

---

## 9. Test plan (default suite: no Node, no network, no real HyperFrames)

**engine, `tests/test_hyperframes.py`:**
- `resolve_node`:
  - `bin/` and no-`bin/` layouts;
  - `node.exe` on a faked Windows layout;
  - an empty dir falls back to `which`;
  - nothing found gives `None`.
- `probe_env` raises `HyperFramesUnavailable` for each case: no node, node v16 (faked `--version`), no
  CLI file, no gsap.
- `child_env` strips `ANTHROPIC_*` and the `api_key_env` variable, puts the node dir first on PATH, sets
  the TMP variables and the no-telemetry variables.
- `ass_color_to_css`: white, yellow, and alpha `80` gives 0.5.
- `build_composition` with en and hi words, including `<`, `&`, `"` and `</script>` in the text:
  - the text is escaped in the HTML and absent from the `<script>`;
  - there is no `http` in the file;
  - the root attributes (`data-duration` = cut duration, width/height/fps) are right;
  - group windows do not overlap;
  - group count = ceil(n / words_per_line);
  - `top` comes from `position_pct`, or `bottom` from `margin_v` when it is null;
  - the font file is copied (FONTS_DIR is monkeypatched to a tmp dir holding a dummy `NotoSansDevanagari-Bold.ttf`);
  - gsap is copied;
  - pop and karaoke differ;
  - the output is deterministic (byte-identical twice);
  - there is no `visibility`/`display` on `#g*` in the script and no `height: 0`.
- `render_overlay` with `FakeHFRunner`, which emits `@hf-progress` lines and writes an alpha overlay via
  `make_alpha_overlay`:
  - progress is monotonic and below 1;
  - args, cwd and env are as in section 3.3.
  - Error cases:
    - rc 1 gives a `HyperFramesError` holding the last lines;
    - a timeout gives `HyperFramesTimeout`;
    - `on_progress` raising `KeyboardInterrupt` propagates;
    - the `.tmp` and `out` dirs are removed in every case.
- `_popen_runner` timeout kill: a real child `python -c "import time; time.sleep(60)"` with timeout 1 s
  is killed (tree kill on POSIX; the test is skipped on Windows).
- `verify_overlay`:
  - an alpha webm passes;
  - an opaque webm (`yuv420p`, no alpha) fails;
  - a wrong size fails;
  - a wrong duration fails.
- `make_overlay` cache: the second call with the same inputs does not call the runner; a changed style,
  word or format calls it again.
- `tests/fakes.py` (additive):
  - `make_alpha_overlay(dst, seconds, width=1080, height=1920, fmt="webm")` uses ffmpeg
    `color=c=white@0.0:s=WxH:r=25:d=S,format=yuva420p,drawbox=...:color=yellow@1:t=fill` and `-c:v libvpx-vp9
    -pix_fmt yuva420p -auto-alt-ref 0` (or `prores_ks -pix_fmt yuva444p10le`). I verified this recipe: it
    writes the tag `alpha_mode=1` in lower case, while HyperFrames writes `ALPHA_MODE=1`, which is why the
    check ignores case;
  - `FakeHFRunner(rc=0, lines=[...], write=True, delay=0)` records calls.
- **Real render**, marked `@pytest.mark.hyperframes` and `@pytest.mark.slow`, and skipped unless node 22
  and the CLI are found: a 2 s, one-word composition rendered to webm, then `verify_overlay`, then a
  composite over `fakes.make_video`, then a check of the alpha pixel stats. Register the `hyperframes`
  marker in pyproject.

**integration, `tests/test_step3_integration.py`:**
- `render_final(overlay=webm)`: output size and duration equal the cut's. The pixels in the overlay's box
  differ from the cut's, and the pixels outside are unchanged (within tolerance). The audio stream is
  identical to the `overlay=None` render: same `build_audio_graph` string, and an equal loudness
  measurement within 0.1 LU.
- `ass` + `overlay` raises `RenderError`.
- The libvpx flag comes before the webm input only (assert on the captured `ffmpeg.run` args).
- `render_clip`:
  - engine ass means `make_overlay` is not called;
  - hyperframes OK gives `engine_used="hyperframes"` and no warning;
  - `make_overlay` raising `HyperFramesUnavailable`, `HyperFramesError` or `ValueError` gives an ASS
    render with the warning text from 3.4;
  - `KeyboardInterrupt` propagates.
- Pipeline with fakes:
  - `run_job` with engine hyperframes (fake `make_overlay`) gives job done, `render.json` stamped and the
    warning `None`;
  - with `make_overlay` failing, the job is still done, `ClipLangState.warning` is set, the log has a
    `WARNING` line and the stage message has "ASS fallback";
  - resume after switching engine ass to hyperframes re-renders **only** render (the voice and captions
    stage calls are counted: 0);
  - resume with an unchanged engine skips;
  - a legacy final without render.json plus the default engine is not stale;
  - a fallback stamp is stale;
  - `rerender` works with both engines;
  - `probe_env` is memoised (called once for 2 clips x 2 langs).
- Doctor: `check_hyperframes` with faked runs gives the rows of section 6 for node v16, node v22 with no
  CLI, everything ok, and a missing vp9 decoder. The doctor exit code is unaffected.
- Settings: defaults, old settings.json without the new keys loads, `settings set
  captions.engine=hyperframes` works, `hyperframes.fps=null` works, and the validation bounds hold.

**web_docs, `tests/test_web_step3.py`:** as in section 7.

**Run the tests ONLY like this** (/tmp is 98 % full):
```bash
mkdir -p ~/.cache/ac-step3/bt-<role>
TMPDIR=~/.cache/ac-step3 uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-step3/bt-<role>
```
The step-2 total was 754 passed. After step 3 it must be 754 or more, all passing.

## 10. Lead acceptance (sign-off checklist)
1. The full suite is green, and the `-m "hyperframes"` real-render test passes on this machine.
2. A real end-to-end run on a **copy** of the sample job (`cp -a` to a new id, voice.json paths
   rewritten, the original untouched; md5 checked before and after) with `captions.engine=hyperframes`:
   - resume re-renders the 4 clip-langs with no Voicebox or LLM calls;
   - each render takes under 3 min;
   - frames at 3 points per clip are looked at: captions at 72 %, words pop, Devanagari conjuncts
     correct, no box or black background;
   - the audio LUFS and peak match the ASS render within 0.2 LU.
3. Fallback is real: set `hyperframes.package_dir` to a bogus path and rerender one clip. The result is
   done, with the warning visible in the UI and the log.
4. A Web UI rerender works with both engines, and Doctor shows the new rows.
5. The lead decides the default engine and records it in README, the TEAM_LOG and this contract.
