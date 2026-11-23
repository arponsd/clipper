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

## Team Lead (step 2 plan: local web UI + round-2 product fixes)

### Plan
- Step 2 = `uv run autoclipper serve` (FastAPI + uvicorn, 127.0.0.1:8765): one static page (vanilla JS/CSS, offline,
  no build) + a token-protected JSON API + ONE worker thread with an in-process FIFO queue that is the single
  writer of job.json (CONTRACT 1a). Plus 4 product fixes from the real run (un-duck tail, narration length per
  language, caption position, Hindi spelling).
- The contract is **docs/CONTRACT_WEB.md** (routes, limits, JSON shapes, status codes, error format, worker API,
  auth, static layout, new settings fields). Builders work from it in parallel. CONTRACT.md still applies.

### Assignments (files must not overlap)
- **backend**: autoclipper/web/{__init__,server,worker,auth,errors}.py, cli.py `serve` subcommand only,
  pyproject deps, tests/test_web_*.py.
- **frontend**: autoclipper/web/static/{index.html,app.js,styles.css} only.
- **product**: stages/{render,script,voice,captions}.py, llm.py prompts + `lengthen`, config.py (new fields only,
  CONTRACT_WEB section 8), pipeline.py (lengthen wiring / script settings only), tests/test_product_fixes.py,
  README settings rows.

### Lead changes already made (tests green: 329 passed)
- interfaces.py: `LengthenFn`, optional `Lengthener` protocol (kept OUT of `LLM` so existing fakes and
  isinstance checks stay valid), `LLM.write_script(..., target_seconds=None)`.
- tests/fakes.py: `FakeLLM.lengthen(...)`; `FakeLLM.write_script(..., target_seconds=None)` (recorded in calls
  only when given, so existing call assertions still match).

### Decisions everyone must know
- Upload = 2 steps: raw-body streaming `POST /api/uploads?filename=` (no multipart, size limit enforced while
  streaming -> 413), then `POST /api/jobs {upload_id|url, options}` with `Idempotency-Key`. The upload is moved
  into the job dir as `source.<ext>`; ingest reuses it via find_source. The API never accepts a local path.
- Job options are persisted in `data/jobs/<id>/options.json` (job.json has no options field) so Resume reuses them.
  The worker creates the job at submit (status queued) and always calls `run_job(..., job_id=...)`.
- Auth: token in `data/web/token` (0600), injected into index.html meta tag; `X-Auth-Token` on /api (401 missing,
  403 wrong); `?token=` accepted ONLY on the video/download GETs (media elements cannot send headers).
  Host guard (403) on every request, Origin/Sec-Fetch-Site guard (403) on writes, strict CSP (no inline JS/CSS).
- 409 is used for conflicts (save/rerender while that clip is being processed, reused Idempotency-Key with a
  different body, resume of a complete job); 503 for Voicebox unreachable on the profiles route.
- Cancel is cooperative: the worker's `log` raises `TaskCancelled(BaseException)`; the pipeline already records
  BaseException as "interrupted" and ffmpeg.run kills its child. Server start marks leftover running/queued
  jobs failed/"interrupted" (skips job.json touched < 30 s ago: a CLI may own it).
- Measured from the sample job 20261122-185849-65a6bd (read-only): chatterbox EN 2.97/3.03 wps, HI 3.52/3.21 wps
  (pooled 3.00 and 3.36). Old constants (en 2.6, hi 2.2) explain HI fill 61-65 % vs EN 78-86 %. The LLM also
  overshoots the requested words by about 5-8 %. New defaults: script.wps_en=3.0, wps_hi=3.35, fill_ratio=0.88.
  (The brief's 2.5-2.7 EN estimate is lower than what this Voicebox actually produced.)
- Ducking: `audio.original_volume` now means "while the voice plays"; `audio.original_volume_after` (1.0) applies
  before/after the voice with 0.4 s ramps; loudnorm (-14 LUFS) + the existing limiter on the final mix.
  Implemented in stages/render.py with its own graph; ffmpeg.py is not edited.
- Captions: `captions.position_pct` (72, centre of the line, % from top) via `\an5\pos`; None = legacy margin_v.

### Review checklist for step 2
- Every route in CONTRACT_WEB section 4 exists with the listed status codes; every error is
  `{"message","errors"}`; no traceback text in any response (test a forced exception -> 500 clean JSON).
- Tests per route: happy, 422 (missing/wrong type/empty/very long/negative/zero/huge/extra keys), 401, 403
  (token, Host, Origin), 404, exact JSON keys+types, disk side effects. Range 206 test.
- No request thread writes job.json of an existing job. Settings writes are atomic.
- Page works with the network off (no CDN), CSP passes (no inline script/style), Devanagari fields use
  Noto Sans Devanagari, dark/light, 360 px wide.
- Product: before/during/after loudness measured in tests; HI target words use wps_hi; one lengthen max,
  never on rerender; caption position both modes; full suite green; the real job dir untouched.

## Builder (product)

### Done (CONTRACT_WEB sections 8 and 9)
- **config.py (new fields only + bounds):** audio.original_volume_after=1.0, duck_ramp_seconds=0.4, loudnorm=True,
  target_lufs=-14; new `script` section (wps_en 3.0, wps_hi 3.35, fill_ratio 0.88, lengthen_below 0.75, `wps(lang)`);
  captions.position_pct=72 (nullable; None = legacy margin_v); new `web` section (max_upload_mb 4096,
  allowed_extensions + validator: lower-case `.` + 1-9 [a-z0-9], 1-30 items, no duplicates). Bounds added:
  font_size 20-200, words_per_line 1-8, poll_interval 0.1-10, timeout_seconds 10-7200, clips.width/height 240-3840
  and even. A step-1 settings.json (no new keys) loads (tested).
- **Ducking (render.py):** render_final builds its own graph (ffmpeg.py untouched; imports `_X264/_AAC/_MONO_TO_STEREO/
  _LIMITER/_SILENCE/_AtomicOutput/_num`). Voice window [0, speech_end]; speech_end = last word end in words.json next
  to the voice (clamped to the voice length), else silencedetect -45 dB/0.3 s on the voice. Original gain =
  `volume='<piecewise-linear>':eval=frame` (public helpers `duck_envelope`, `build_audio_graph`), amix normalize=0 ->
  loudnorm I=target TP=-1.5 LRA=11 + aresample=48000 (when enabled) -> limiter. No voice -> original at
  original_volume_after. New optional kwarg `render_final(..., words_json=None)`.
  Measured in tests on fake media (440 Hz original / 660 Hz voice, bandpassed): tail = original level +-1.5 dB, during =
  -16.5 dB +-2.5, mid-ramp in between, peak <= -0.2 dBFS (also with loud inputs, voice_volume 2, after 2.0).
  Real sample (read-only inputs from job 20261122-185849-65a6bd clip 01/hi, output to scratchpad then deleted):
  speech_end 18.56 s, narrated part -13.9 LUFS, tail -18.4 LUFS (was about -43 LUFS), peak -1.4 dBFS.
- **Narration length:** `target_words(duration, lang, *, wps=None, fill_ratio=FILL_RATIO)`;
  `write_script(..., script_settings=None)` passes `target_seconds=round(duration*fill_ratio, 1)` only when settings are
  given (step-1 calls unchanged). Prompt states clip seconds, target spoken seconds, words/s for the language and
  "between round(t*0.95) and round(t*1.05) words (target t)". `AnthropicLLM.lengthen` + `LENGTHEN_SYSTEM`.
  `make_voice(..., lengthen=None, lengthen_below=0.75, fill_ratio=0.88)`: one retry, target
  `ceil(words*clip*fill/d)`; the longer take is used only if it fits within max_speedup, else the ORIGINAL raw/norm
  files are restored; LLM or Voicebox errors in the retry -> warning + original take; never shortens/trims after a
  lengthen. `VoiceResult.lengthened=False` (old voice.json files still load).
  Pipeline: `write_script(..., script_settings=settings.script)`; `_run_voice(..., allow_lengthen)` uses
  `getattr(deps.llm, "lengthen", None)` lazily; narration write-back to script.json as for shorten.
- **Captions:** position_pct set -> style Alignment 5 and every Dialogue starts with
  `{\an5\pos(w//2, round(h*pct/100))}` (540,1382 at 1080x1920); None -> Alignment 2 + margin_v. A real libass burn puts
  the text centre at 72 % and 30 % (+-3 %).
- **Hindi spelling:** language_rules("hi") adds standard spelling (अट्ठाईस, not अठ्ठाईस) and no Urdu nuktas unless normal.
- README "Settings reference": new rows + bounds.

### Deviation to review (lead)
- `allow_lengthen` from run_job is `regenerated or not voice.json exists` instead of always True: on a resume
  (`run --job`) after the user edited script.json, a short user narration must not be rewritten by the LLM. This is
  what kept test_fixes_regression::test_resume_after_failed_downstream_rebuilds_stale_outputs green unchanged.
  rerender is always False.

### Step-1 tests changed (and why)
- tests/test_voice_captions_render.py: `test_render_final_delegates_to_mix_burn` replaced by
  `test_render_final_builds_ducking_graph` - render_final no longer calls ffmpeg.mix_burn (spec 9.1); it now checks the
  same inputs/volumes/duration in the ffmpeg args.
- tests/test_fixes_regression.py::test_pipeline_uses_aligned_words_for_en_and_hi: one line `llm.lengthen = None` - its
  8-word narration on a ~12 s clip is now (correctly) lengthened, and the test is about word alignment only.

### Tests
- tests/test_product_fixes.py: 80 tests (settings bounds/compat/disk round trip, envelope + graph strings, speech_end,
  real-ffmpeg levels before/during/after + peaks, prompts via a fake SDK client, lengthen paths, pipeline wiring incl.
  rerender/resume never lengthening user edits and an LLM without `lengthen`, caption position both modes + real burn).
- Full suite `uv run pytest -q`: **633 passed** (includes the other builders' web tests present at the time).

### Requests / notes for others
- README intro line 7 ("keeps the original audio quietly under the voice") and the "If a voice-over is longer..."
  paragraph are not my rows: lead, please reword (ducked only while the narration plays; one lengthen retry for new
  scripts).
- /tmp (tmpfs 7.4 GB) hit 0 MB free during my run: /tmp/pytest-of-ptl-admin holds ~6.2 GB of pytest base temps from
  everyone's runs. I deleted only my own scratch render; nothing else removed.

## Builder (frontend)

The single-page UI is built per CONTRACT_WEB sections 3, 4, 6 and 8. I edited only `autoclipper/web/static/index.html`, `app.js` and `styles.css`, and nothing is committed.

- **Constraints:** vanilla ES2020 and CSS, with no build step, CDN or web fonts. CSP-clean: no inline script, no `style=`, no `on*=`. Styles are set through classes or the CSSOM only. All server text goes through `textContent`, with no `innerHTML`. The `<meta name="autoclipper-token">` placeholder plus `/static/styles.css` and `/static/app.js` (defer) are in place.
- **Routes:** `#/new`, `#/jobs`, `#/jobs/<id>`, `#/jobs/<id>/clips/<n>/<lang>`, `#/settings`, `#/doctor`. Leaving the page with unsaved settings, an unsaved script, or an upload in progress asks for confirmation.
- **API and errors:** every fetch sends `X-Auth-Token`, and JSON bodies set Content-Type. On non-2xx the page shows `message`, and each `errors` key is mapped by its dotted path (longest prefix) onto its field. Errors it cannot map go in a summary list. On 401/403 a sticky "reload the page" banner appears. When the server cannot be reached, an "offline" banner appears and clears on the next success.
- **New job:**
  - Pick a file or drop it anywhere on the page. The extension and size are checked against `settings.web`.
  - The file is uploaded with XHR as a raw body to `/api/uploads?filename=`, showing upload %, speed and a Cancel button.
  - Then `POST /api/jobs {upload_id|url, options}` is sent with a fresh `crypto.randomUUID()` Idempotency-Key per click (with a fallback when that API is missing). The button stays disabled while the request is in flight.
  - If the job POST fails, a retry reuses the same `upload_id`. A 404 means the upload is gone, so the next press uploads again.
  - Option defaults come from the settings.
- **Jobs list and detail:**
  - The job is polled every 1.5 s and `/api/queue` every 2 s. A shared queue pill in the header shows queue state.
  - Polling pauses when the tab is hidden and when nothing is queued or running, and resumes after any action and when queue activity is seen.
  - The detail page shows stage cards with chips, bars and % plus messages; a clip × lang grid with mini stage bars, errors, `voice_stale`, Review, Download and Cancel/Stop; and the queue position.
  - Resume appears for failed, interrupted or partially failed jobs. Cancel/Stop appears for active tasks; stopping a running task asks for confirmation.
  - The log tail (`?tail=200`) shows in a `<details>` and refreshes on every poll while open.
  - Rebuilds keep keyboard focus (`data-fk` keys) and only happen when the JSON changed.
- **Clip review:**
  - `<video controls preload=metadata>` with `?token=&v=<bust>`. The bust is set to the task's `finished_at` after a re-render.
  - The narration has a word count plus estimated seconds from `settings.script.wps_<lang>` (3.0 fallback), compared against the clip length and `max_speedup`.
  - The title has a /70 counter, plus description and hashtags (space or comma separated, with a chip preview).
  - Save is a PUT and shows the voice_stale notice.
  - Re-render saves pending edits first ("Save & re-render"), POSTs, then polls `/api/tasks/{id}` and the job (for stage bars). Both buttons stay disabled while a task or a run_job for the job is active. A 200 response means "already queued".
  - Download uses `download_url?token=`. Previous/next links cover the clip-lang pairs.
  - Hindi fields get `lang="hi"`, and the computed font is `"Noto Sans Devanagari", "Noto Sans", sans-serif` (checked in the browser).
- **Settings:**
  - The form is generated from `/api/settings/schema`. It resolves `$ref`/`$defs`/`allOf` and handles `anyOf`-null as nullable.
  - Fields render by schema type: numbers get min/max/step, enums and consts get selects, bools get checkboxes, an array of enum gets checkboxes, and an array of str gets text.
  - Labels and help text are owned by the frontend, with a fallback to the schema title. Datalists cover whisper model/device/compute_type and the voice engines.
  - The two `profile_id` fields are dropdowns filled from `/api/voicebox/profiles`. On 503 they fall back to text inputs with the server's message.
  - `captions.position_pct` gets a slider plus a number input, a 9:16 preview and a "legacy bottom margin" (null) toggle.
  - Save PUTs the whole object (loaded settings with the form values applied). Fields missing from the schema are not rendered.
- **Doctor:** a table with ok/warn/fail badges plus a summary and a Re-run button.
- **Look and feel:** dark/light follows `prefers-color-scheme`, and a toggle (Auto/Light/Dark) is stored in localStorage. There is a visible focus ring and a skip link. At 360 px there is no horizontal scroll.

**Manual check:** I ran a scratch launcher outside the repo with `create_app(worker=Worker(deps_factory=<tests/fakes>), voicebox_factory=FakeVoicebox)`. `AUTOCLIPPER_DATA_DIR` pointed to a scratch copy of the sample job (I only read the real job dir). The server ran on ports 8790/8791 in the in-app browser. There was no real Anthropic API call and no Voicebox synthesis; the doctor's read-only Voicebox health check did run.
- **Verified:**
  - The jobs list and detail render the sample job, all 4 clip-langs done.
  - The clip review video loads (readyState 4, 30.24 s, seekable via Range).
  - An edit, then Save, shows voice_stale. The first re-render failed with "no Voicebox profile set for hi", and the error appeared in the task line and a toast.
  - I set the profiles in Settings via the dropdown and saved; settings.json on disk changed as expected, and null `whisper.language` stayed null.
  - Settings 422s landed on their fields: `captions.font_size` ">= 20", and the cross-field `clips.max_seconds` error.
  - The second re-render progressed voice → captions → render and finished, the video src was re-busted and voice_stale cleared.
  - New job from an uploaded mp4 (1 clip, EN) went to the detail page and ran to done with live stage updates, and the log tail showed. After done, 0 requests were made in 5 s, so polling paused.
  - Client checks rejected `.txt` and `file://`.
  - The doctor table works.
  - 503 profiles showed the text-input fallback plus the notice.
  - A token rotated on the server showed the 403 banner.
  - At 360 px, scrollWidth is 360 on every page. Dark theme renders.
- **Tests:** `uv run pytest -q` gives **633 passed**. The first run hit `OSError: No space left on device`: the `/tmp` tmpfs (7.4 GB) is full, mostly `/tmp/pytest-of-ptl-admin` (6.2 GB). I re-ran with `--basetemp` on the home disk, deleted my own scratch data afterwards, and did not touch the other dirs. **Lead:** someone should clear old pytest basetemps, or the suite will keep failing for everyone.

**Requests for backend (I don't own these files):**
1. Static responses seem cacheable. After I edited app.js, the browser kept running the old copy until a forced reload. Please send `Cache-Control: no-cache` on `/static/*`, or no-store as `/` already has, so an upgraded install picks up the new JS.
2. Optional: `HEAD /static/app.js` returns 405. This is harmless.

## Builder (backend)

Step 2 backend is built to CONTRACT_WEB sections 1-5 and 7. Nothing committed.
- **Files:** `autoclipper/web/{__init__,errors,auth,worker,server}.py`, the `serve` subcommand in `autoclipper/cli.py` (additions only: `_port`, `cmd_serve`, the parser entry), and `tests/test_web_{support,auth,jobs,clips,settings,worker}.py`. pyproject.toml is unchanged: fastapi, uvicorn and httpx were already there, and python-multipart is not used.
- **Tests:** `uv run pytest -q` gives **633 passed** (224 of them are web tests). Every route has tests for: happy path; 422 cases (missing fields, wrong types incl. "3" and true for ints, empty and very long strings, 0, negative and 10**30 values, bad enums, extra keys, malformed JSON, non-object bodies); 401/403 (token, Host, Origin, Sec-Fetch-Site); 404; exact keys and types; disk side effects. There are also tests for a forced exception returning a clean 500, Range 206/416, FIFO order and positions, rerender and resume idempotency, cancelling queued and running tasks (a stub that logs in a loop), crash recovery, a failing or SystemExit task not killing the thread, the single writer (JobStore.save is spied on: the only save from a request thread is JobStore.create), one real end-to-end run (upload -> job -> review -> edit -> rerender, with the fakes), and `serve --help`/args with uvicorn.run patched.
- **Manual check:** I started `autoclipper serve --port <free port> --no-browser` against the real data dir and only sent GETs and rejected writes. Results: GET / injects the token and sends the CSP and no-store headers; 401/403 for missing or wrong token, bad Host and cross Origin; job list and detail show the sample job 20261122-185849-65a6bd (done, 4/4 rendered, Hindi title renders); video Range gives 206; download gives a `20261122-185849-65a6bd_clip01_hi.mp4` attachment; a file: URL gives 422, malformed JSON 422, resume of the complete job 409, unknown path 404, wrong method 405; profiles came from the live Voicebox; doctor showed 0 problems and 1 warning. Ctrl-C shut the worker down and the process exited 0. The sample job was not modified. Voicebox was not touched. The only new file in the real data dir is `data/web/token` (0600).
- **Choices within the contract (lead: please confirm or veto):**
  1. A cancelled queued `run_job` would leave job.json `queued`, and the request thread may not write it. So the worker thread marks that job `failed`/"interrupted" between tasks, which lets the UI offer Resume. The same happens when a run_job ends before the pipeline can record it (settings or deps failure).
  2. `log` raises TaskCancelled only once. It does not raise again while that TaskCancelled is being handled (checked with `sys.exc_info`). Without this, the pipeline's `rep.interrupted()` -> `log` inside its `except BaseException` would raise again before `_mark_job_interrupted` saves job.json.
  3. Extra 409 on resume when job.json says `running`, no worker task exists, and the file changed less than 30 s ago (a CLI process is running the job, so there must not be a second writer).
  4. Resume idempotency looks only at `run_job` tasks for the job. A queued or running rerender does not block a resume; the resume is queued after it.
  5. Uploads keep a small `upload.json` (the original filename) beside `source<ext>`. The job source becomes `upload:<original name>`. Allowed Content-Types are missing, application/octet-stream, video/* and application/ogg; multipart and anything else get 415. A malformed Range is ignored (full 200). An unsatisfiable Range gets a JSON 416 with `Content-Range: bytes */size` (Starlette would send plain text).
  6. GET /api/settings when settings.json is invalid: 500 with the dotted field errors (PUT can still fix it). PUT validates with `strict=True`, so `"78"` for an int is 422.
  7. `create_app` takes an additive `static_dir=` kwarg for tests. GET / serves a tiny fallback page (still with the token meta) when index.html is missing. HEAD is also accepted on the video and download routes.
  8. An unreadable job.json shows in the list and detail as `failed` / "job.json unreadable". Other routes return 409 for it.
- **Ops note:** `/tmp` (tmpfs, 7.4 G) is 98 % full. About 6.2 G of it is pytest's retained `/tmp/pytest-of-ptl-admin/pytest-*` dirs, which come from the e2e/ffmpeg tests of all builders. A plain `uv run pytest -q` aborted with "No space left on device". I deleted nothing. I ran the suite with `--basetemp` and `TMPDIR` under `~/.cache/autoclipper-backend-pytest/`. The user should decide whether to clear the old pytest dirs.

## Reviewer (api)

I reviewed the step-2 web backend against the API rules and CONTRACT_WEB sections 2-5. I changed no source files. I tested by driving `create_app` in-process with TestClient, a stub worker and a data dir under `~/.cache/autoclipper-review/`, which I deleted afterwards. The real `data/` and Voicebox were not touched.

Overall the backend is solid. Routes I probed came back with the expected codes and the clean `{"message","errors"}` body: bad ids, traversal attempts, NaN/Infinity, `true` for an int, both or neither of url/upload_id, bad Idempotency-Key, Range 206/416, multi-range, query-token scope, Host/Origin guards, 405/404. The defects below are real.

- **API-1 (high) Binding to a non-loopback address hands the token to anyone on the network.** Started with `serve --host 0.0.0.0` (or a LAN IP), `GET /` is public. The Host guard checks only the Host header, which the client chooses, and not the peer address. Proof: a TestClient with peer 192.168.1.99 sent `GET /` with `Host: 127.0.0.1:8765`. It got 200 and the real token in the meta tag. `GET /api/settings` with that token then returned 200. The CLI warning "the API is token-protected but reachable from the network" is therefore false. In the same mode, a request with the real Host (`192.168.1.20:8765`) gets 403 "forbidden host", so only spoofed requests work. Fix: when the bind is not loopback, either refuse to inject the token into `/` (or refuse to serve `/`) for non-loopback peers (`scope["client"]`), or require the token to be typed in. Fix the warning text, and add a test with a non-loopback `client=`.
- **API-2 (medium) Some malformed JSON bodies get 400, not 422.** The contract says malformed JSON is 422. FastAPI turns any non-`JSONDecodeError` from `json.loads` into `HTTPException(400, "There was an error parsing the body")`. Proof: `POST /api/jobs` with `{"url":"https://a.b/x","options":{"clips":<5000 digits>}}` gives 400. So does `{"url":[[[[...100000 deep]]]]}` (RecursionError), and `PUT /api/settings` with a 5000-digit int. Fix: in `_http_error`, map a 400 with that detail to 422 `{"message":"validation failed","errors":{"body":["invalid JSON"]}}`. Add tests for both cases.
- **API-3 (medium) `PUT /api/settings` saves values that can never work.** Free fields have no format or bounds check. Proof (all returned 200 and were written to settings.json):
  - `voicebox.url = "file:///etc/passwd"`. The typo `"127.0.0.1:17493"` (no scheme) is also accepted, after which every voice stage and `/api/voicebox/profiles` fail.
  - `captions.margin_v = -5` and `= 10**30`. Both go into the ASS header.
  - A 100 000-char `llm.model`, and `captions.primary_color = ""`.

  Fix: validate in the API layer, as for jobs (CONTRACT_WEB section 8 lets the product builder own config.py): voicebox.url must be http/https with a host, `margin_v` must be in 0..height, colours must match `^&H[0-9A-Fa-f]{8}$`, and free strings need max lengths. Add 422 tests for each.
- **API-4 (low) A job URL with an invalid port passes validation.** `POST /api/jobs {"url":"http://a.b:99999/x"}` gives 201 and a queued job, which only fails later in yt-dlp. `_check_url` never reads `urlsplit(url).port`, which is the call that raises ValueError for out-of-range or non-numeric ports. Fix: read `parts.port` inside the try and return 422 `errors.url`.

Checked and OK: path params use regex and int bounds, so `..`, `%2e%2e%2f` and huge clip numbers get 404/422. `/static` resolves the path and checks the parent. The upload filename blocks `/`, `\`, NUL, `..` and control characters, a duplicate `filename=` query is caught, and the extension comes from the allow-list. Uploads stream with both a Content-Length check and a streamed-byte 413, and a partial file is removed on error. `?token=` works only on the media GET/HEAD routes. Idempotency replay gives 200, a different body gives 409, and keys persist. A rerender while one is queued returns the same task. Errors never echo the input or a traceback.

## Reviewer (worker)

Lens: worker and concurrency (job.json single writer, queue, rerender vs run, shutdown, crash recovery, leaks, sync vs async handlers, slow Voicebox). No project source edited. Repros ran from the scratchpad with `AUTOCLIPPER_DATA_DIR` set to a temp dir and stub `run_job`. The real `data/` was not touched.

- **WORKER-1 (medium, proven) After a stop and a quick restart, a job can stay "queued" (or "running") forever with no task and no Resume button.** `Worker.stop()` cancels only the running task. Queued `run_job` tasks are dropped from memory, and their job.json stays `queued`. `_loop` also returns before it handles `_finalize`. On restart, `recover_interrupted` skips any job.json changed less than 30 s ago, and it only runs at startup. Repro: submit A and B, `stop()`, then a new `Worker.recover_interrupted()`. Result: A is failed/interrupted, B is `queued`, `recovered: []`, `active_task_for(B)` is None. `app.js` has `canResume = !d.task && (status=='failed' || interrupted || failed>0)`, which is False for B. The UI shows "queued" until a later restart that comes more than 30 s after B's last write. A hard kill or a 10 s join timeout during an LLM call (see WORKER-2) leaves a `running` job in the same state. Fix: in `stop()`, after the join (worker = writer), mark every still-queued `run_job` job interrupted with `_mark_interrupted(only_if=("queued",))`, and run `_finalize` before returning. Also show Resume for `queued`/`running` jobs that have no task, since the API already allows resume for `queued`, and for `running` after 30 s. Another option is to re-run recovery lazily (for example from GET /api/jobs/{id}) once the 30 s have passed.
- **WORKER-2 (medium) The 30 s mtime heuristic marks a live CLI job interrupted, and Resume then gives two writers.** Several pipeline spans never write job.json: the moments LLM call (`rep.start("moments")`, then `run_moments` with no progress), the whisper model load or first-run download before the first segment, `extract_audio_for_asr`, the per-clip `write_script` and `lengthen` LLM calls, and Voicebox `generate`. Each can last more than 30 s. Scenario: `autoclipper run <url>` in a terminal is on the moments call. The user starts `autoclipper serve`, and `recover_interrupted` marks that job failed/"interrupted". The UI offers Resume, and the resume 409 guard only covers `status=="running"`. The worker then runs run_job on the same job while the CLI still runs it: two job.json writers and two ffmpeg/Voicebox runs into the same outputs. The CLI later overwrites the status. Fix: use a real per-job ownership marker instead of mtime. For example, a `job.lock` with pid and hostname, written by both CLI `run` and the worker around `run_job`/`rerender`, removed in `finally`, and treated as stale only when the pid is dead. Or heartbeat-touch job.json from a timer while a stage runs.
- **WORKER-3 (low, plausible) Ctrl-C on `serve` records the current clip as a normal ffmpeg failure, and the pipeline starts the next clip before the cancel arrives.** SIGINT reaches the whole foreground process group, so the ffmpeg child dies at once. The worker thread gets no KeyboardInterrupt, though. It sees `FFmpegError` (an `Exception`), so `_process_clip_lang` stores `render: ffmpeg ...` (or `_job_stage_cut` records a cut error and moves on to the next clip), and the next ffmpeg or Voicebox generation starts. Only later does uvicorn finish its graceful shutdown, call `worker.stop()`, and set the flag. Result: the clip-lang shows as "failed" with a misleading ffmpeg error instead of "interrupted", and extra work is started and then killed. Fix: in `_execute`, after a task finishes with an Exception while `_stopping` or the cancel flag is set, report it as cancelled/interrupted. Or have `cmd_serve` set the worker's stopping/cancel flag from a SIGINT handler before uvicorn's graceful wait. ffmpeg.py is frozen, so `start_new_session` cannot be added there in step 2.
- **WORKER-4 (low, proven) The task table and cancel events grow without bound.** `_tasks` is never pruned, and `cancel_ex` on a queued task never pops `_cancel[task.id]`. Repro: 500 queued-then-cancelled resumes leave `tasks kept: 501, cancel events kept: 500`, while `recent` stays at 20. Fix: pop `_cancel` in the queued-cancel path, and drop a finished task from `_tasks` when it falls out of `_recent`, or keep a bounded LRU. GET /api/tasks/{id} can then return 404 for evicted tasks.

Checked and OK: only the worker thread writes existing job.json files. Request threads read through `read_job` (atomic replace, retry on parse error), and `submit_job` is the only create. `exclusive()` holds the worker lock, so no task can start during a script PUT, and it checks `_running` under the same lock (no TOCTOU). Rerender is 409 while a run_job is queued or running. A queued rerender for the same key is reused, and a running one does not block a new one. Resume re-checks under the lock, so two concurrent resumes give one task. Cancel-then-resume before finalize is safe (`_safe_finalize` skips when a run_job is active). TaskCancelled before the pipeline's own handlers, and the raise-once `sys.exc_info` guard, both still end with job.json failed/interrupted. A failing task does not kill the thread. Idempotent job creation is serialized by `idem_lock`. The upload handler is async, with writes in the threadpool. The Voicebox profiles route and the doctor route are sync or threadpool with a 10 s timeout, so a slow Voicebox does not block the event loop. During a task, Voicebox polling calls `on_progress` on every poll, so cancel works there. GuardMiddleware is pure ASGI, so streaming bodies work.

## Reviewer (ux)

Browser review: headless Google Chrome via playwright-core (in `~/.cache/autoclipper-ux-review`, outside the repo). I ran two `autoclipper serve` instances. The first (:18765) used the real data dir with GETs only. The second (:18766) used `AUTOCLIPPER_DATA_DIR` pointed at a scratch copy of the sample job (id `20261122-999999-uxcopy`, also under `~/.cache/autoclipper-ux-review`), and all edits and settings saves went there. Both servers are stopped. The real job and data/settings.json have the same md5 as before, no job was created and Voicebox is still running. No re-render was triggered, because the copied voice.json has absolute paths into the real job. Screenshots are in `~/.cache/autoclipper-ux-review/shots/`.

**Works:**
- No console errors or page errors on any route.
- The jobs list and detail show the sample job.
- The review video plays: readyState 4, 1080x1920, 206 Range.
- Devanagari renders correctly in the narration, title, description and hashtag chips, and in the burned-in captions.
- Save writes script.json and shows voice_stale.
- The unsaved-changes guard works on the clip and settings pages.
- Settings round-trip: font_size, wps_hi and whisper.language "hi" then back to null are saved on disk. A 422 lands on its field. Empty and decimal ints are caught on the client.
- The slider can be moved with the arrow keys.
- At 360 and 320 px (also in dark mode) there is no horizontal scroll on any page.
- The focus ring is visible, and the theme toggle and log `<details>` work from the keyboard.

**Defects:**
- **UX-1 (medium):** the hashtag limit does not match. The clip editor says "up to 30", previews 30 chips and validates 30 on the client. ClipScriptIn also accepts 30. But `normalize_script` keeps only 8 (`MAX_HASHTAGS = 8`), and the toast just says "Saved.". Proven: I entered 12 tags and only #टैग1-#टैग8 were saved to script.json; 4 tags were dropped without notice. Fix: make the UI hint, client check and server model say 8, or return 422 above 8. Or the toast could report dropped or merged tags, as it already does for the shortened title.
- **UX-2 (medium, a11y):** the "Skip to content" link (`href="#main"`) goes through the hash router. Pressing it on any page shows "Page not found", which loses the page. With unsaved edits it brings up the "Leave without saving?" dialog. Proven in a screenshot (`k_skiplink.png`). Fix: a click handler that calls preventDefault and runs `main.focus()`, or have route() ignore `#main`.
- **UX-3 (low, a11y):** on New job, after the "Choose file…" button there is a second tab stop on the visually hidden 1x1 `<input type=file>`. Its focus ring is invisible. Fix: give the input `tabindex=-1`, because the button already opens the picker.
- **UX-4 (low):** Revert on the clip editor does not call `updateButtons()`. The primary button keeps saying "Save & re-render" after Revert, even though nothing is dirty and the "Unsaved changes" label has cleared.
- **UX-5 (low, privacy):** the video and download URLs carry `?token=`, so uvicorn's access log prints the full auth token on every video request (8 lines in a short session). Users paste terminal output into bug reports. Fix: filter the query out of the access log, or use `access_log=False` / a log filter for `token=`.
- **UX-6 (low, a11y):** form errors add a CSS class but never set `aria-invalid="true"` on the control, on the clip editor or in settings. Errors are still announced through `role=alert` and `aria-describedby`, so this is minor.

**Note (not a defect):** saving settings unchanged writes every default (for example script.*, web.*, audio.loudnorm) into settings.json, so those values no longer follow future default changes. This is as the contract specifies (PUT of the whole object). I am only mentioning it.

## Team Lead (step 2 review triage)

I checked every finding against the code myself (auth.py `_reject`/index(), errors.py `_http_error`, server.py `_check_url`/`put_settings`/ClipScriptIn, worker.py `stop`/`_loop`/`cancel_ex`/`recover_interrupted`, app.js route/FormErrors/clip editor). I also confirmed in Python that `urlsplit('http://a.b:99999/x').hostname` succeeds and only `.port` raises, and that `json.loads` on a 5000-digit int raises a plain ValueError, not JSONDecodeError. FastAPI routing.py maps that to 400 "There was an error parsing the body". **All 14 findings are accepted.** None is a false positive.

**Backend (auth.py, errors.py, server.py, worker.py, cli.py, tests/test_web_*):**
- API-1 (high): index() injects the token for any request whose Host header passes, and Host is chosen by the client. Fix: in `index(request)`, inject the token only when `request.client.host` is loopback (`is_loopback`). Otherwise replace the placeholder with "" so the existing "no access token" banner shows. Change the cli.py warning to: "binding <host>: the UI token is only given to loopback clients; other machines cannot use the app". Tests: `TestClient(app, client=("192.168.1.99", 1))` with `Host: 127.0.0.1:8765` must not contain the token. The loopback-peer case still gets it.
- API-2: in `_http_error`, map status 400 with detail "There was an error parsing the body" to 422 `{"message":"validation failed","errors":{"body":["invalid JSON"]}}`. Tests: a 5000-digit int and 100000 nested `[` on POST /api/jobs and PUT /api/settings must return 422 with that exact shape.
- API-3: in put_settings, after model validation, add these checks with dotted 422 errors and no write: `voicebox.url` http/https with a host, and port valid if given; `captions.margin_v` 0..clips.height; `captions.primary_color`/`highlight_color` match `^&H[0-9A-Fa-f]{8}$`; max lengths on free strings (llm.model, api_key_env, font names, whisper.* strings, profile ids ≤ 200; urls ≤ 2048). Before adding the colour regex, check that the current defaults pass it. Tests must cover each field and assert settings.json is unchanged on 422.
- API-4: in `_check_url`, evaluate `parts.port` inside the try. Tests: `:99999` and `:abc` -> 422 errors.url, and no job dir created.
- UX-5: remove the token from uvicorn access logs. Add a `logging.Filter` on `uvicorn.access` that rewrites `token=[^&\s"]+` to `token=***` in record.args, installed in cmd_serve. Test the filter function directly.
- Builder (frontend) request: add `Cache-Control: no-cache` to /static/* responses, and let the static route accept HEAD (currently 405). Add a test for each.
- WORKER-1: in `stop()`, after join (if the thread exited), mark every still-queued run_job's job interrupted with `_mark_interrupted(only_if=("queued",))` and drain `_finalize`. In app.js, canResume must also be true for `queued`/`running` jobs with no `d.task` (the resume 409 guard still protects a live CLI). Test: submit A+B, stop(); B's job.json must be failed/interrupted.
- WORKER-2: replace the 30 s mtime heuristic with a `job.lock` file `{pid, host, started}` in the job dir. The worker writes it in `_execute` and the CLI writes it in `cmd_run`/`cmd_rerender`; both remove it in finally. recover_interrupted and the resume/rerender 409 guards treat a lock as live when the host matches and `os.kill(pid, 0)` succeeds. A lock from a dead pid is stale and is overwritten. Keep RECOVER_MIN_AGE only as a fallback when no lock exists. Tests: a live-pid lock blocks recovery and resume (409), and a dead-pid lock does not.
- WORKER-3: in `_execute`, if the task ends (Exception or normal return with failures) while `_stopping` or its cancel flag is set, set the task to cancelled and mark the job interrupted with `_mark_interrupted(only_if=("running",))`. In cmd_serve, also set the worker's stopping flag first through uvicorn's shutdown hook. Test with a stub run_job that raises after stop() sets the flag.
- WORKER-4: pop `_cancel[task.id]` in cancel_ex's queued branch, and evict finished tasks from `_tasks` when they drop off `_recent` (in both places that appendleft). Test: 500 cancel cycles leave len(_tasks) ≤ history+queue and len(_cancel)==0, and an evicted id gets 404 from GET /api/tasks/{id}.
- Sign-offs: approved are the queued-cancel finalize -> interrupted behaviour, the raise-once TaskCancelled guard, and the listed additions beyond the contract (resume 409, upload.json, Range/416, settings 500, static_dir kwarg, HEAD). Add them to docs/CONTRACT_WEB.md.

**Frontend (app.js, index.html):**
- UX-1: use the hashtag limit 8 everywhere. Hint "up to 8", `list.slice(0, 8)`, validateClient `> 8` -> "At most 8 hashtags.". Backend: ClipScriptIn.hashtags `max_length=MAX_HASHTAGS` imported from stages/script.py, plus a 422 test for 9 tags. Also, after save, if the returned hashtags differ from what was sent (dedupe/merge), say so in the toast, the same way the shortened-title toast does.
- UX-2: add a click handler on `.skip-link` that calls preventDefault() and `document.getElementById('main').focus()`. Also make route() and the hashchange listener ignore `#main` (restore lastHash, no re-route, no guard).
- UX-3: give `#nj-file` the attribute `tabindex: '-1'`.
- UX-4: call `updateButtons()` at the end of fillForm(), which also covers the post-load case.
- UX-6: in FormErrors.show() set `aria-invalid="true"` on the first input/select/textarea of each matched container, and remove it in clear().
- Manual check still open: Stop on a running task from the queue panel. Do it in the next UX pass on a scratch data dir with a stub/short job.

**Product:**
- allow_lengthen = `regenerated or not voice.json exists` in run_job: approved. It protects user-edited narration on resume. Document it in README/CONTRACT.
- The two changed step-1 tests are approved; the old assertions described behaviour that is now replaced.
- README intro line 7 and the paragraph on long voice-overs: the product builder now owns those lines and should update them to the step-2 behaviour (ducking, lengthening).
- git stash/pop while others were editing: backend and frontend must each diff their own tracked files (cli.py, TEAM_LOG.md) against what they last wrote, and re-apply anything lost. Nobody stashes again while builders are active.

**Not a code issue, user decision:** /tmp (tmpfs 7.4G) is ~98% full, with ~6.2G in old `/tmp/pytest-of-ptl-admin/pytest-*`. Nobody deletes it. Until the user decides, run the suite with `--basetemp` (and TMPDIR) under `~/.cache/autoclipper-*-pytest/`.

## Fixer (step 2 review fixes)

All 14 review findings and the 4 lead items are applied. I fixed code, not tests, except for the 4 test lines below. Nothing is committed and I did not use git stash. The real data/, the sample job and Voicebox were not touched.

- **API-1:** `index(request)` injects the token only for a loopback peer (`is_loopback(request.client.host)`) and puts "" in the meta tag for anyone else. The cli.py warning now says "the UI token is only given to loopback clients; other machines cannot use the app".
- **API-2:** `_http_error` turns 400 "There was an error parsing the body" into 422 `{"message":"validation failed","errors":{"body":["invalid JSON"]}}`.
- **API-3:** `put_settings` calls `_settings_value_errors` and writes nothing on any error. It checks:
  - `voicebox.url`: http/https, a host, a valid port, at most 2048 chars;
  - `margin_v`: 0..clips.height;
  - colours: `^&H[0-9A-Fa-f]{8}$` (both defaults pass);
  - at most 200 chars: llm.model, llm.api_key_env, font_en/font_hi, whisper.*, and voicebox english/hindi profile_id and engine.
- **API-4:** `_check_url` reads `parts.port` inside the try.
- **UX-1:** `ClipScriptIn.hashtags` uses `max_length=MAX_HASHTAGS` (8, from stages.script). In app.js, `MAX_HASHTAGS = 8` sets the hint, the chip preview and the client check. The save toast now says when the server changed the hashtags.
- **UX-2:** a `.skip-link` click handler calls preventDefault and focuses `#main`. On hashchange, `#main` restores lastHash with no guard or route. A first load on `#main` goes to `#/jobs`.
- **UX-3:** `#nj-file` has tabindex -1.
- **UX-4:** fillForm() calls updateButtons().
- **UX-6:** FormErrors sets and clears aria-invalid.
- **UX-5:** `auth.TokenRedactFilter` / `install_access_log_filter()` on `uvicorn.access`, installed in cmd_serve. I checked that it survives uvicorn's dictConfig.
- **Static:** `/static/*` accepts GET and HEAD and sends `Cache-Control: no-cache`.
- **WORKER-1:** `stop()` = `begin_stop()` + join. If the thread has exited, it drains `_finalize` and cancels every still-queued task ("server stopped"), and a queued run_job's job.json goes to failed/interrupted. app.js `canResume` is also true for queued/running jobs with no task.
- **WORKER-2:** new `autoclipper/joblock.py`: `job.lock` `{pid, host, started}`, `hold_job_lock`, `live_lock`, and `pid_alive` (os.kill(pid, 0) on POSIX, OpenProcess on Windows, because os.kill(pid, 0) sends CTRL_C on Windows). Who uses it:
  - The worker `_execute` holds the lock around run_job/rerender. If another process holds a live lock, it raises `JobLocked`: the task fails and job.json is not touched.
  - cli `run` creates a new job first, the same way the worker does, so the lock covers the whole run. cli `run --job` and `rerender` also hold the lock. A foreign live lock gives exit 1.
  - recover_interrupted, resume and rerender treat a live lock as "another process" (409), unless the worker itself has a task for the job.
  - The 30 s mtime check is used only when there is no job.lock file.
- **WORKER-3:** a task that ends with an Exception or a normal return while `stopping` or its cancel flag is set becomes `cancelled`. Its job.json goes to failed/interrupted (`only_if=("queued","running")`). I added "queued" because a run that never reached the pipeline's own writes would otherwise stay queued. `cmd_serve` passes `create_app(stop_on_signal=True)`. The lifespan then chains SIGINT/SIGTERM in front of uvicorn's handler with `install_stop_hook`, so `worker.begin_stop()` runs as soon as the signal arrives. I kept `uvicorn.run`, so the serve tests still patch it. ffmpeg.py is untouched.
- **WORKER-4:** cancel_ex's queued branch pops `_cancel`. `_remember()` replaces both appendleft calls and evicts a finished task that falls out of `_recent`.
- **Docs:** CONTRACT_WEB.md has a new section 10 that lists every approved addition, the job.lock rules and the allow_lengthen rule, and sections 1, 4.4 and 5 are updated. README intro item 7 and the voice-over paragraph now describe ducking and loudnorm, the one lengthen retry, and when lengthening is allowed. The README also gives the bounds for margin_v and the colour format.
- **Stash check (BUILDER-product-stash):** the backend's tracked cli.py still has the whole serve block (`_port`, `cmd_serve`, parser entry), and TEAM_LOG.md has every builder and reviewer section. The frontend files are untracked, so git stash never touched them. Nothing was lost.
- **Tests changed (they were wrong after API-1):** tests/test_web_auth.py now uses `client=("127.0.0.1", 50000)` for the 3 index tests, because TestClient's default peer "testclient" is not loopback. tests/test_web_worker.py: the network-bind warning test now checks the new text.
- **New tests:** tests/test_web_review_fixes.py has 79 tests, one or more per fix:
  - non-loopback/loopback peers;
  - 5000-digit and 100000-deep bodies on jobs and settings, checking that no job dir is created and settings.json is byte-identical;
  - every settings field and its boundaries;
  - ports;
  - 9 hashtags;
  - the redact filter;
  - static HEAD/no-cache;
  - stop with A+B;
  - live, dead, other-host and garbage locks for recovery, resume and rerender;
  - the lock written by the worker and the CLI;
  - an exception or a normal return after stop;
  - the signal hook;
  - 500 queue+cancel cycles, with a 404 for an evicted task.
- **Suite:** `uv run pytest -q` passed with **712 tests**. I ran it with `--basetemp` and TMPDIR under `~/.cache/autoclipper-fixer-pytest/`, because /tmp is still 98 % full.

## QA co-lead (step 2 review-fix sign-off)

I reproduced every accepted item's failure scenario on my own, outside the fixer's tests. All of them are fixed, apart from the two gaps below, which I fixed myself with tests. Nothing is committed, git stash was not used, and data/, the sample job 20261122-185849-65a6bd and Voicebox (still running) were not touched.

**Confirmed fixed (repro -> now correct):**
- API-1: LAN peers (192.168.1.99, 10.0.0.5, ::ffff:192.168.1.9, "testclient") with a spoofed `Host: 127.0.0.1:8765` get an empty token. Loopback peers (127.0.0.1, ::1, and ::ffff:127.0.0.1 for a dual-stack bind) get it.
- API-2: a 100000-deep body and a 5000-digit int give 422 `{"body":["invalid JSON"]}` on POST /api/jobs (even with an Idempotency-Key, and no job dir is created), on PUT /api/settings, and also on PUT .../script, where script.json stays byte-identical.
- API-3/API-4: bad voicebox URLs, margin_v checked against a `clips.height` changed in the same PUT (1000 is accepted, 1001 is 422), bad colours, and ports :99999/:abc/:-1. All give 422 with the dotted key, and the file stays byte-identical.
- UX-1: 9 tags give 422 and 8 are accepted and written.
- UX-5: a record shaped exactly like uvicorn's access log is redacted. In the live server log, `?token=***` appears for the video request.
- WORKER-1/3: with A running and B queued, stop() leaves both tasks cancelled, both job.json files failed/interrupted, and no job.lock left behind.
- WORKER-4: 200 queue+cancel cycles through the API leave `_cancel` empty and `_tasks` within the history size, and the evicted id gives 404 with the standard shape.
- Static: HEAD on a file gives 200 with no body and no-cache, and HEAD on a missing file gives 404.
- WORKER-2: the fixer's live, dead, other-host and garbage lock tests cover it. I reviewed `joblock.py`.
- **Browser UX pass** (scratch data dir under my scratchpad, `serve --port 8799`, a fake job; deleted afterwards). These were the fixer's open item:
  - UX-4: Revert turns "Save & re-render" back into "Re-render".
  - UX-6: 9 tags set aria-invalid="true" with "At most 8 hashtags.", and a later good save clears it.
  - UX-1: the hint says "up to 8", and the toast for "a a b" says the hashtags were adjusted.
  - UX-3: `#nj-file` has tabindex -1.
  - UX-2: the skip link focuses #main and keeps `#/new`, and a hand-typed `#main` goes back to `#/new` without re-routing.
  - Ctrl-C (SIGINT) shuts down cleanly. One accidental Save & re-render ran on the fake job. It skipped the voice step and failed at ffprobe on the fake wav, so no Voicebox or LLM call was made.

**Found and fixed by QA (with tests in tests/test_qa_step2_signoff.py):**
- `ASS_COLOR_RE` used `$`, so `"&H00FFFFFF\n"` passed and a newline would have been written into settings.json and the ASS style line. It is now `\Z`. The same fix is applied to `IDEMPOTENCY_RE` (server.py) and to config.py `_EXT_RE` (`".mp4\n"` was accepted as an extension).
- `_check_url` accepted port 0 (`http://a.b:0/x` for jobs, `http://127.0.0.1:0` for voicebox.url). It is now 422 "port must be 1-65535". CONTRACT_WEB.md section 10 is updated for both.

**Suite:** `uv run pytest -q` passed with **754 tests** (712 + 42 new QA tests). It ran with `--basetemp`/TMPDIR under `~/.cache/autoclipper-qa-pytest/`, because /tmp is still 98 % full and that is the user's decision.

**Still open (not a blocker):** `hold_job_lock` reads and then replaces the lock file, which is not atomic. Two processes that start the same job at the same instant could both take it. This is acceptable for one user. An `O_EXCL` create would close it if anyone wants it.

## Team Lead (step 2 sign-off)

**Suite:** `uv run pytest -q` passed with **754 tests** in 165 s. It ran with `--basetemp`/TMPDIR under `~/.cache/autoclipper-lead-pytest/`, because /tmp is still 98 % full.

**Browser walk-through.** I used headless Chrome (playwright-core from `~/.cache/autoclipper-ux-review`) against the live `serve` on 127.0.0.1:8765 and looked at every screenshot. There were no console or page errors and no unexpected 4xx/5xx.
- Jobs list: both jobs show as done with 4 rendered each.
- Sample job detail: the 4 job stages and the per-clip-lang progress bars render, and the log expands.
- Review of clip 01 en and hi: the video plays (readyState 4, 1080x1920, 28.84 s). Devanagari renders correctly in the narration, title, description and chips, and in the burned captions.
- Settings: every section renders, including the new audio/script/web fields and the position slider with its preview.
- Doctor: 0 problems and 1 warning (node v16, which only step 3 needs).
- While the rerender ran, the job detail showed "Task running" with Stop, and the review page showed "Rendering…" with live per-stage percentages and the "Narration changed" hint.
- The server log has `token=***` 13 times and no raw token.

**Real check of the product fixes through the API.**
- I copied the sample job to `20261122-203600-leadqa` (`cp -a`, voice.json paths rewritten to the copy).
- I wrote one fresh hi script for clip 01 with real Claude (`write_script` with `settings.script`): target 85 words, got 87.
- I saved it with `PUT .../clips/1/hi/script` (200, voice_stale) and ran one `POST .../rerender` (202) on the real worker with the real Voicebox. The task was done in 205 s.
- I sent a second POST by accident, milliseconds after the first had started running. As the contract says, it was queued as a new task, so I cancelled it while it was still queued (200 cancelled). No second render ran.

Results (clip 01/hi, clip 28.84 s):

| Check | Old render (sample) | New render (copy) |
|---|---|---|
| Voice length | 18.72 s, about 65 % of the clip | 25.10 s, **87 %** (last word ends 25.08 s; tempo 1.0, not shortened, no lengthen needed) |
| Loudness while narrated (2-20 s) | -17.5 LUFS | -13.9 LUFS |
| Tail loudness (26.2-28.8 s) | -46.9 LUFS | **-25.4 LUFS** |
| Peak | | -1.5 dBFS |

- **The original audio returns after the voice ends.** The tail went from -46.9 to -25.4 LUFS. The cut.mp4 tail on its own is -30.4 LUFS, so the tail now plays the original at full level plus the loudnorm gain.
- **The captions sit below the face.** I extracted frames at 6, 15 and 24 s and looked at them. The line is centred at `\pos(540,1382)` (72 %), on the collar and tie, clear of the face. The Devanagari conjuncts are correct (शब्द, जाएँ, अट्ठाईस).
- The live API negatives were all correct: 404 for clip 9, 422 for lang xx, clip -1 and a 20-digit clip, 401 with no token, and 422 for a whitespace-only narration.
- The original job 20261122-185849-65a6bd is unchanged: the md5 of every file is the same before and after. The copy job stays in data/jobs as the demo of the fixes. Delete it whenever you like.

**Fixes:** no code defects found in this pass, so no code was changed.
- README: added "Usage: Web UI" (how to run, auth, stop) and the running-server note. The Step 1/Step 2 status lines are updated.

**Server left RUNNING for the user:** `autoclipper serve --host 127.0.0.1 --port 8765 --no-browser`. Its details:
- PID **1796838**, whose parent is the `uv run` wrapper, PID 1796830.
- The PID is also in `/tmp/claude-1000/ac_serve.pid`.
- The log is `/tmp/claude-1000/ac_serve.log`.
- Stop it with `kill -INT 1796838`, or `pkill -INT -f "autoclipper serve --host 127.0.0.1 --port 8765"`.

Voicebox is untouched and still running.

**Sign-off: step 2 APPROVED.** Still open, not blocking:
- QA's non-atomic `hold_job_lock`.
- The /tmp cleanup decision, which is the user's.
- A native-speaker check of the Hindi pronunciation.

## Team Lead (step 4 plan: auto-publish)

Worktree `/home/ptl-admin/projects/auto-clipper-step4`, branch `step4-publish` (base 6288baf). Contract: `docs/CONTRACT_STEP4.md`.

**Plan.** English clips go to the en YouTube channel / FB Page / IG account and Hindi clips to the hi ones. The new `autoclipper/publish/` package holds one client per platform behind a `Publisher` protocol. Every Google and Meta call goes through `httpx`, so pyproject needs no new dependency. `settings.publish` defaults: `dry_run` true, every target disabled, YouTube privacy private. Secrets live in `data/secrets/` with mode 0600 and never leave the process. A dry run writes `clips/NN/<lang>/publish/<platform>.json` (the request plan). A live run is a `publish` task on the one worker. It writes `<platform>.status.json`, is idempotent through the published record plus the Idempotency-Key, and resumes interrupted uploads.

**APIs researched (official docs, 2026-11-23).**
- YouTube Data API v3 `videos.insert`, resumable (`uploadType=resumable`, chunks of 256 KiB multiples, 308/Range, `bytes */N` probe). Quota is 100 inserts a day. A Short is vertical or square and at most 3 min.
- Graph **v27.0** (latest, 2026-11-04). FB `/{page}/video_reels` start -> rupload `video-upload/v27.0` -> finish PUBLISHED. Limits: 3-90 s, 30 a day per Page.
- IG `/{ig}/media` REELS `upload_type=resumable` -> rupload `ig-api-upload/v27.0` -> poll `status_code` -> `media_publish`. Limits: 3 s-15 min, 300 MB, 100 posts a day.
- Google loopback OAuth with PKCE. Refresh tokens expire after 7 days while the app's consent screen is in "Testing".

**Split (no overlapping files; the agreed interface is `publish/service.py` + Worker.submit_publish + the JSON shapes + `tests/publish_fakes.py`).**
- **core:** `autoclipper/publish/*`, one import + one field in `config.py`, the publish task kind in `web/worker.py`, the one-line subcommand registration in `cli.py`, `tests/publish_fakes.py`, `tests/test_publish_*.py`. Deliver the settings model, `service.py` stubs and the fakes first, and log "interface ready".
- **web:** `web/publish_routes.py`, minimal hooks in `server.py`, `static/publish.js`, `static/publish.css`, small hooks in `index.html` and `app.js`, `tests/test_web_publish*.py`, `README.md`.

**Merge note.** Step 3 edits config.py, cli.py, server.py, app.js, styles.css and README in the main checkout. Keep our edits to those files to small insertions, so merging later stays easy.

**Hard rules repeated.** Never post anything and never call a real social or Google API: tests use MockTransport. No real OAuth and no account creation. Run pytest with `TMPDIR=~/.cache/ac-step4` and `--basetemp ~/.cache/ac-step4/bt-<role>`. For manual runs use `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data` and port 8767 only. No commits.

## Web builder (step 4: publish routes, UI, README)

**Started.** Building `web/publish_routes.py`, `static/publish.js`, `static/publish.css`, `tests/test_web_publish*.py` and the README section against CONTRACT_STEP4 sections 7-9 and 11. `autoclipper/publish/` does not exist yet, so the routes are written against the agreed `service.py` signatures. I will wait for "interface ready" before running tests, and I will not write core code.

**Requests to core (additive, please confirm):**
1. `complete_youtube_connect` could add `"lang"` to the returned AccountStatus dict (or expose the pending state's lang). The callback page then says "Connected <channel> for Hindi". The routes otherwise fall back to "Connected <channel>".
2. `Task.to_dict` gaining `"platforms"` changes the exact-key `TASK_KEYS` in `tests/test_web_support.py` (and `assert_task`'s kind list). That file is shared step-2 test support. Please update it with the worker change, or tell me and I will do it.

## Core builder (step 4: publish package, clients, worker task, CLI)

**Interface ready.** `autoclipper/publish/` exists with the final `service.py` signatures and real implementations (not stubs): settings, secrets, accounts, oauth_google, base, metadata, records, youtube, facebook, instagram, runner, service. `config.py` has its one import + one field. `web/worker.py` has the `publish` kind, `Task.platforms`/`Task.force`, `submit_publish`, `active_publish`, `exclusive_publish`. `tests/publish_fakes.py` is ready: `FakeNet` (FakeGoogle + FakeGraph as one MockTransport handler, `.client()`), `make_job()`, `connect_all()`, `connect_youtube/connect_meta(lang)`, `set_publish(**kw)`, `client_secret_json()`, `no_network_client()`, `grep_secrets()`, `tree_text()`, `LogSink`, fake token constants `ALL_SECRETS`, injection via `net.google.inject["chunk"] = [response, ...]` / `net.graph.inject["fb_start"] = [...]`.

**Answers to web:**
1. `complete_youtube_connect` now returns the AccountStatus plus `"lang"` (additive).
2. I updated `tests/test_web_support.py`: `TASK_KEYS` gains `"platforms": (list, None)`, `assert_task` accepts kind `publish` and checks that platforms is null exactly for non-publish tasks.
3. **Bug in your `publish_routes.py`**: pydantic `Field(pattern=r"\A...\Z")` crashes at import (the Rust regex engine has no `\Z`; it made `import autoclipper.web.server` fail). Use `^...$` in `pattern=` (Rust `$` matches only at the very end, so no `\n` loophole) or a `field_validator` with Python `re` + `\Z` like `publish/settings.py` does.

**Deviations from the contract (please note, lead):**
- Resume state (YouTube session URI, FB video id, IG container id) is NOT in `<platform>.status.json`: section 0 says a resumable session URI is a secret and must never be written to a job dir. It lives in `data/secrets/resume_<job>_<NN>_<lang>_<platform>.json` (0600) and is deleted after success. The status file therefore never has a `resume` key; `records.public_status` strips everything outside the contract keys anyway.
- `service._update_settings(fn)` / `service.save_publish_settings(ps)` (extra helpers) replace only the `publish` section of settings.json atomically.
- YouTube connect fills `settings.publish.<lang>.youtube.channel_id` only when it is empty (so a wrong channel picked in Google's chooser shows as "settings channel differs from the connected one" instead of silently remapping). Meta connect always sets `page_id` (and `ig_user_id` when given), since the user typed them.
- `disconnect facebook` keeps Instagram only when it has its own `ig_token`; otherwise the whole meta record goes. Meta tokens are not revoked remotely (no API for a Page token; revoke only applies to YouTube).
- Force re-publish appends the old published record to `<platform>.history.jsonl` before overwriting.

**Web builder: done (2026-11-23).** Full suite: `TMPDIR=~/.cache/ac-step4 uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-step4/bt-web` passed with **1076 tests**. 151 of them are mine: 103 in `tests/test_web_publish.py` and 48 in `tests/test_web_publish_clip.py`. Nothing was posted, no real API was called and nothing was committed.

- **Routes.** `autoclipper/web/publish_routes.py` registers every route in section 9 through `install_publish_routes(app, state, publish_http=...)`. `server.py` change is three small edits: the import, the `publish_http` kwarg and the install call.
  - Service exceptions map to errors as follows: PublishInputError->422, PublishConflict->409 (with per-platform `errors`), PublishNotFound->404, PublishUpstreamError->503.
  - `PUT /api/publish/google-client` streams the body and stops at 64 KiB with 413.
  - YouTube connect builds `redirect_uri` from the request Host, and returns 409 when that Host is not loopback.
  - `/oauth/google/callback` returns HTML in every case. Headers: no-store, no-referrer and the same CSP; the page has no script and the service message is escaped.
  - An `OAuthQueryRedactFilter` on `uvicorn.access` turns `code=` and `state=` in the logged URL into `***`.
  - Publish POST checks run in this order:
    1. Idempotency-Key format (422).
    2. Clip 404.
    3. For a live publish, a missing key or `confirm` -> 422.
    4. `dry_run:false` while settings say dry run -> 409.
    5. No final.mp4 -> 409. A live publish also needs metadata.json.
    6. Dry run: runs under `worker.exclusive_publish` and returns 200 `{dry_run, plans}`.
    7. Live: runs under `idem_lock` as `publish:<key>`, with the same replay and 409 rules as step 2. Then `_check_no_other_owner`, then `check_live`, then `submit_publish` -> 202. Nothing left to send -> 200 with `task: null` and `already_published`.
  - `resume` is stripped from every status that leaves the API.
  - `JobDetail...langs[lang].publish_url` is added by wrapping `state.detail` inside `install_publish_routes` (server.py's `detail()` is untouched).
- **UI.** The two new files are `static/publish.js` (`window.ACPublish = {viewAccounts, panel}`) and `static/publish.css`. index.html gets 2 tags and the nav link `#/accounts`. app.js gets one line each for:
  - the route;
  - the `window.ACApp` export;
  - the `ACPublish.panel(...)` hook in `viewClip`;
  - `taskDesc` for `publish`;
  - skipping the `publish` section on `#/settings` (still kept on save via deepClone).
- **Browser check.** Port 8767 with `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data`, using a copy of the sample job and the FAKE accounts from publish_fakes; the server is stopped now.
  - The Accounts page and the mapping table render. Devanagari names show correctly.
  - The Hindi clip dry-run preview shows the full plan (the YouTube plan has 3 requests) with tokens as `***`, and the toast reads "Nothing was posted".
  - The confirm dialog shows each platform -> account, privacy, title, the warning "This posts publicly to … on Facebook", and the force box that needs "again" typed. I opened it with dry run off and pressed **Cancel**. I never pressed "Publish now". Dry run was turned back on, and no status file or publish task exists.
  - A bad category id comes back as a field error.
  - At 375 px nothing overflows on the Accounts, clip or Settings pages. There were no console errors and no server errors.
- **Shared test edit.** `tests/test_web_jobs.py::test_job_detail_structure` now expects `"publish_url": str` (the contract's additive key). That is a one-line change.
- **README.** Added the section "Publishing (step 4)" before "Data layout". It covers what is posted where, YouTube and Meta credential setup with Linux and Windows commands, where secrets live, and how to disconnect. It is a single insertion.
- **Open.**
  - The callback page says "Connected <channel>" without the language, because `complete_youtube_connect` does not return `lang` (request 1 above, optional).
  - The Instagram Page-token acceptance is still unconfirmed against the real API (lead note).

**Core builder: done (2026-11-23).** Full suite: `TMPDIR=~/.cache/ac-step4 uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-step4/bt-core` passed with **1076 tests**. 103 of them are core tests in `tests/test_publish_{settings_secrets,metadata,clients,service,worker,cli}.py`. Every request goes to `httpx.MockTransport` fakes. Nothing was posted, no real Google or Meta endpoint was called, no OAuth login was done and nothing was committed.
- **Web, re-check your open item 1:** `complete_youtube_connect` does return `"lang"` now, so the callback page can say "for Hindi".
- **Clients:**
  - **YouTube:** token refresh, then a resumable start (`notifySubscribers` from settings; Location must be on googleapis.com), then chunk PUTs. Chunks are `chunk_mb` MiB and each is a multiple of 256 KiB.
    - 308/Range moves the next offset.
    - A 5xx or network error is retried with backoff (Retry-After respected), and a `bytes */N` probe finds where to continue.
    - A 404 starts a new session (at most 2 restarts).
    - A 401 refreshes the token once.
    - After an interrupt, a new run probes the saved session first. If the upload had already finished, it recovers that video id and uploads nothing.
  - **Facebook:** start, then rupload, resuming from `bytes_transfered` after a transient failure. Then finish PUBLISHED, then poll `status` until publishing is complete or an error state appears. A timeout becomes a `processing` error that says the reel may still appear.
  - **Instagram:**
    1. `content_publishing_limit`, checked live (a full quota sends nothing else).
    2. REELS container.
    3. rupload.
    4. Poll `status_code` every `poll_interval_seconds`, up to `poll_timeout_seconds`.
    5. `media_publish`.
    6. permalink.
    - A FINISHED/IN_PROGRESS container younger than 23 h is reused on resume.
- **Error kinds and their messages:** `auth` ("… login expired or was revoked - reconnect on the Accounts page"), `quota` ("YouTube daily upload quota used up; it resets at midnight Pacific time"), `rate_limit`, `unsupported_video`, `processing`, `transient`, `network`, `invalid`, `unknown`. Every message goes through `redact` (known secret values, `Bearer/OAuth <token>`, `*_token=`, `upload_id=`).
- **Manual check:** `autoclipper publish 20261122-185849-65a6bd --clip 1 --lang hi --platform all`, with `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data` on a copy of the sample job. It ran ffprobe on the real final.mp4 (11156919 bytes, 28.84 s, 1080x1920), printed and saved the 3 plans, sent nothing, and exited 0.
- **Fixed during the run:**
  - The `OAuth <token>` redaction pattern was masking prose ("Google OAuth client" became "Google OAuth ***"). It now needs a token-like word of 8 or more characters that includes a digit or punctuation.
  - The IG "uploaded" resume flag is now saved before the next cancel point.
- **Known limits (not blockers):**
  - `media_publish` and FB `finish` are retried on network errors like any request. If the first call succeeded but its answer was lost, the retry fails with an "invalid" error even though the post went out. The message does not say so; the user would need to check the account before using `force`.
  - The Instagram Page-token acceptance is still unconfirmed against the real API (lead note). The connect form takes a separate IG token.

## Security reviewer (step 4)

Reviewed the step 4 diff for secret leakage, file permissions, OAuth state/loopback, test network isolation, publish gating (confirm/dry_run), idempotency and path traversal. Gating, OAuth state (single use, PKCE, loopback-only redirect), redaction and path validation look sound. The real findings:
1. HIGH, instagram.py: when a resumed container reports status_code PUBLISHED, the code creates and publishes a new container, so the reel is posted twice. Reproduced with MockTransport: the resume record had uploaded=True, no media_id, and the container was PUBLISHED.
2. MEDIUM, runner.py: the resume record is cleared on any non-transient error, even one raised after the post already went live. Example: the Facebook status poll gets a rate_limit or auth error after finish. The next Publish then starts a new reel.
3. MEDIUM, secrets.py and README: on Windows, os.chmod does not restrict access, and data/ sits in the project folder rather than the user profile, so the "user-only ACL" claim does not hold.
4. LOW, app.js settings page: PUT /api/settings sends back the stale `publish` snapshot, which can undo a dry_run=true set on the Accounts page.

## API + correctness reviewer (step 4)

**Verified.** The full suite passed: 1076 tests (`--basetemp ~/.cache/ac-step4/bt-review`).

On port 8767 with `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data` and the FAKE accounts:
- Accounts and the hi dry-run worked, and the Devanagari metadata was correct.
- The 401/403/404/422 matrix behaved as documented, and so did the callback with a bad state (400 HTML).
- The UI publish panel rendered.
- Nothing was posted. The server is stopped, and the server log has no token.

Against the official docs (fetched 2026-11-23):
- YouTube `videos.insert`: the endpoint, 1 unit in the uploads bucket (100/day), title 100 chars, description 5000 bytes, the tag rule, and `containsSyntheticMedia` being writable all match.
- FB `video_reels` and IG REELS resumable (Page token on graph.facebook.com, rupload `ig-api-upload`) match too.

**Findings:**
1. HIGH. A lost response on IG `media_publish` or FB `finish` leads to a duplicate post on the next publish. Http.request retries the non-idempotent POST. The retry's non-transient error is then classed `invalid`. runner clears the resume record and the status is `failed` (not published). A normal re-publish, with no force needed, creates a new container/reel. Reproduced with the fakes: run 2 made a 2nd container and media.
2. MEDIUM. The `#/settings` save sends back a stale `publish` section from deepClone(loaded). PUT /api/settings then overwrites it without `service._settings_lock`. This can silently revert dry_run, enabled flags or the channel/page ids set by connect.
3. LOW-MEDIUM. Publish is allowed while voice_stale. The narration was edited but not re-rendered, while metadata.json is already rewritten. The post would pair the new title with the old narration, and there is no problem or warning.
4. LOW:
   - FB finish does not send `is_ai_generated` (YouTube does disclose the synthetic media).
   - `uploadLimitExceeded` is reported as the API quota "resets at midnight Pacific", but it is the channel's upload limit.
   - IG polling defaults (10 s for up to 600 s) exceed the docs' "once per minute, no more than 5 minutes".

## Lead (step 4 triage of security + API reviews)

**Result.** All 10 findings ACCEPTED (SEC-1 and F1, SEC-2, and SEC-4 and F2 are the same bugs, so each pair got one fix). Fixed with regression tests in `tests/test_publish_lead_fixes.py` (18 tests). Full suite: `TMPDIR=~/.cache/ac-step4 uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-step4/bt-lead` passed with **1094 tests**. Nothing was posted, no real API was called (MockTransport only), no server was started and nothing was committed.

**Decisions:**
- **SEC-1 + F1 + SEC-2 (HIGH, duplicate posts): accepted.** I confirmed all three paths against the code. They share one root cause: the resume record was dropped, or ignored, after the non-idempotent publish request may already have gone out. Fix (a "commit point"):
  - `base.Http.request(..., retries=0)`: IG `media_publish` and FB `finish` are sent exactly once and never auto-retried.
  - Before sending either request, the resume record gets `"sent": true`. `records.resume_sent()` lets `runner.run_publish` keep the record on every error once it is set. Only success or `force` clears it, and `force` drops a `sent` record before posting anew. The failure message then adds "the post may already be live; publishing again checks … first and does not post twice".
  - **IG:** on any `media_publish` error, it reads the container `status_code`. On resume, it checks any stored container that is `sent` or `uploaded`, at any age and for any render. `PUBLISHED` means the reel is live and is never re-published. The media id is recovered from `GET /{ig}/media?fields=id,caption,permalink,timestamp` when exactly one post has the same caption and was posted after the container was created. Otherwise the clip is `failed` with error code `maybe_published` and a "check the account; force only if missing" message. A verified FINISHED/ERROR/EXPIRED drops the `sent` flag, because nothing went live.
  - **FB:** on a `finish` error, or on resume with `sent`, it reads `status`. If `publishing_phase` has started (anything but not_started) or video_status is ready/published, it skips `finish` and only polls. A repeated `finish` only ever targets the same video id, and a new `start` never happens without force. A `rate_limit`/`auth` error while polling after finish now keeps the record, so the next Publish just polls (the SEC-2 scenario).
  - The core builder's "known limit" (the lost `media_publish`/`finish` answer) is resolved by this.
- **SEC-3 (Windows ACL): accepted.** On `os.name == "nt"`, `secrets_dir()` runs `icacls <dir> /inheritance:r /grant:r DOMAIN\USER:(OI)(CI)F` once per process, and logs a warning with the command to run by hand if that fails. New files inherit the ACL. The README claim is corrected. Tested with a mocked `subprocess.run`; it is not verified on a real Windows box.
- **SEC-4 + F2 (stale publish section on Settings save): accepted.**
  - `app.js` now deletes `out.publish` before `PUT /api/settings`.
  - `server.put_settings` keeps the stored `publish` section when the body has none, and writes under `publish.service._settings_lock`.
  - A body that includes `publish` is still accepted, as a full replace (step-2 contract kept).
- **F3 (voice_stale): accepted, as a blocking problem rather than a warning.** It posts the wrong narration under a new title. `service.voice_stale()` (same rule as the web view) adds `VOICE_STALE_PROBLEM` to `load_item`'s extra problems. So it appears in plans, `publish_view`, `check_live` (409) and the runner's last-moment check.
- **F4 (FB `is_ai_generated`): accepted.** I checked it against the official reels-publishing doc: an optional boolean on finish, default false. New setting `FacebookTarget.is_ai_generated = True`. It is sent on finish and shown in the plan.
- **F5 (`uploadLimitExceeded`): accepted.** It has its own message ("this YouTube channel has reached YouTube's upload limit for now … not the API quota"), kind still `quota`.
- **F6 (IG polling): accepted.** IG polling starts at `poll_interval_seconds` and doubles to 60 s, with the last sleep capped to the remaining time. The default `poll_timeout_seconds` is now 300 (was 600). That gives at most 8 status GETs per reel instead of 61. The interval default stays 10 s. FB polling also uses the 300 s default: a timeout is `processing` and keeps the record, so publishing again resumes the poll.

**Files touched:**
- `publish/{base,facebook,instagram,runner,records,service,secrets,settings}.py`
- `web/server.py` (put_settings only)
- `web/static/app.js` (one line plus a comment)
- `tests/publish_fakes.py`, additive:
  - `lose[step]` = do the work, then drop the answer
  - a container becomes `PUBLISHED` after media_publish, and a second publish of it is rejected
  - `GET /{ig}/media` listing
- `README.md`: Windows secrets paragraph, plus idempotency and AI-label notes.
- `docs/CONTRACT_STEP4.md`: sections 2.2, 2.3, 3, 4 and the status/resume notes.

**Still open (not blockers):**
- The real Windows ACL run.
- The exact real-world `publishing_phase.status` strings. The code treats anything other than empty/`not_started` as started, which is the safe direction.

## Lead sign-off (step 4)

**Result: APPROVED.** The full suite passed: 1094 tests (`--basetemp ~/.cache/ac-step4/bt-signoff`).

The manual run used port 8767 with `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data-signoff`, a fresh copy of the sample job and FAKE secrets. All six accounts (en/hi x 3 platforms) were enabled, with separate ids per language.
- **Accounts page:** it rendered with dry run ON, the "what is posted where" table, the per-language cards and the Disconnect controls.
- **Clip 01 Publish panel (en and hi, all 3 platforms):** the browser dry-run wrote `clips/01/{en,hi}/publish/{youtube,facebook,instagram}.json`.
  - The en plans used only the en channel, Page and IG account; the hi plans used only the hi ones.
  - The Devanagari title, description and tags were intact.
  - Limits were respected: the hi YouTube title is 43 characters, the description is 538 bytes and the tags total 54.
  - Privacy was as set (en private, hi unlisted). `is_ai_generated` and `containsSyntheticMedia` were both true.
  - Every token was `***`. No token or secret appeared in the plans or the server log.
- **Refusals:**
  - `confirm:true` with dry_run on returned plans only (200) and queued nothing.
  - `dry_run:false` while the settings had dry run on returned 409.
  - With dry run off and nothing connected, the publish returned 409 "not connected" for each platform.
  - A live publish without `confirm` or `Idempotency-Key` returned 422.
  - The rest of the matrix: no token 401, bad token 403, clip 99 404, and lang/platform 422.
- **CLI:** `autoclipper accounts` and `publish --dry-run` worked. The server is stopped and dry_run was set back to true.
- **Notes:**
  - DELETE on a YouTube account revokes at Google by default (`revoke=true`), so a disconnect makes a network call. Use `?revoke=false` or `--no-revoke` for tests with fake tokens.
  - The README's `icacls ... "%USERNAME%"` is cmd syntax. In PowerShell it is `$env:USERNAME`, so it should say "run in cmd".
