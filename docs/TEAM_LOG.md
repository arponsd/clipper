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
