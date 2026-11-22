# Auto Clipper - Team Log

Append-only team channel. Add notes under your role heading: what you did, decisions, anything a
teammate must know. Read docs/CONTRACT.md first - it is the source of truth for names and paths.

## Team Lead

### Plan (Step 1 = CLI pipeline end to end)
ingest -> transcribe -> moments -> cut, then per clip per language: script -> voice -> captions -> render.
Job data under data/jobs/<job_id>/ (layout table in CONTRACT.md section 1). Later steps (FastAPI UI +
one thread worker, HyperFrames captions, auto-publish) are NOT in scope; keep job.json / words.json /
script.json shapes stable so they can plug in.

### Assignments (files must not overlap)
- **media**: autoclipper/ffmpeg.py, autoclipper/stages/ingest.py, autoclipper/stages/cut.py,
  tests/test_ffmpeg.py, tests/test_ingest_cut.py
- **ai**: autoclipper/llm.py, autoclipper/stages/transcribe.py, autoclipper/stages/moments.py,
  autoclipper/stages/script.py, tests/test_llm_moments_script.py
- **voice_captions**: autoclipper/voicebox.py, autoclipper/stages/voice.py, autoclipper/stages/captions.py,
  autoclipper/stages/render.py, tests/test_voicebox.py, tests/test_voice_captions_render.py
- **orchestration**: autoclipper/pipeline.py, autoclipper/cli.py, README.md, tests/test_pipeline_e2e.py,
  tests/conftest.py (autoclipper/__init__.py and stages/__init__.py already created by lead - keep them tiny)
- **lead-owned** (ask before editing): docs/CONTRACT.md, autoclipper/interfaces.py, tests/fakes.py,
  tests/test_fakes_smoke.py; architect files config.py / models.py / jobstore.py (additive changes only).

### Done
- `uv sync` failed (hatchling could not find a package; `autoclipper/__init__.py` was missing). Fixed by
  creating the package files and adding `[tool.hatch.build.targets.wheel] packages = ["autoclipper"]`.
  Venv OK: anthropic 2.12.0, faster-whisper 1.2.1, yt-dlp 2026.10.26, httpx 0.28.1, pytest 9.2.0.
- Verified the Anthropic call shape offline (mock transport, no real API): `output_config` works on
  both `messages.create` and `beta.messages.create`; `betas` + `fallbacks` exist ONLY on
  `beta.messages.create`. Exact shape in CONTRACT.md section 3.
- Wrote docs/CONTRACT.md, autoclipper/interfaces.py (Protocols LLM / VoiceboxAPI / Transcriber +
  ProgressFn / ShortenFn / LogFn), tests/fakes.py (FakeLLM, FakeVoicebox, FakeTranscriber,
  fake_transcript, make_video, make_tone). Smoke test passes: `uv run pytest -q`.

### Decisions everyone must know
- Clip indexes are 0-based = position in job.clips; dirs clips/00, clips/01.
- cut.mp4 always has an audio track (silent if the source had none), so render can always mix.
- Voice stage returns the narration actually spoken; if it was shortened, the pipeline writes it back
  to script.json, so captions/rerender match the audio. Rerender regenerates voice only when
  sha256(script.narration.strip()) != voice.json narration_hash.
- Hindi always uses engine "chatterbox", whatever settings say.
- Machine: CPU only (no CUDA) -> whisper cpu/int8. ffmpeg 7.1.1 with libass. Noto Sans + Noto Sans
  Devanagari are system fonts; assets/fonts is empty -> pass fontsdir only when it has fonts.
  node is v16.20.2 -> `doctor` warns (HyperFrames needs 22+ later).
- Anthropic SDK 2.x uses `httpx2` internally; voicebox.py uses plain `httpx` (separate package) - do not mix their objects.
- No git commits.

### Review checklist (lead will check each builder against this)
1. Signatures match CONTRACT.md. 2. No network in tests. 3. utf-8 + ensure_ascii=False everywhere.
4. No shell=True, pathlib only. 5. Errors are clear, actionable, typed. 6. `uv run pytest -q` green.

## Builder (ai)

### Done
- `autoclipper/llm.py`: `LLMError` / `LLMRefusal` / `LLMTruncated`, `AnthropicLLM(settings, client=None)`.
  Uses the call shape from CONTRACT section 3. `beta.messages.create(betas=["server-side-fallback-2026-07-01"],
  fallbacks="default")` when `use_fallbacks` is on, otherwise `messages.create`. No thinking, temperature or
  prefill. `stop_reason` is checked first (refusal includes stop_details category/explanation when present).
  Only `type=="text"` blocks are joined. SDK errors map to LLMError (auth, 403, 404, 429, 400, other
  status, timeout, connection). Strict schemas: `MOMENTS_SCHEMA`, `SCRIPT_SCHEMA`, `SHORTEN_SCHEMA`.
  A missing key raises `LLMError("<api_key_env> is not set (put it in .env)")`.
- `stages/transcribe.py`: `TranscribeError`, `resolve_device`, `WhisperTranscriber` (loads lazily on first
  call; word_timestamps + vad_filter), `run_transcribe` (idempotent), `transcribe_voice`. Extras:
  `load_transcript`, `save_transcript`.
- `stages/moments.py`: `format_transcript`, `validate_moments`, `run_moments`. Extras: `MomentsError`,
  `save_moments`, `load_moments`. `run_moments` raises MomentsError if the transcript is empty or no moment
  survives validation (it writes no file in that case, so a rerun asks again).
- `stages/script.py`: everything in the contract. Extras: `ScriptError` (empty narration, bad or missing
  script.json), `save_script`.
- Tests: `tests/test_llm_moments_script.py` has 60 tests, all passing. Fake client plus an httpx2.MockTransport
  wire test; nothing touches the network or loads a real Whisper model. Full `uv run pytest -q`: 79 passed.

### Decisions / notes for teammates
- Snapping: start/end snap to the nearest segment start/end only within 5 s (`moments.SNAP_TOLERANCE`).
  Beyond that the LLM's time is kept. Short moments extend to the first segment end at or after min;
  if that runs past the video, the end anchors to the video end and the start moves back. Long moments
  shrink to the last segment end at or before max. If the video is shorter than min_seconds, the whole
  video becomes one moment. Moments that only touch (end == start) do not count as overlapping.
- Hashtag cleanup filters by Unicode category (L/M/N/_), not regex `\w`. Python's `\w` drops Devanagari
  vowel signs (it turns "#हिंदी" into "#हद"). voice_captions: `captions.split_words` / `align_words`
  normalization has the same pitfall. Do not strip punctuation with `[^\w]` on Hindi text.
- `transcribe_voice` returns `[]` when Whisper hears nothing. captions should fall back to `distribute_words`
  (the contract already says this).
- orchestration: `write_script` always overwrites `out_json`, so the pipeline must check that script.json
  exists before calling it (CONTRACT idempotency rule) so that user edits survive.
- Open: the prompt-quality of Hindi output and the real refusal/fallback behaviour are only verified with
  fakes. A real smoke run needs ANTHROPIC_API_KEY (run it outside tests).

## Builder: voice_captions

### Done
- `autoclipper/voicebox.py`: VoiceboxClient (plain httpx) matches CONTRACT 4. It takes `transport=`, `sleep=` and `clock=`, and also works as a context manager with `close()`. It reads the id from `id` or `generation_id`. Status is compared case-insensitively, and `failed`/`error` raise VoiceboxError using the server's `error`, `message` or `detail` field. Timeouts and HTTP >= 400 also raise VoiceboxError (HTTP errors include FastAPI `detail`). ConnectError gives exactly "Voicebox is not reachable at <url> - is it running?". Server `progress` may be on a 0..1 or 0..100 scale.
- `autoclipper/stages/voice.py`: VoiceResult, narration_hash, engine_for (hi -> chatterbox) and make_voice/load_voice_result follow CONTRACT. `VoiceResult.path` = absolute path of voice.wav. `raw_duration` = length of the take that was used (after any regeneration). Additionally there is `VoiceError` for empty narration and clip_duration <= 0. An empty profile_id raises VoiceboxError naming `autoclipper profiles` and `voicebox.<english|hindi>.profile_id`. Shorten is called at most once. If the shorten callback returns the same or empty text, we skip regeneration and go straight to atempo max + trim with a 0.3 s fade. Exceptions from shorten propagate. `load_voice_result` returns None when voice.json is missing or invalid, or when voice.wav is missing.
- `autoclipper/stages/captions.py`: split/distribute/align/build_ass/make_captions follow CONTRACT. Alignment uses difflib over tokens normalised with NFC + casefold and keeps only Unicode L/M/N, so Devanagari matras survive. The 30% threshold applies. Unmatched runs are spread by character count between matched neighbours. Style colours are &HAABBGGRR; inline overrides use 6-digit `{\c&HBBGGRR&}`. Escaping: `{`->`\{`, `}`->`\}`, `\`->`\`+U+2060 (word joiner), so `\N` never fires. Each word event lasts until the next word in its line starts. The last word holds until the next line starts if the gap is <= 0.5 s. Additionally there is `CaptionsError` for a missing voice file. PlayRes comes from `settings.clips.width/height` (1080x1920 by default).
- `autoclipper/stages/render.py`: resolve_fonts_dir (.ttf/.otf/.ttc under assets/fonts, else None), render_final -> `ffmpeg.mix_burn(..., duration=ffmpeg.probe(cut).duration)`, write_metadata. Additionally there is `RenderError` for a missing cut/voice/ass.
- Tests: tests/test_voicebox.py (httpx.MockTransport, fake clock) and tests/test_voice_captions_render.py (FakeVoicebox/FakeTranscriber plus the real media-builder ffmpeg.py). That includes an end-to-end run: hi voice -> shorten -> captions -> final.mp4 in a path with a space. I checked a frame by eye: Noto Sans Devanagari renders and the active word is yellow. 45 passed.

### For orchestration
- Call `make_voice(..., shorten=script.make_shorten_fn(llm, lang))`. If `result.shortened`, write `result.narration` back to script.json. Captions should use `result.narration` and `Path(result.path)`.
- make_captions needs `settings: Settings` (full settings, not only CaptionSettings).

## Builder (media)

### Done
- Wrote autoclipper/ffmpeg.py, autoclipper/stages/ingest.py, autoclipper/stages/cut.py, plus
  tests/test_ffmpeg.py and tests/test_ingest_cut.py. `uv run pytest tests/test_ffmpeg.py tests/test_ingest_cut.py -q`
  gives 70 passed in about 8s; the full suite gives 176 passed. All media is synthetic (lavfi), and yt-dlp is monkeypatched.
- Everything in CONTRACT section 2 is implemented with the signatures as written.

### Things teammates should know
- `run(args, *, desc, on_progress=None, total=None)`: I added two optional arguments. When you pass both
  `on_progress` and `total` (output seconds), progress comes from ffmpeg `-progress pipe:1`
  and is capped at 0.99 until the run finishes, then reported as 1.0. `run` also passes `-nostdin` and stdin=DEVNULL so ffmpeg
  never blocks waiting on the terminal.
- Every ffmpeg output is written to `<stem>.part<ext>` and then renamed into place with os.replace. A crash therefore
  never leaves a non-empty half-written cut.mp4, audio.wav or final.mp4 that the pipeline's "exists and size > 0" check would skip.
- `find_source` counts only files whose stem is exactly `source`. That ignores yt-dlp leftovers such as
  `source.f137.mp4`, `source.mp4.part` and `source.temp.mp4`.
- `ingest` rejects files that ffprobe cannot read and files with no video stream (IngestError).
  `extract_audio_for_asr` raises IngestError("... has no audio track ...") when the source is silent.
- `cut_vertical` clamps `end` to the source duration and raises FFmpegError("empty range") when end <= start.
  `cut_clip` always re-cuts, so skipping an existing output is the pipeline's job.
- `mix_burn` (for voice_captions/render): it adds anullsrc itself when the video has no audio. It checks
  whether fonts_dir really holds .ttf/.otf/.ttc/.otc files before adding `fontsdir`, so render can pass FONTS_DIR unconditionally.
  The voice is resampled to the 48 kHz stereo aac output automatically.
- `escape_filter_path(path)` is an extra public helper that returns the quoted value. Windows paths (PureWindowsPath, or any
  str on nt) become `'C\:/...'`. Spaces and apostrophes work, and a real libass burn test runs from a dir named "my clip's dir".
- `probe` ignores cover-art video streams (attached_pic), so an mp3 or m4a with artwork has has_video=False.
- Extra helper: `synth_video(dst, seconds, width, height, audio)`.

### Open issues / requests
- None blocking. yt-dlp progress stays monotonic across the separate video and audio downloads, but it is
  approximate: the bar does not reset for the second file, it holds until that file passes the first.

## Builder: orchestration

### Done
- `autoclipper/pipeline.py`: `Deps`, `default_deps`, `effective_settings`, `parse_langs`, `run_job`,
  `rerender`, `PipelineError`, as in CONTRACT.md section 5. Extras: `run_job(..., debug=False)`,
  `rerender(..., debug=False)` and `output_exists(path)`.
  - Deps are lazy (`_LazyDeps`). When `deps=None`, each real client is built only when a stage needs it.
    A fully done job can be resumed with no API key and no Voicebox running (a test checks this).
  - Idempotency: a stage is skipped when its output exists and is not empty. Once any stage in a
    clip-language is regenerated, every later stage reruns. Voice counts as current only when
    `voice.json.narration_hash == narration_hash(script.narration)`, so editing the narration and
    running `run --job` again also regenerates the voice. `metadata.json` is rewritten on every run
    (cheap, and picks up title edits).
  - Skip marks "skipped" only when the stage was never "done"; a stage that was "done" stays "done".
  - Per clip-language exceptions: that stage is marked "failed", `ClipLangState.error = "<stage>: <msg>"`,
    and the other clips continue. A failed cut fails only that clip: each of its languages gets an
    error and the "script" stage marked failed. The "cut" job stage raises only if every cut fails.
  - Job status is "failed" only if a job-level stage fails (this raises PipelineError) or every
    clip-language failed; otherwise it is "done". Zero valid moments raises PipelineError.
  - Voice clip_duration = `ffmpeg.duration(cut.mp4)`, not moment.duration.
  - Writes the shortened narration back to `script.json` through `stages.script.save_script`.
  - Progress lines: `[job] stage NN/lang pct% msg`. Callbacks are throttled to one line per 10% and a
    job.json write every 5% or 1 s.
- `autoclipper/cli.py`: run/rerender/status/jobs/profiles/settings show|set/doctor. Exit codes: 0 ok,
  1 error (`error: msg`), 2 usage. `--debug` works before or after the subcommand.
  `settings set` rejects unknown keys and whole groups and re-validates (also min<=max).
  `languages=en,hi` works as a comma list. `doctor` checks python, data dir, ffmpeg/ffprobe, the
  filters subtitles/ass/amix/atempo, fonts (assets/fonts or fc-list), Voicebox health + profile ids
  for the configured languages, the API key (never printed), and node (warns below 22).
  The check functions are injectable for tests.
- `tests/conftest.py`: an autouse fixture sets `AUTOCLIPPER_DATA_DIR` to `tmp_path/data` for EVERY test.
- `tests/test_pipeline_e2e.py` (46 tests): a 40 s synthetic video, 2 clips x en/hi, through the real
  ffmpeg stages and fakes. Checks: final.mp4 is 1080x1920 with audio and the cut's duration,
  metadata shape, all job and clip stages done, engines (hi=chatterbox), idempotent rerun with real
  client construction forbidden, rerender on a title edit (no voice regen) and on a narration edit
  (voice regenerated, hash/words updated), isolated hi failure, all-failed -> job failed, missing
  source, bad overrides, CLI run/status/jobs/settings/usage errors, doctor checks.
  `uv run pytest -q` -> 222 passed.
- README.md written: setup for Linux and Windows, CLI, data layout, settings, roadmap.

### Notes for teammates
- Real `doctor` on this machine: ffmpeg, filters and fonts OK; Voicebox not running (FAIL); profile
  ids not set (FAIL); node v16 WARN.
- No requests for other builders' files.

## Reviewer (robustness)

Read-only review of autoclipper/ and tests/ (222 tests green). Reproductions are in /tmp/rv_rob/*.py and all run against fakes, with no network. Findings: **R1 (high)**: resume can ship a stale final.mp4. If voice or captions are regenerated and a later stage then fails or is interrupted, the next `run --job` skips captions and render because the old captions.ass and final.mp4 still exist and `regenerated` starts out False. The job reports "done" while final.mp4 still has the old narration (r1_stale.py). Fix: compare each output's mtime with its inputs, or delete the downstream outputs whenever a stage is regenerated. **R2 (medium)**: in ffmpeg.run's progress path, an exception raised from on_progress (job.json write error, KeyboardInterrupt, a later UI cancel) leaves the ffmpeg child running with no reader, so it is orphaned (r2_leak.py). Fix: try/finally that kills and waits for the child. **R3 (medium)**: make_voice accepts a zero-length take from Voicebox. A 78-byte voice.wav passes output_exists, and the clip renders with no narration while the job says "done" (r3_empty_voice.py). Fix: raise VoiceError when the duration is below about 0.2 s. **R4 (medium)**: _populate_clips never rebuilds job.clips. After moments.json is deleted, or `--clips N` changes on resume, the new moments are written but ignored, so moments.json and job.json no longer agree (r4_moments.py). **R5 (low)**: Ctrl-C leaves job.status and the stage status at "running" in job.json for good. Tests do not exercise align_words with real recognized words end to end, because FakeTranscriber returns no voice words, so only the distribute fallback is covered in e2e.

## Reviewer (correctness)

I reviewed all of autoclipper/ and tests/ against CONTRACT.md. I did not edit any source files. The full suite gives 222 passed. I checked behaviour with scratch scripts and fakes; nothing called the network. Several things work as intended: modules wire together correctly in pipeline.py/cli.py; filtergraph path escaping holds even for a dir named `a,b;c[d]:e'f %g` (real libass burn); ASS `{ } \` escaping renders literally on libass 0.17.3; Devanagari survives `_norm`, hashtags and the UTF-8 writes; atempo chains are correct. I found these defects:
- C1 (high, pipeline.py:470/480): staleness is tracked only inside one run through the `regenerated` flag. Reproduced: edit the narration, then `run --job`. The voice is regenerated, then captions fail (Whisper error). On the next `run --job`, voice counts as "current", and captions and render are SKIPPED. The job reports done, but final.mp4 and words.json still contain the OLD narration. The same thing happens when render fails, for example on Windows when final.mp4 is open in a player. Fix: before regenerating an upstream stage, delete the downstream outputs (captions.ass, words.json, final.mp4), or skip only when the output mtime is at least the mtime of its inputs.
- C2 (medium, pipeline.py:297): `_populate_clips` keeps the old job.clips when moments.json has been re-picked. MomentsError itself says "delete it to pick moments again". Reproduced: moments.json held start=15 while job.clips[0] kept start=0. The LLM was paid to re-pick, the new picks were ignored, and the two files now disagree.
- C3 (medium, captions.py:141): `WrapStyle: 2` turns wrapping off. A 3-word English line such as "unbelievable transformation everywhere" at size 78 runs from x=0 to x=1079 and is clipped at both edges in the burned frame (checked visually).
- C4 (low, cli.py:235): `settings set voicebox.english.profile_id=12345` (any numeric-looking string value) is parsed as a JSON number and rejected with "Input should be a valid string". Workaround: quote it as `'"12345"'`.
- C5 (low, pipeline.py:520): Ctrl+C (KeyboardInterrupt) is not caught, so job.json stays `status=running`, with stages left `running`, until someone resumes the job. A later web UI would show the job as running forever.

## Team Lead (triage)

I checked every finding against the code. All of them are real defects. C/R pairs that report the same bug are merged into one fix, and the C id is canonical.
- ACCEPT C1 (high; covers R1): `_process_clip_lang` tracks staleness only through the in-run `regenerated` flag. Fix: persist the dependency. Before `_run_voice` regenerates, unlink words.json, captions.ass and final.mp4 in that clip-lang dir. Before `_run_captions` regenerates, unlink final.mp4. Do the same in `rerender`. Add the R1 e2e test: edit the narration, make render (or captions) fail, resume, and assert that final.mp4 was rebuilt and the captions/words match the new narration.
- ACCEPT C2 (medium; covers R4): `_populate_clips` ignores re-picked moments. Fix: `_job_stage_moments` returns whether it regenerated. When it did and job.clips is non-empty, rebuild job.clips from the new moments. Any clips/NN dir whose moment changed, or whose index is no longer present, is renamed aside to `clips/NN.stale-<n>` (renamed, NOT deleted). When moments.json is reused and the --clips/--min/--max overrides differ from what produced it, log a warning that the overrides are ignored for an existing job. Add a test.
- ACCEPT C3 (medium): change `WrapStyle: 2` to `WrapStyle: 0` in captions.build_ass so libass wraps inside MarginL/R. Add a test that the header carries WrapStyle 0. A real-burn check with "unbelievable transformation everywhere" should show no pixels in the outer 20 px columns.
- ACCEPT C4 (low): in `apply_setting`, when the JSON-parsed value is not a str, the raw value is not JSON-quoted, and the current value at that key is a str (or None with a str field), keep the raw string. Test `voicebox.english.profile_id=12345` -> "12345".
- ACCEPT C5 (low; covers R5): add `except BaseException` handling (re-raise) to run_job (both the job-level try and the clip loop), `_process_clip_lang` and `rerender`. It marks the running stage failed with "interrupted", sets job.status="failed" and job.error="interrupted", saves, and re-raises. Test with a fake that raises KeyboardInterrupt.
- ACCEPT R2 (medium): in ffmpeg.run's Popen path, wrap the stdout loop and wait in try/except BaseException. On error: popen.kill(), popen.wait(), close stdout/stderr, join the reader, re-raise. Test: an on_progress that raises -> the child process is gone (popen.poll() is not None).
- ACCEPT R3 (medium): make_voice raises VoiceError("Voicebox returned empty audio (Xs) for <lang>; check the profile/engine") when the duration after generate() is < 0.2 s. Check again after the regeneration that follows shortening. Test with a 0-sample WAV fake.
- ACCEPT B1 (doc): record the approved additive APIs in CONTRACT.md: ffmpeg.run(on_progress, total), escape_filter_path, synth_video; MomentsError/ScriptError/VoiceError/CaptionsError/RenderError; moments.SNAP_TOLERANCE=5.0 s (approved); run_job/rerender debug=, parse_langs, output_exists.
- ACCEPT B2 (tests): add an e2e/captions test where FakeTranscriber returns real recognized words, so align_words (not only the distribute fallback) is exercised through make_captions.
- Not actioned (verified OK or informational): the Devanagari `\w` concern does not apply, because captions.split_words uses str.split() and `_norm` filters on Unicode category ('#हिंदी' survives). The write_script overwrite is already guarded by the pipeline's skip on script.json. Voicebox live behaviour, Whisper model load and Windows paths remain untested and need manual smoke runs. The README Voicebox link still needs a human to confirm it.

## Fixer

All accepted fixes applied. `uv run pytest -q` -> 245 passed (222 before + 23 new in `tests/test_fixes_regression.py`). No network; fakes only. Nothing committed.
- C1/R1 (pipeline.py): `_unlink_outputs` deletes words.json, captions.ass, final.mp4 at the start of `_run_voice`, and final.mp4 at the start of `_run_captions`. Both run_job and rerender use these, so staleness survives a failed or interrupted run. The in-run `regenerated` flag is still there. Test: edit narration, run with render failing (and a captions-failing variant), then run with working fakes. final.mp4 is rebuilt, voice is not regenerated, and words.json/captions.ass match the new narration. I checked that the test fails without the unlinks.
- C2/R4 (pipeline.py): `_job_stage_moments` returns `(moments, fresh)`. `_populate_clips(..., fresh=, log=)` calls `_rebuild_clips` when fresh and job.clips is non-empty. Clips whose moment is unchanged keep their state and dir. Changed or dropped indexes are renamed to `clips/NN.stale-<n>` (first free n, never deleted) and get a fresh ClipState, so the cut stage re-cuts them. When moments.json is reused, `_warn_ignored_overrides` logs `[job] warning: --clips N (moments.json has M) ... ignored for an existing job ...; delete moments.json to pick moments again`. The job does not store its settings, so the comparison uses what moments.json implies: count vs len(moments), and min/max vs moment durations with 0.5 s tolerance. It warns only for overrides that were actually passed.
- C3 (captions.py): `WrapStyle: 0`. I changed the old assertion in test_voice_captions_render.py from "WrapStyle: 2" to "WrapStyle: 0", because that test encoded the bug. A new real-libass burn test renders "unbelievable transformation everywhere" on black 1080x1920 and finds no bright pixels in the outer 20 px. It fails with WrapStyle 2.
- C4 (cli.py): `apply_setting` keeps the raw text when the JSON value is not a str and not None, the raw value is unquoted, and the current value is a str or the field is annotated `str`/`str | None` (`_is_str_field`). `null` still gives None, and int/float/bool fields parse as before.
- C5/R5 (pipeline.py): `except BaseException` branches in `_process_clip_lang` (clip error "<stage>: interrupted", re-raise), in the job-level try, around the clip loop, and in rerender. `_mark_job_interrupted` sweeps every stage still marked "running" to failed/"interrupted", sets job.status=failed and job.error="interrupted", saves and logs a resume hint. Tests cover a clip stage (voice), a job stage (transcribe), rerender, and a resume afterwards.
- R2 (ffmpeg.py): Popen progress path. On BaseException it kills and waits the child (`_kill`), joins the stderr reader, closes stdout/stderr, and re-raises. Test: cut_vertical with on_progress raising on the 2nd call. Afterwards the child has exited (rc -9), the pipes are closed, and no .part or cut.mp4 is left.
- R3 (voice.py): `MIN_VOICE_SECONDS = 0.2`. The check runs after the first generate and after the regeneration that follows shortening. It deletes the raw/norm/work temp files and writes no voice.json. Tests: a 0-sample WAV fake; empty audio after shortening; in the pipeline the clip is failed at voice and the next run retries the voice and finishes.
- B1: CONTRACT.md has a new section 5a listing the approved additive APIs and the behaviours above.
- B2: make_captions Hindi test with recognized Devanagari timings: one word unmatched, and "है।" carries a danda. Matched words take the recognized timings exactly, the unmatched word interpolates into the gap, and timings are monotonic. A pipeline e2e test with an EchoTranscriber (en+hi) asserts that words.json follows the recognized timings.
- Note for lead: the job-level `rep.interrupted` only runs when the stage already exists in job.stages. That is always true for the 4 job stages.

## Team Lead (final sign-off)

APPROVED. `uv run pytest -q` gives 245 passed (72 s), with no network and fakes only. `uv run autoclipper --help` lists run/rerender/status/jobs/profiles/settings/doctor. `uv run autoclipper doctor` reports python, data dir, ffmpeg/ffprobe, the filters, fonts (Noto Sans + Devanagari) and the API key OK. It shows FAIL for Voicebox not running and for the en/hi profile ids not set, and WARN for node v16. All of these are environment items for the user, not code defects.
- I checked each accepted fix in the source. C1: `_unlink_outputs` in `_run_voice`/`_run_captions`, used by run_job and rerender. C2: `_job_stage_moments` returns (moments, fresh), and `_rebuild_clips` renames dirs to `NN.stale-<n>`, with `_warn_ignored_overrides`. C3: `WrapStyle: 0`. C4: `_is_str_field` in cli.apply_setting. C5: BaseException branches plus `_mark_job_interrupted`/`_sweep_running`. R2: ffmpeg.run kill/wait/join/close on BaseException. R3: `MIN_VOICE_SECONDS=0.2` checked after both generates. B1: CONTRACT.md section 5a. B2: aligned-word tests for en and hi. There are 15 regression tests in tests/test_fixes_regression.py.
- I also ran manual CLI checks in a temp data dir. `settings set voicebox.english.profile_id=12345` stores "12345", a bad int gives exit 1 with a clean message, and an unknown job gives exit 1.
- I made no code changes. Nothing is committed.
- Still untested and needs the user: a live Voicebox (the endpoints in the README and the profile ids), a real Claude API call, the first faster-whisper model download/load, Windows paths, and a real long video. Upgrade Node to 22+ before step 3 (HyperFrames).

## Ops (environment)

### Done
- **Node.js 22 LTS** for HyperFrames (step 3): v22.23.5 linux-x64 from https://nodejs.org/dist/v22.23.5/
  (tarball checked against the official SHASUMS256.txt: OK). Installed at `/home/ptl-admin/tools/node22/`
  (`bin/node`, `bin/npm` 10.9.9, `bin/npx` 10.9.9). NOT on PATH; the system default stays nvm node v16.20.2
  for the user's other projects. Use it with a PATH prefix only:
  `PATH=/home/ptl-admin/tools/node22/bin:$PATH npx ...` (verified: `npx -y semver@7.6.3` runs under v22).
  Code that shells out to node/npx (step 3, and `doctor`) should take the node bin dir from a setting
  (suggest `hyperframes.node_bin_dir`, default `/home/ptl-admin/tools/node22/bin` on this machine) and pass
  `env={**os.environ, "PATH": f"{node_bin}{os.pathsep}{os.environ['PATH']}"}` to subprocess. `doctor` will keep
  warning about v16 until it checks that dir. Original tarball + SHASUMS256.txt kept in
  /home/ptl-admin/tools/downloads/node22/.
- **Real test video** (public domain): `data/test_media/reagan_challenger_speech_1986.ogv` (data/ is git-ignored),
  provenance in `data/test_media/SOURCE.txt`. President Reagan's Challenger address, Oval Office, 1986-01-28,
  from Wikimedia Commons (PD US Government; SHA1 matches Commons). 284.99 s, theora 320x240 25 fps + vorbis
  44.1 kHz stereo. Clear English speech from ~10.9 s to ~258.6 s (silencedetect), frame checked by eye.
  Low-res 4:3 source: good for a pipeline smoke run, output will look soft after the 9:16 crop/upscale.
  Ingest takes .ogv via ffmpeg. Run it with e.g. `uv run autoclipper run data/test_media/reagan_challenger_speech_1986.ogv`
  (needs the API key + Voicebox; not run by ops).
- Not run: ASR on the file (no English Whisper model cached; first `transcribe` will download it).

## Specialist reviewer (windows)

Read-only review of Windows compatibility. Scratch repros are in /tmp/wrev/*.py. No code changes, nothing committed.
- **Verified OK:** `escape_filter_path`/`ass_filter`. I burned subtitles for real (ffmpeg 7.1.1) with the .ass file AND a populated fontsdir under directory names containing a space, `'`, `:`, `,`, `[]`, `;`, `\`, `%`, `=` and Devanagari, and all 10 render. The PureWindowsPath output (`'C\:/...'`) is correct. Every subprocess call uses a list, no shell, and utf-8 with errors=replace. main() sets stdout/stderr errors=backslashreplace, so a cp1252 pipe or redirect never crashes on Devanagari. All text I/O passes encoding="utf-8".
- **WINDOWS-1 (medium):** `doctor` always FAILs fonts after the README's "Install for all users" option on Windows. Windows has no fc-list, so check_fonts only looks in assets/fonts, and `doctor` exits 1 (repro: check_fonts with which(fc-list)=None). Fix: on Windows, also scan %WINDIR%\Fonts and %LOCALAPPDATA%\Microsoft\Windows\Fonts by file name, or say in the README to use assets/fonts on Windows.
- **WINDOWS-2 (medium):** JobStore.save calls os.replace onto job.json with no retry. On Windows, MoveFileExW fails with WinError 5/32 while another process has job.json open (a concurrent `autoclipper status`/`jobs`, the planned UI poller, Defender, OneDrive). save runs from the progress callback, so the PermissionError kills the running ffmpeg and fails the stage (/tmp/wrev/lock.py: the cut is aborted and no cut.mp4 is written). Fix: retry os.replace for about 1 s on PermissionError, and don't let a progress-save failure abort a stage.
- **WINDOWS-3 (medium):** _AtomicOutput: if final.mp4 is open in a Windows player, `rerender`/`run --job` does the whole encode and only then fails on os.replace. It prints a raw "[WinError 5] Access is denied" and leaves final.part.mp4 behind, because __exit__ does not clean up when the replace fails (/tmp/wrev/part.py). Fix: try/except around the replace, unlink tmp, and raise an FFmpegError saying "close final.mp4 in your player". Optionally check that dst can be opened before encoding.
- **WINDOWS-4 (low):** whisper.device=auto picks CUDA whenever ctranslate2 sees an NVIDIA driver. On Windows the cuBLAS/cuDNN DLLs are not pip-installed, so a gaming PC with no CUDA toolkit fails in transcribe ("cublas64_12.dll is not found"), with no CPU fallback and no README note. Fix: fall back to cpu on that load error, and document `settings set whisper.device=cpu`. (Not reproducible here because there is no Windows GPU.)
- **Untested (needs a real Windows box):** whether the gyan build's libass/fontconfig finds system-installed fonts when fontsdir is not passed (gyan ships no fonts.conf), and whether libass's fontsdir loader handles non-ASCII paths on Windows.

## Specialist Reviewer (hindi_captions)

Read-only review of the Hindi/captions/voice-fit/mix paths. No source edits; scratch repros in /tmp/rv_hc/*.py (fakes only, no network). Verified OK: ASS escaping of `{ } \` and `\N` renders literally (real libass burn), Latin/digits inside Hindi captions fall back cleanly from Noto Sans Devanagari Bold to Noto Sans Bold, Devanagari matras survive `_norm`, atempo/shorten math matches the contract. Findings:
- HINDI_CAPTIONS-1 (medium, captions.py align_words / voice.py trim path): when the voice is trimmed (`trimmed=True`), captions and words.json still contain the whole narration. Repro h1_trim.py: 22-word Hindi narration, 4 s clip, only ~10 words are spoken; the 12 unspoken words get crammed into 3.98-4.00 s (several zero-length events), and in the no-recognition fallback every word is spread over 4 s so captions run ~2x faster than speech.
- HINDI_CAPTIONS-2 (low/medium, captions.py `_norm`): Devanagari spelling variants that Whisper commonly emits (nukta ज़/ज, फ़/फ, क़/क; anusvara vs virama-nasal हिन्दी/हिंदी; chandrabindu/anusvara हँ/हं) never match. A script rich in them drops under the 30% threshold and the whole clip falls back to even distribution (h4_norm.py: "है" captioned at 11.3 s while it is spoken at 6.0 s).
- HINDI_CAPTIONS-3 (low, ffmpeg.mix_burn): no limiter/loudness control. With the allowed `audio.voice_volume=2.0` a -1 dBFS TTS take + original at 0.15 renders at +2.5 dBFS peak (clipping); mono voice is also upmixed at -3 dB so voice_volume=1.0 is not unity (h2_clip.py, h2b.py).

## Specialist Reviewer (pipeline)

Read-only review of pipeline.py, cli.py, jobstore.py and stage wiring. Baseline `uv run pytest -q` gives 246 passed. Reproductions are in /tmp/rv_pipe/*.py (fakes only, no network). Run each with `uv run python /tmp/rv_pipe/<name>.py` from the project root.
- PIPELINE-1 (high, jobstore.py:52): `save()` always uses the same `job.json.tmp`, and `_lock` only works inside one process. Two processes saving the same job (web worker plus CLI rerender/run --job) race. p_save_race.py: 2 x 2000 set_stage calls gave 584 and 1184 FileNotFoundError from os.replace. In the pipeline that error fails the clip or the job. p_torn.py: a reader saw an invalid job.json. Separately, two JobStore copies of one job overwrite each other's changes (lost update). Fix: use a unique tmp name per save (pid/thread/uuid), add a per-job file lock across processes, and reload-and-merge or route every write through one owner. On Windows, os.replace also fails while a reader holds job.json open. This was not verified here.
- PIPELINE-2 (medium, pipeline.py:412/563): when cut.mp4 is regenerated, the outputs after it are not invalidated. p_cut_stale.py: change clips.reframe to blur, delete cut.mp4, run --job. The clip is re-cut, but voice, captions and render are all skipped, so final.mp4 still holds the old crop. This contradicts the README ("every stage after it ... runs again"). Fix: when a clip is re-cut, unlink that clip's per-language voice/words/captions/final outputs (or force regenerated=True).
- PIPELINE-3 (medium, pipeline.py:177/486/515): `_unlink_outputs` deletes files but leaves job.json as it was. p_stale_state.py: during and after a failed voice regeneration, job.json still shows captions=done and render=done, and `final` points to the deleted final.mp4. A UI would show a broken link while the job runs (error is None until the failure). Fix: when outputs are unlinked, reset the downstream stage states to pending and set state.final=None.
- PIPELINE-4 (medium, pipeline.py:741): `rerender` changes a failed job to "done" whenever any clip-lang has no error. Pending clip-langs that never ran have no error either. p_rerender_status.py: interrupt the job, then rerender 0/en. The job becomes status=done, error=None, while 01/en and 01/hi are still pending and 00/hi failed. Fix: mark the job done only if every clip-lang for the job's languages has its render stage done.
- PIPELINE-5 (low, pipeline.py:703-711): rerender for a language the job never ran (rerender J 0 hi on an en-only job) saves a phantom `hi` ClipLangState with every stage pending before it raises "script.json not found". After that, `jobs` reports 1/2 rendered. Fix: validate script.json before setdefault/save.

## Specialist Reviewer (failures)

Read-only review of failure modes. All repros use fakes, MockTransport or local synthetic audio. Nothing touched the network, the real API or a real Voicebox. Scripts are in the session scratchpad under `rv/` and `fw/`.
- **FAILURES-1 (high)**: real transcription is broken on the locked env (py3.12, av 19.0.2 + faster-whisper 1.2.1). When `model.transcribe(str(path))` gets a path it calls `decode_audio`, which does `av.open(..., metadata_errors=...)` and gets a TypeError from PyAV 19. Reproduced through `WhisperTranscriber.transcribe` with no model download: `TranscribeError: ... unexpected keyword argument 'metadata_errors'`. As a result every real `run` fails at transcribe, and caption alignment fails the same way. Fix: read the 16 kHz s16 `audio.wav` / `voice.wav` ourselves (wave+numpy -> float32) and pass the ndarray, or pin `av<19`. Add a doctor check.
- **FAILURES-2 (medium)**: memory on long videos. faster-whisper builds the mel features for the whole (post-VAD) file in one go. Measured peak RSS was about 1.6 GB for 30 min and about 3.2 GB for 60 min, growing linearly. That means about 6.5 GB for 2 h and about 13 GB for 4 h, before the model and decoded audio are counted, on a 14 GB box. Fix: transcribe in windows of about 10-20 min, slicing the ndarray and offsetting the timestamps.
- **FAILURES-3 (medium)**: transcript.json and moments.json are written non-atomically, and the pipeline loads them without a recovery path. Under disk full (simulated with RLIMIT_FSIZE) a truncated 4 KB transcript.json is left behind. `output_exists` treats it as done, and every resume then fails with a raw pydantic "EOF while parsing" message and no "delete it" hint. Fix: tmp+os.replace for all JSON outputs, and on a parse error treat the file as missing (or use load_transcript/load_moments).
- **FAILURES-4 (medium)**: a yt-dlp download that fails `_check_has_video` (audio-only URL, corrupt file) raises IngestError but leaves `source.<ext>` in place. On resume, `find_source` reuses it without the check, spends whisper and the LLM moments call, and then fails "every clip failed to cut". Fix: delete or rename the file on a failed check, or re-check it in `ingest()` when it is reused.
- **FAILURES-5 (medium)**: when the voice is too long, an LLM error in `shorten` (refusal or rate limit) fails the clip at voice, and the documented speed-up+trim last resort is never used. Each resume pays Voicebox again and fails again (3 runs gave 3 generations and 3 failures). Fix: catch LLMError around shorten and fall through to tempo+trim. Optionally keep voice_raw.wav for reuse.
- **FAILURES-6 (medium)**: Voicebox timeout never cancels. Real Voicebox runs one serial queue and has `POST /generate/{id}/cancel`. After a timeout the abandoned job keeps occupying the queue, so the next clip times out even though its own job fits the budget. Model: the first job takes 900 s (model load), later jobs take 450 s, timeout is 600 s. Result: clips 0 and 1 both FAIL, 0 cancels are sent and 4 jobs are enqueued. Fix: send cancel on timeout or interrupt, and on resume reuse a completed generation for the same text.
- **FAILURES-7 (low)**: an ASR failure on voice.wav fails the captions stage, although `distribute_words` is a working fallback. Fix: catch TranscribeError in make_captions and distribute.
- **FAILURES-8 (low)**: LLMTruncated says to retry "with a larger max_tokens", but max_tokens is hard-coded (16000) and `settings set llm.max_tokens=...` is rejected as an unknown key. A long transcript at high effort can't be recovered. Fix: an `llm.max_tokens` setting, or retry once with a larger value.
- Verified OK: an empty or music-only transcript gives a clean MomentsError. Zero valid moments gives a clean error. A video shorter than min gives one full-length clip that renders. Voicebox down, non-JSON and HTTP errors map to clean VoiceboxErrors. job.json writes are atomic. All ffmpeg outputs are atomic.

## Specialist Reviewer (tests)

I reviewed tests/ read-only, with no source edits and nothing committed. Baseline: 246 passed in about 77 s. I ran the whole suite under a socket/anthropic-client logging plugin (/tmp/tests_rev/netguard.py) and saw **0 network connects**. Every real `anthropic.Anthropic` that gets built uses a MockTransport or is never called. The data dir is isolated (the real data/jobs is untouched). Voicebox tests use a fake clock, so there are no real sleeps. I checked the client's endpoints and status names against upstream jamiepine/voicebox backend/routes and models.py, and they match. My scratch tests are in /tmp/tests_rev/tc and the mutation copy is in /tmp/tests_rev/mut.
- **TESTS-1 (high)**: the Whisper fake hides FAILURES-1. `_FakeWhisperModel` replaces `faster_whisper.WhisperModel` entirely and the test "audio" is `b"RIFF"`, so audio decoding never runs. `faster_whisper.decode_audio(<real 16 kHz wav>)` raises `TypeError ... 'metadata_errors'` on the locked env, yet all tests stay green. Fix: add a non-slow test that decodes a real `make_tone` wav through the same input path WhisperTranscriber hands to `model.transcribe` (no model is needed). It should fail today and pass after the FAILURES-1 fix.
- **TESTS-2 (medium)**: there is no suite-wide guard against real services. `config.load_dotenv()` at import puts the project's real ANTHROPIC_API_KEY into os.environ for every test, so it is still present at session end. Inside a test, `pipeline._make_llm(Settings())` builds a live client for https://api.anthropic.com. conftest only isolates the data dir, and `forbid_real_clients` is used by one test. Any test that reaches run_job/rerender/cli with deps=None without patching `_make_*` would bill the user's key, hit a real local Voicebox at 127.0.0.1:17493, or download a Whisper model. Today that covers the untested `cli rerender` and `cli profiles` paths, and it would also happen after any `_LazyDeps` regression. Fix: an autouse conftest fixture that removes ANTHROPIC_* env vars, points HOME at tmp, makes `pipeline._make_llm/_make_voicebox/_make_transcriber` raise unless a test opts in, and blocks non-loopback socket.connect.
- **TESTS-3 (medium)**: the voice-shortening write-back is untested at pipeline level. The e2e fakes always fit (8.4 s of voice in a 12 s clip). I made a mutation copy with `_write_script(script_path, script)` removed from `_run_voice`, and test_pipeline_e2e + test_fixes_regression + test_voice_captions_render still pass (96/96). Without the write-back, script.json keeps the long text and voice.json has the short text, so every resume pays Voicebox + LLM shorten again. That breaks the README promise. The current code does work (scratch test with FakeVoicebox(0.8)). Fix: an e2e test with seconds_per_word=0.8 that asserts voice.shortened, script.json == voice.json narration, words.json matches it, and a second run makes 0 Voicebox calls.
- **TESTS-4 (low)**: the `slow` marker is not skipped by default, although CONTRACT section 0 says it is. pyproject only registers it (no addopts or skip hook). A scratch `@pytest.mark.slow` test runs under plain `pytest -q`. Fix: `addopts = "-m 'not slow'"` or a `--run-slow` option.
- **TESTS-5 (low)**: test_pipeline_e2e depends on test order. It shares a module job that the rerender tests change. Running `test_rerender_narration_edit_regenerates_voice` before `test_run_job_produces_final_clips` fails on `assert "परीक्षण" in raw`. Fix: give the mutating tests their own job, or have the fixture snapshot the values it asserts on.
- **TESTS-6 (low)**: `test_doctor_command_runs` asserts `code in (0, 1)`, which always passes. doctor's exit-code contract (1 when something required is missing, 0 otherwise) is never checked through main(). Fix: patch the checks to all-ok and assert 0, then make one fail and assert 1.

## Ops (Voicebox)

Voicebox v0.6.0 now runs locally on CPU at http://127.0.0.1:17493. English and Hindi both generate real audio through the API. Details, start/stop commands and the PID are in /home/ptl-admin/tools/voicebox/START.md, and the log is /home/ptl-admin/tools/voicebox/server.log.
- Install: the releases have no Linux AppImage, so the backend runs from source (`python -m backend.main`) in its own Python 3.12 venv under /home/ptl-admin/tools/voicebox, with torch 2.6.0+cpu. Only the `chatterbox` engine's dependencies are installed (PyPI only), plus `setuptools<81`. resemble-perth imports `pkg_resources`, and without it every chatterbox generation fails with "'NoneType' object is not callable".
- Engine id: `chatterbox` = Chatterbox **Multilingual** (repo ResembleAI/chatterbox, 23 languages including `hi`). `chatterbox_turbo` is English only. The full engine enum is qwen|qwen_custom_voice|luxtts|chatterbox|chatterbox_turbo|tada|kokoro.
- Profile: one cloned profile `279c77e6-db44-476a-a560-ff9d64cac669` ("Narrator (LibriVox PD)", default_engine chatterbox). It is cloned from an 11 s public-domain LibriVox intro on archive.org. Settings are set: english/hindi profile_id = that id, both engines = chatterbox. `autoclipper doctor` reports Voicebox OK.
- Timings on CPU: en 4.06 s of audio in 33.7 s (includes the model load); hi 5.26 s of audio in 24.4 s. Warm: en 1.94 s of audio in 13.1 s, hi 2.58 s in 14.1 s. Server RSS is about 4.7 GB with the model loaded. A 60 s clip narration will take a few minutes per language.
- API vs autoclipper/voicebox.py: **compatible, no code change needed.** I verified this live: `VoiceboxClient().synthesize(...)` worked for en and hi. POST /generate takes `{text, profile_id, language, engine}` (optional: seed, model_size, instruct, max_chunk_chars=800, crossfade_ms=50, normalize=true, effects_chain) and returns immediately with `{"id": ..., "status": "generating", ...}`. GET /history/{id} returns `status` generating -> completed|failed, and `error` holds the message (a cancel is stored as failed / "Generation cancelled"). It also has a `duration` field. GET /audio/{id} returns audio/x-wav (pcm_s16le, 24 kHz mono), or 404 before it is completed. GET /profiles returns a plain list of profile objects.
- Cosmetic notes only: `language` is validated by a regex pattern (en, hi, ... are fine). A profile can only be used with chatterbox after a reference sample has been uploaded (POST /profiles/{id}/samples, multipart with `file` and `reference_text`).

## Team Lead (triage)

I checked every finding against the code. Verified directly: FAILURES-1 (`faster_whisper.decode_audio` on a real 16 kHz wav gives `TypeError ... 'metadata_errors'` with av 19.0.2 and faster-whisper 1.2.1), TESTS-4 (pyproject only registers `slow`), TESTS-2 (`config.load_dotenv()` runs at import), FAILURES-6 (`POST /generate/{generation_id}/cancel` is in Voicebox's openapi.json, and `wait()` never calls it), PIPELINE-1 (fixed `job.json.tmp`), PIPELINE-4 and PIPELINE-5 (rerender code), WINDOWS-1 (only fc-list), WINDOWS-3 (`_AtomicOutput.__exit__`), FAILURES-3/4/5/7/8 and HINDI_CAPTIONS-1/2/3 (stage code). The config defaults already match the real Voicebox (`chatterbox` for en and hi), and ops confirmed voicebox.py is compatible. The only Voicebox code change is the cancel call (FAILURES-6).

**Accepted (all 28 findings).** Narrowed scope:
- PIPELINE-1 + WINDOWS-2: give each save a unique tmp name, retry os.replace on PermissionError, and make progress saves non-fatal. The cross-process lock and lost-update merge are deferred to step 2. The rule is one writer per job (the worker), and the UI must send rerender through that worker. This is documented in CONTRACT.
- FAILURES-2: transcribe in windows only when the audio is longer than 20 min.
- FAILURES-8: add the `llm.max_tokens` setting and make the error message name it. No automatic retry.
- FAILURES-1: fixed by passing an ndarray. We do not pin `av<19`.

**Assignments, split by file:**
- fixer_a (pipeline.py, cli.py, jobstore.py, config.py, README, docs, pyproject.toml, conftest, test_pipeline_e2e, test_fixes_regression): PIPELINE-1..5, WINDOWS-1, WINDOWS-2, FAILURES-3 (pipeline side: moments.json parse), FAILURES-8 (setting), WINDOWS-4 (README), TESTS-2..6, Voicebox README notes.
- fixer_b (ffmpeg.py, llm.py, voicebox.py, stages/*, other tests): FAILURES-1, 2, 3 (stage-side atomic writes and corrupt transcript), 4, 5, 6, 7, 8 (llm side), WINDOWS-3, WINDOWS-4 (cpu fallback), HINDI_CAPTIONS-1..3, TESTS-1.
- Interface notes: (1) fixer_a adds `LLMSettings.max_tokens: int = 16000` (ge 1024, le 64000). fixer_b reads it with `getattr(settings, "max_tokens", 16000)` so the two can land in either order. (2) For HINDI_CAPTIONS-1, fixer_b adds an optional `VoiceResult.spoken_narration`, and make_captions reads voice.json itself. pipeline.py does not change. (3) fixer_a's conftest guard must leave the existing `forbid_real_clients` fixture and MockTransport-based tests working.

## Fixer A

All 16 assigned fixes applied, in my files only (pipeline.py, cli.py, jobstore.py, config.py, README, CONTRACT, pyproject.toml, conftest, test_pipeline_e2e, test_fixes_regression). No network; fakes only. Nothing committed.
- **PIPELINE-1** (jobstore.py): `save()` writes `job.json.<pid>.<thread>.<uuid8>.tmp` and removes it in a `finally` block, so nothing is left behind if the replace fails. The module RLock is kept, and there is no cross-process lock. The new CONTRACT section 1a says each job has exactly one writer, and that the step-2 UI reads job.json read-only and sends rerender/resume through the worker. Test: 2 threads with separate JobStores make 200 saves each while a reader parses job.json. There are no errors, no torn reads and no tmp files left.
- **WINDOWS-2**: `_replace_with_retry` retries on PermissionError (`REPLACE_RETRIES=20` x `REPLACE_RETRY_DELAY=0.05`), then re-raises. In `_Reporter.progress`, the throttled `set_stage` is wrapped in `except OSError` and logs `warning: could not save progress to job.json: ...`. Tests: one PermissionError and the save succeeds; a persistent one is re-raised after 20 tries; the progress callback swallows the error.
- **PIPELINE-3**: new helper `_invalidate(store, clip, lang, stages)` sets the stages to pending with progress 0 and message "", sets `final=None`, and saves. `_run_voice` calls it for captions+render right after its unlink, and `_run_captions` calls it for render, both before `rep.start`. Tests: a failing voice regeneration leaves captions/render not done and final None, both while the voice runs (a spy reads job.json) and afterwards. A failing captions regeneration leaves render pending and final None.
- **PIPELINE-2**: `_job_stage_cut` calls `_invalidate_after_cut` when a todo clip was cut successfully. For every lang dir under clips/NN/ it unlinks voice.wav, voice.json, words.json, captions.ass and final.mp4. script.json is kept. It then calls `_invalidate(voice, captions, render)` for each lang. Tests: delete clips/00/cut.mp4 and resume. Voicebox is called only for clip 00, clip 00's final.mp4 is rebuilt and clip 01's is untouched, and no script is rewritten. An interrupt variant checks the reset state on disk.
- **PIPELINE-4**: `_job_complete(store)` requires every JOB_STAGES stage to be done/skipped. Every clip must also have, for every job language (the union of the languages in clip.langs), render done/skipped and no error. rerender flips failed to done only when that holds. Tests: interrupt partway, rerender 0/en, and the status stays failed/"interrupted". A fully finished job marked failed becomes done.
- **PIPELINE-5**: rerender now checks cut.mp4 and script.json before `setdefault`/`save`, without creating the lang dir. Test: rerender J 0 hi on an en-only job raises, job.json has only `en`, and no clips/00/hi dir is created.
- **FAILURES-3** (pipeline side): `_move_corrupt` renames a bad file to `<name>.corrupt` (os.replace), logs `warning: moments.json is corrupt (...); moved to ... and regenerating it`, and the stage then runs as if the file were missing. This happens in `_job_stage_moments` (bad JSON, not a list, or Moment validation) and in `_job_stage_transcribe` (Transcript validation). Tests: truncated moments.json, invalid moment entries, truncated transcript.json.
- **WINDOWS-1** (cli.py): `check_fonts(..., system_font_dirs=None)` defaults to `windows_font_dirs()` (%WINDIR%\Fonts and %LOCALAPPDATA%\Microsoft\Windows\Fonts) when `os.name == "nt"`, and to [] otherwise. It uses the same `_font_files` stem matching (shared `_family_in_stems`) and labels hits "system". Test: which=None plus a tmp system dir holding the two Noto files gives ok.
- **FAILURES-8** (config.py): `LLMSettings.max_tokens: int = Field(16000, ge=1024, le=64000)`. I documented it in the README settings table. Test: `settings set llm.max_tokens=32000` works, and 100, 100000 and "lots" are rejected.
- **WINDOWS-4 / VOICEBOX-OPS** (README only): I added a Windows section on the CUDA 12 cuBLAS + cuDNN 9 runtimes, `whisper.device=cpu`, and the auto fallback (implemented by fixer_b). The Voicebox section now covers: no Linux AppImage (run from source, see Ops notes), chatterbox = Multilingual, chatterbox_turbo = English only, the engine id list, the reference sample, `setuptools<81`, CPU speed and `voicebox.timeout_seconds`. I added a fonts note for Windows. config defaults are unchanged.
- **TESTS-2** (conftest): the autouse `_no_real_services` fixture removes ANTHROPIC_* and sets HOME, USERPROFILE and XDG_CONFIG_HOME to tmp. It makes `pipeline._make_llm/_make_voicebox/_make_transcriber` raise `RuntimeError('real client in tests')` unless the test has the `real_clients` marker (registered in pyproject), and it blocks AF_INET/AF_INET6 `connect`/`connect_ex` to non-loopback addresses. `forbid_real_clients` and the MockTransport tests still work. No existing test needed a change for this. A guard self-test and a `real_clients` marker test are in test_fixes_regression.
- **TESTS-3**: `test_voice_shortening_writes_back_and_resume_is_free` (FakeVoicebox 0.8 s/word) checks that voice.json has shortened=True, that script.json and voice.json have the same narration, and that words.json joins to it. A second `run_job(--job)` makes 0 Voicebox calls and 0 shorten calls.
- **TESTS-4**: pyproject `addopts = "-m 'not slow'"`. The README dev section mentions `uv run pytest -m slow`.
- **TESTS-5**: the new `private_job` fixture copytrees the shared job into the test's own data dir. Both mutating rerender tests use it. Checked in reverse order (narration/title rerender before `test_run_job_produces_final_clips`) and with `-k` selection: both pass.
- **TESTS-6**: `test_doctor_command_runs` patches every check_* to ok and expects exit 0. One failing fonts check gives 1. Only node at warn gives 0.
- **Test I adjusted (the test was wrong after FAILURES-7):** `test_resume_after_failed_downstream_rebuilds_stale_outputs[captions]` used a failing transcriber to fail captions. fixer_b's FAILURES-7 now falls back to even word spread, so captions no longer fails there. The test now monkeypatches `stages.captions.make_captions` to raise, and its intent is unchanged.
- Final full run: `uv run pytest -q` gives 296 passed, 2 failed. Both failures are in `tests/test_ffmpeg.py` (`test_mix_burn_graph_and_fontsdir_only_with_fonts`, `test_mix_burn_graph_without_voice`). They assert the old mix_burn graph, but fixer_b's in-progress HINDI_CAPTIONS-3 work in ffmpeg.py (`aformat`/`alimiter`) has changed it. Those files belong to fixer_b and I did not touch them. Every test in my files passes.

## Fixer B

All 15 assigned fixes are applied, and each has a regression test. `uv run pytest -q` gives 327 passed (fixer_a's work is included, nothing failed). Only fakes and MockTransport are used: no Anthropic API, no Voicebox, no model download. Nothing is committed. I edited only my files: ffmpeg.py, llm.py, voicebox.py, stages/*, and test_ffmpeg, test_ingest_cut, test_llm_moments_script, test_voice_captions_render, test_voicebox, plus the new tests/test_transcribe_audio.py.
- FAILURES-1 / TESTS-1 (transcribe.py): `_load_audio(path)` reads a 16 kHz mono s16le wav with `wave`+numpy (float32 / 32768). Anything else is first converted with `ffmpeg.to_wav(..., 16000, 1)` into a temp dir. `model.transcribe` now gets the ndarray, never a path, and `duration = len(arr)/16000`. Tests use real make_tone wavs at 16 kHz and 44.1 kHz. A fake model asserts it gets a 1-D float32 ndarray of the right length, and `faster_whisper.decode_audio` is patched to raise the PyAV TypeError. I changed `test_whisper_transcriber_lazy_and_options` to write a real 4 s wav instead of the bytes `b"RIFF"`, because a fake wav cannot be decoded.
- FAILURES-2: audio over 20 min (`WINDOWING_THRESHOLD`) is transcribed in windows of about 15 min. Each cut moves to the quietest 20 ms frame within +/-10 s, and the last window is 5-20 min long. Window offsets are added to segments and words, and both are clamped to the window, so timestamps stay monotonic. Progress is reported across windows ("transcribing (2/3)"), each chunk is deleted before the next one, and the language the first window detected is reused for the later windows.
- WINDOWS-4: with device=auto resolved to cuda, a WhisperModel load error, or an error during the first transcription (call or segment iteration), that matches cuda/cublas/cudnn/.dll/.so logs a warning, resets the model and retries once on cpu/int8. It does not fall back for explicit `device=cuda`, for errors that do not name a library (e.g. OOM), or after a CUDA transcription has already succeeded.
- FAILURES-3: new private `stages/_atomic.write_text_atomic` builds the text first, writes a unique sibling `.name.<pid>.<rand>.tmp`, fsyncs it, then calls os.replace, and removes the tmp file on any error. It is used for transcript, moments, script, voice.json, words.json, captions.ass and metadata.json. In `run_transcribe`, an unreadable transcript.json is moved to `transcript.json.corrupt`, logged and reported through on_progress, then transcribed again. The old `test_corrupt_transcript_raises` tested the old behaviour, so it now asserts the regeneration.
- FAILURES-4 (ingest.py): a download without a video stream is renamed to `source.rejected.<ext>` (or unlinked if the rename fails), so a retry downloads again. If an existing source is reused and has no video, ingest raises "<name> in <dir> has no video stream; delete it or start a new job".
- FAILURES-5 (voice.py): any exception from `shorten` (e.g. LLMRefusal) is logged and reported as "shortening failed: ...; speeding up and trimming instead", and the original text goes through tempo + trim.
- HINDI_CAPTIONS-1: `VoiceResult.spoken_narration: str | None = None` (old voice.json files still validate). When trimmed, `estimate_spoken_prefix` spreads d/tempo over the words by character weight and keeps the words that end <= clip_duration, always at least one word. narration and narration_hash are unchanged. `make_captions` reads `load_voice_result(out_dir)`, but only uses it when its narration_hash matches the narration it was given. If that voice was trimmed, it captions spoken_narration, and `align_words(..., trimmed=True)` drops script words after the last matched word. build_ass now works in integer centiseconds with end >= start + 1 cs. In the 22-word / 4 s repro, words.json holds 10 words (none unspoken) and no event has zero length.
- HINDI_CAPTIONS-2: `_norm` (used only for matching) applies NFD, removes the nukta, maps chandrabindu to anusvara, turns nasal+virama before a consonant into anusvara, strips ZWJ/ZWNJ, then NFC + casefold + L/M/N. All 5 pairs match. In the 7-word example every word matches, and है is at 6.0 s.
- FAILURES-7: make_captions catches any exception from transcribe_voice, logs it, and falls back to distribute_words.
- HINDI_CAPTIONS-3 (ffmpeg.mix_burn): the voice gets `aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0` (unity). The original gets the same chain when probe says it is mono, otherwise `aformat=channel_layouts=stereo`. `MediaInfo.audio_channels` is new and defaults to 0. Both the amix path and the no-voice path end in `alimiter=limit=0.97:level=0`. **level=0 is required:** alimiter's default auto-level would raise quiet audio up to the limit. Tests: voice_volume=2.0 over a -0.5 dBFS original peaks below 0 dB (it reads 0.0, i.e. clipped, without the limiter), and voice_volume=1.0 keeps a -6 dB mono voice at -6 dB.
- **Found while testing: `apad` is removed from the voice chain.** The old graph (`...,apad[a1]` + amix + `-t`) sometimes deadlocks ffmpeg 7.1.1 under CPU load: the process sits in a futex at 0% CPU, and SIGTERM does not stop it. In a stress run with 12 parallel loops, the old and the new graph both hung about 1 run in 30. Without apad, 0 of 360 hung. `amix duration=first` already keeps the original going after the voice ends, so the output is the same. ffmpeg.run has no timeout, so a hung render blocks the job forever. A watchdog is worth considering in step 2.
- WINDOWS-3: `_AtomicOutput.__exit__` retries os.replace on PermissionError for about 1 s (`_REPLACE_RETRY_SECONDS`). On the final OSError it unlinks the .part file and raises FFmpegError("could not replace final.mp4: is it open in another program (e.g. a video player)? Close it and retry (...)").
- FAILURES-6 (voicebox.py): new `cancel(gen_id)` sends `POST /generate/{id}/cancel` (method checked in openapi.json) with a 5 s timeout. It never raises and returns True/False. `wait()` cancels once on timeout (it raises a plain VoiceboxError) and on any BaseException (KeyboardInterrupt, a progress callback that raises), then re-raises. It does not cancel failed generations or transport errors. The deadline still starts when waiting starts, because Voicebox only reports generating/completed/failed.
- FAILURES-8 (llm.py): the budget is `getattr(settings, "max_tokens", 16000)`. shorten uses `min(8000, budget)`. Above 21333, `timeout=1800` is passed. A MockTransport test checks that the real SDK otherwise raises "Streaming is required". LLMTruncated now says "...cut off at max_tokens=N; raise it with `autoclipper settings set llm.max_tokens=32000` or lower llm.effort".

## QA Co-Lead (verification of round-2 fixes)

I checked all 28 accepted fixes by reproducing each failure scenario on the current tree with fakes, MockTransport and real ffmpeg. I did not rely on reading the diffs. Nothing called the real Anthropic API, a real Voicebox or a Whisper model download. Nothing was committed. The scratch repro scripts are outside the repo.

**Verified (scenario reproduced, and the fix holds):**
- PIPELINE-1: separate JobStores with 2 threads x 500 saves plus a reader: no errors, no torn reads, no tmp files left. **Gap fixed:** the thread test `test_concurrent_saves_from_two_jobstores_never_collide` also passes on the *old* code (the module RLock serializes threads), so it did not guard the fix. The real race is between processes. I added `test_concurrent_saves_from_two_processes_never_collide` (3 subprocesses x 400 saves). It passes now. Against the HEAD jobstore.py it fails with the original `FileNotFoundError: job.json.tmp -> job.json`.
- WINDOWS-2: one PermissionError from os.replace and the save succeeds on the 2nd try, with no tmp left. A progress callback whose set_stage raises PermissionError logs "could not save progress" and does not raise.
- PIPELINE-2: run a job, delete clips/00/cut.mp4, then `run --job`. Exactly 1 Voicebox call is made, for clip 00. Clip 00's final.mp4 is rebuilt, clip 01's mtime is unchanged, and the job ends done.
- PIPELINE-3: edit the narration and rerender with a failing Voicebox. job.json shows voice failed, captions/render pending, final=None, and final.mp4 is gone. A later resume repairs it.
- PIPELINE-4: interrupt in clip 01's voice, then rerender 0/en: the status stays failed. Rerender 1/en too and the status becomes done with error=None.
- PIPELINE-5: rerender J 0 hi on an en-only job raises PipelineError. There is no `hi` in job.json and no clips/00/hi dir.
- FAILURES-3 (pipeline + stages): truncate moments.json and transcript.json and resume. Both are moved to *.corrupt with a warning and regenerated, and the job ends done. When `write_text_atomic`'s os.replace fails, the old file stays intact and no tmp file is left.
- WINDOWS-1: with which=None and a tmp "system" dir holding the two Noto files, the check is ok and labels them (system). With an empty dir it fails.
- FAILURES-8: `settings set llm.max_tokens=32000` works and 100 is rejected (exit 1). AnthropicLLM sends max_tokens=32000 with timeout=1800. shorten sends 8000 and no timeout. The truncation message names the setting.
- FAILURES-1: I reproduced the original bug here: `faster_whisper.decode_audio` on a real 16 kHz wav raises `TypeError ... 'metadata_errors'`. `_load_audio` returns float32 1-D arrays of the right length for 16 kHz and 44.1 kHz wavs, and the model receives an ndarray. Mutation check: switching back to `decode_audio(str(audio))` makes tests/test_transcribe_audio.py fail (TESTS-1 guards the fix).
- FAILURES-2: on 25 min of synthetic audio with a quiet spot 4 s after 15:00, there are 2 windows, the cut lands at 904.01 s, the offsets are applied, the segments are monotonic and progress is monotonic.
- WINDOWS-4 (code): with auto + a cuda WhisperModel that raises "Library cublas64_12.dll is not found", it retries on cpu/int8. With explicit `device=cuda` it does not fall back. **Correction to fixer_b's note:** the matcher keys on `cuda` anywhere in the message, so a real CTranslate2 OOM ("CUDA failed with error out of memory") *does* fall back to CPU once in auto mode. Only an OOM message without "CUDA" (the one the test uses) does not. I left this as it is: the fallback runs once, logs a warning, and on auto it is better to finish slowly than to fail.
- FAILURES-4: a fake YoutubeDL that writes an audio-only m4a leaves `source.rejected.m4a`, and find_source returns None. A reused audio-only source.m4a gives "has no video stream; delete it or start a new job".
- FAILURES-5 + HINDI_CAPTIONS-1: 22 words / 4 s clip with shorten raising LLMRefusal: trimmed=True, the narration is unchanged, and spoken_narration holds the first 10 words. words.json holds those 10 words, ending at 4.0 s, and no ASS event has end <= start.
- FAILURES-7: with a transcriber that raises TranscribeError, captions.ass is still written.
- HINDI_CAPTIONS-2: all 5 pairs normalise equal, and the display text is unchanged.
- HINDI_CAPTIONS-3: measured with volumedetect on real renders. A -0.2 dBFS original plus a -0.9 dBFS voice at voice_volume=2.0 peaks at -0.3 dB. The no-voice path with original_volume=1.5 also peaks at -0.3 dB. A -24 dB mono voice at voice_volume=1.0 comes out at -23.9 dB (unity). Mutation check: replacing the limiter with `anull` fails a test.
- WINDOWS-3: when os.replace always raises PermissionError, the result is FFmpegError("... is it open in another program ..."), the .part file is removed and the old final.mp4 is kept.
- FAILURES-6: on a timeout, exactly one `POST /generate/g1/cancel` is sent. A KeyboardInterrupt during polling also sends the cancel.
- TESTS-2: with temp probe tests (since deleted), I confirmed there are no ANTHROPIC_* vars, HOME is under tmp_path, all 3 factories raise "real client in tests", and httpx/IPv6 connects to non-loopback addresses are blocked.
- TESTS-4: addopts is `-m 'not slow'`, and the README mentions `uv run pytest -m slow`. TESTS-5: the e2e tests pass in reverse order with `-p no:randomly`, and with `-k` selection. TESTS-3/TESTS-6: present and passing.
- WINDOWS-4 (README), VOICEBOX-OPS, the llm.max_tokens README row and CONTRACT 1a are all present and match the instructions.
- Mutation checks on PIPELINE-2/3/4: disabling `_invalidate_after_cut`, the voice `_invalidate`, or `_job_complete` in rerender each makes a regression test fail.

**Changed by QA:** tests/test_fixes_regression.py (new cross-process PIPELINE-1 test). No production code changed.

**Full suite:** `uv run pytest -q` gives **328 passed** (the 2 test_ffmpeg failures in fixer_a's report are gone; fixer_b updated those tests).

**Open for step 2 (not regressions):** ffmpeg.run has no watchdog (fixer_b's note). The cross-process lock and lost-update merge for job.json are still deferred: the single-writer rule in CONTRACT 1a is what prevents lost updates.

## Team Lead (round-2 final status: real end-to-end run)

### Real run
- `autoclipper doctor`: 0 problems, 1 warning (node v16; only needed for step 3).
- Command: `uv run autoclipper run data/test_media/reagan_challenger_speech_1986.ogv --clips 2 --min 20 --max 35 --langs en,hi`. This used real Claude (claude-opus-5-5), real faster-whisper `small` on CPU (downloaded on first use) and the real Voicebox (`chatterbox`, cloned LibriVox PD profile).
- Job `20261122-185849-65a6bd`, status=done, with all 4 clip-langs done. It took about 14 min wall time; most of that was Voicebox on CPU, about 2 min per narration.
- Moments: clip 00 is 127.66-157.88 s ("school children of America ... belongs to the brave"); clip 01 is 231.88-260.72 s ("like Drake's ... touch the face of God"). Both are the right picks, and both start and end on segment boundaries.

### Editor review
- Every final.mp4 is h264 1080x1920 + aac 48 kHz stereo, at 30.24 s and 28.84 s (equal to the cut length).
- Voice: en 26.0 s and 22.4 s, hi 18.5 s and 18.7 s. tempo=1.0 on all four; nothing was shortened or trimmed.
- Whisper on the voices: the English narration is word-perfect. The Hindi audio is complete (checked by transcribing the last 6 s), though Whisper's own Hindi output is rough ("लाको" for "लाखों"), so Hindi pronunciation is understandable but not checked by a native speaker.
- Loudness: the cut is about -25 LUFS, the voice.wav files about -20.5 LUFS, and the finals about -17.6 LUFS (mono voice copied to 2 channels, +3 dB). There is no clipping.
- Scripts and metadata: the Hindi is natural Devanagari, conversational with English loanwords (क्लास, लाइव). Titles are 43-61 chars, hashtags are 7 per clip-lang and sensible, and the facts check out (Peggy Noonan, "High Flight", 73 seconds). One small spelling slip: "अठ्ठाईस" in 00/hi; 01/hi uses the standard "अट्ठाईस".

### Defect fixed (lead)
- **Hindi caption shaping was broken.** The burned frames showed भविष्य as "भवष्ि्य", खत्म with a visible virama, and छियासी mangled: the `subtitles` filter on this FFmpeg 7.1 / libass build used simple shaping.
- Fix: `ffmpeg.ass_filter` now emits `ass=filename=...:shaping=complex[:fontsdir=...]`. The `ass` filter is already required by doctor.
- Tests: updated the two string tests in tests/test_ffmpeg.py and added tests/test_devanagari_shaping.py, which does a real ffmpeg burn compared pixel-for-pixel against explicit complex and simple shaping. Mutation check: the test fails with the old `subtitles=` filter.
- README and CONTRACT section 2 are updated.
- I re-rendered all 4 finals with `autoclipper rerender`. The voice was reused (no new Voicebox calls), and the frames now show correct conjuncts and matras.

### Open (not fixed, needs a product decision)
- **Original audio stays at 15% after the narration ends.** The Hindi narrations end around 18.6 s in 29-30 s clips, so the last ~10 s are near-silent (-43 LUFS). In clip 01 that is exactly where "touch the face of God" is said. Proposal: un-duck the original (ramp to about 1.0) once the voice ends, or have the script ask for a narration that fills about 85-95% of the clip.
- Hindi narrations come out much shorter than the English ones for the same clip (about 18.5 s vs 22-26 s), which makes the issue above worse.
- Captions sit at chest height (margin_v 520) over a 4:3 source center crop. That is readable, but a talking-head crop puts the face in the upper half; this is fine for now.
- Whisper on the Hindi voice drops its final words (one long segment), so caption timing for the last few Hindi words is interpolated rather than matched. The timing is still monotonic and ends at the voice end.

### Tests
- `uv run pytest -q`: 329 passed (328 + 1 new). Nothing committed.

### Sign-off
- Step 1 is APPROVED for the CLI pipeline: it works end to end with the real services. The un-duck item above is the top item for the next round.
