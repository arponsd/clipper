# Auto Clipper - Build Contract (Step 2: local web UI)

Owner: Team Lead. Builders implement exactly these names, routes, shapes and limits. Additive extras are
fine; renames/removals need a TEAM_LOG note and lead sign-off. Everything in `docs/CONTRACT.md` still
holds (cross-platform rules, `encoding="utf-8"`, `ensure_ascii=False`, tests never hit the network,
CONTRACT 1a single writer for job.json).

## 0. File ownership (no overlaps)

| Builder | Files |
|---|---|
| backend | `autoclipper/web/__init__.py`, `server.py`, `worker.py`, `auth.py`, `errors.py`; `autoclipper/cli.py` (ONLY the new `serve` subcommand); `pyproject.toml` (deps); `tests/test_web_*.py` |
| frontend | `autoclipper/web/static/index.html`, `app.js`, `styles.css` (nothing else) |
| product | `autoclipper/stages/render.py`, `stages/script.py`, `stages/voice.py`, `stages/captions.py`; prompts in `autoclipper/llm.py`; `autoclipper/config.py` (new settings fields only, section 8); `autoclipper/pipeline.py` (only: lengthen wiring, passing `settings.script` to `write_script`, `allow_lengthen`); `tests/test_product_fixes.py` |
| lead (done) | `autoclipper/interfaces.py` (+`LengthenFn`, +`Lengthener`, `write_script(..., target_seconds=None)`), `tests/fakes.py` (`FakeLLM.lengthen`, `FakeLLM.write_script(target_seconds=None)`) |

`ffmpeg.py`, `models.py`, `jobstore.py`, `voicebox.py`, `stages/ingest|cut|transcribe|moments.py` are NOT
edited in step 2. The product builder may import helpers from `ffmpeg.py` (incl. the private
`_X264`, `_AAC`, `_MONO_TO_STEREO`, `_LIMITER`, `_AtomicOutput`, `_num`) but must not change it.

The backend and frontend builders work in parallel against THIS document only. The settings form is
generated from `GET /api/settings/schema` (section 4.6), so the frontend does not hard-code field lists.

## 1. Process model

- `uv run autoclipper serve [--host 127.0.0.1] [--port 8765] [--no-browser]` runs uvicorn with
  `autoclipper.web.server.create_app()`. Default host `127.0.0.1`, port `8765`. When `--host` is not a
  loopback address, print a warning that the UI token is only given to loopback clients (other machines
  cannot use the app; section 10).
  It prints the URL `http://127.0.0.1:8765/` and opens the browser unless `--no-browser` (use
  `webbrowser.open`, never fail if that fails). Exit 0 on Ctrl-C after `worker.stop()`.
- One process, one `Worker` (one daemon thread, in-process FIFO `queue`), no Redis, no multiprocessing.
- `create_app(*, worker: Worker | None = None, settings_loader=load_settings, doctor=run_doctor,
  voicebox_factory=None, allowed_hosts: set[str] | None = None, start_worker: bool = True) -> FastAPI`.
  Tests pass a worker built with fake deps and `start_worker` as needed. The app keeps the worker on
  `app.state.worker`; a lifespan handler calls `worker.start()` on startup (when `start_worker`) and
  `worker.stop()` on shutdown.
- The request threads NEVER hold a `JobStore` for an existing job and never call `save()`/`set_stage()`.
  They read `job.json` read-only (`Job.model_validate_json`). The only exception: `Worker.submit_job`
  creates a brand-new job (`JobStore.create`) before enqueuing it; after that the worker thread is its
  only writer.

## 2. Auth module - `autoclipper/web/auth.py`

```python
TOKEN_HEADER = "X-Auth-Token"
def token_path() -> Path                      # data_dir() / "web" / "token"
def load_or_create_token() -> str             # secrets.token_urlsafe(32); created on first run with
                                              # mode 0o600 (os.open O_CREAT|O_EXCL, chmod ignored on Windows);
                                              # an empty/short (<32 chars) file is regenerated
def check_token(given: str | None, expected: str) -> bool   # hmac.compare_digest; None/"" -> False
DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
def host_allowed(host_header: str | None, allowed: set[str]) -> bool
    # strip the port (handle "[::1]:8765"), lower-case; None -> False
def origin_allowed(origin: str | None, allowed: set[str]) -> bool
    # None -> True (non-browser client; the token still applies); "null" -> False;
    # else urlsplit: scheme http/https and hostname in allowed
```

Rules, applied by middleware in this order to EVERY request (pages, static, API):
1. **Host guard (DNS rebinding):** `Host` not in `allowed_hosts` -> 403 `{"message": "forbidden host", "errors": {}}`.
   `allowed_hosts` = `DEFAULT_ALLOWED_HOSTS` plus the `--host` value when it is a concrete address (not `0.0.0.0`/`::`).
2. **Origin guard (CSRF):** for `POST`/`PUT`/`PATCH`/`DELETE`: `Origin` present and not allowed -> 403
   `"cross-origin request rejected"`. Also `Sec-Fetch-Site: cross-site` -> 403.
3. **Token:** every `/api/*` route needs `X-Auth-Token`. Missing/empty -> **401** `"missing auth token"`;
   wrong -> **403** `"invalid auth token"`. Exception, for media only (a `<video src>` / download link cannot
   send headers): the two `GET .../video` and `.../download` routes also accept `?token=<token>`.
   Public (no token): `GET /`, `GET /static/*`, `GET /favicon.ico` (204 ok).
- `GET /` returns `static/index.html` with the literal placeholder `__AUTOCLIPPER_TOKEN__` replaced by the
  token (the HTML holds `<meta name="autoclipper-token" content="__AUTOCLIPPER_TOKEN__">`). Headers:
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:;
  media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'`.
  No inline scripts or inline styles in the page (CSP); `app.js` reads the token from the meta tag.
- No CORS middleware (same-origin only).

## 3. Errors - `autoclipper/web/errors.py`

Every non-2xx response (including 404 for unknown `/api` paths and 405) is JSON:
```json
{"message": "human readable sentence", "errors": {"field.path": ["what is wrong", ...]}}
```
`errors` is always an object (empty `{}` when there is no field detail).
```python
class APIError(Exception):
    def __init__(self, status: int, message: str, errors: dict[str, list[str]] | None = None): ...
def install_error_handlers(app: FastAPI) -> None
```
- `RequestValidationError` and pydantic `ValidationError` -> **422** `"validation failed"`, `errors` keyed by
  dotted location without the `body`/`query`/`path` prefix (e.g. `"clips"`, `"options.langs.1"`,
  `"captions.font_size"`); messages are pydantic's `msg`. Never echo the raw `input` (it may be huge).
- `StarletteHTTPException` -> same status, `{"message": detail, "errors": {}}`.
- Any other exception -> **500** `{"message": "internal error (see server log)", "errors": {}}`; the
  traceback goes to the server log only, never into the response.
- Malformed JSON body -> 422. Body that is not an object where one is expected -> 422.

Status codes used: 200, 201, 202 (task queued), 204, 401, 403, 404, 409 (conflict with a running
task / reused Idempotency-Key), 413 (upload too large), 415 (wrong upload content type), 422, 500,
503 (Voicebox unreachable for `/api/voicebox/profiles`).

## 4. Routes

Common path-parameter validation (FastAPI `Path(...)` constraints -> 422 on violation):
- `job_id`: `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (no dots or slashes: no path traversal). Well-formed but no
  `data/jobs/<id>/job.json` -> **404** `"job <id> not found"`.
- `clip`: int, `0 <= clip <= 999`; not in `job.clips` -> **404** `"clip <n> not found in job <id>"`.
- `lang`: `"en" | "hi"` -> else 422; lang not in that clip's `langs` -> **404**.
- `task_id`: `^[0-9a-f]{32}$`; unknown -> 404.
Every path below except `/` and `/static/*` needs the token (section 2).

### 4.1 Page and static
| Method | Path | Notes |
|---|---|---|
| GET | `/` | index.html with token injected (section 2) |
| GET | `/static/{file}` | `index.html`, `app.js`, `styles.css` (+ any file the frontend adds under `static/`); 404 JSON otherwise |

### 4.2 Uploads (step 1 of "new job from file")
`POST /api/uploads?filename=<name>` - body is the RAW file bytes (`Content-Type: application/octet-stream`
or any `video/*`; `multipart/form-data` -> 415). Streamed to `data/web/uploads/<upload_id>/source.part<ext>`
in 1 MiB chunks, then `os.replace` to `source<ext>`. No python-multipart, no temp files elsewhere.
- `filename`: required, 1..255 chars, basename only (reject `/`, `\`, NUL, `..`), extension (lower-cased)
  must be in `settings.web.allowed_extensions` -> else 422 `errors.filename`.
- Size: `Content-Length` > `settings.web.max_upload_mb` MiB -> **413** before reading. While streaming, more
  bytes than the limit -> stop, delete the partial file, **413**. Empty body -> 422 `errors.body`.
  Client disconnect -> partial file deleted.
- 201:
```json
{"upload_id": "32 hex", "filename": "talk.mp4", "size": 123456789}
```
- Uploads are claimed by `POST /api/jobs`. Unclaimed uploads are not deleted automatically; on worker
  start only leftover `*.part*` files under `data/web/uploads` are removed.

### 4.3 Jobs
`POST /api/jobs` - create and queue a job. Header `Idempotency-Key` optional (frontend always sends a fresh
`crypto.randomUUID()` per submit click): 1..200 chars of `[A-Za-z0-9_.:-]`, else 422 `errors["Idempotency-Key"]`.
Request JSON (pydantic model `NewJobRequest`, `extra="forbid"`):
```json
{"url": "https://...",          // exactly one of url / upload_id
 "upload_id": "32 hex",
 "options": {                   // optional; every field optional, default = current settings
   "clips": 3,                  // int 1..30
   "min_seconds": 20,           // number 5..180
   "max_seconds": 45,           // number 10..180, must be >= min_seconds (422 errors.options.max_seconds)
   "langs": ["en", "hi"]        // 1..2 unique items of "en"|"hi"
 }}
```
- `url`: 1..2048 chars, scheme `http`/`https` only, must have a host; anything else (file:, ftp:,
  javascript:, a local path) -> 422 `errors.url`. The API NEVER accepts a local file path as a source.
- `upload_id`: `^[0-9a-f]{32}$`; unknown or already claimed -> **404** `"upload <id> not found"`.
- both or neither of url/upload_id -> 422 (`errors.url` and `errors.upload_id`).
- Effect: `Worker.submit_job(...)` creates the job (`JobStore.create(source)`, status `queued`; source is
  the URL, or `"upload:<filename>"`), moves the upload to `data/jobs/<id>/source<ext>` (os.replace; the
  ingest stage then reuses it via `find_source`), writes `data/jobs/<id>/options.json`
  (`{"clips","min_seconds","max_seconds","langs"}`, nulls for "use settings"), enqueues `run_job`.
- **201** with `JobSummary` + `task` (shapes below). Same Idempotency-Key + same canonical body within 24 h
  -> **200** with the same job (no new job, upload not touched again). Same key + different body -> **409**
  `"Idempotency-Key was already used for a different request"`. Keys persist in
  `data/web/idempotency.json` (atomic write, pruned after 24 h) so they survive a restart.

`GET /api/jobs?limit=50&offset=0` - `limit` 1..200, `offset` 0..100000 (else 422). Newest first
(`JobStore.list_ids()` order). A job dir whose job.json does not parse is listed with
`status: "failed"`, `error: "job.json unreadable"` instead of failing the list. 200:
```json
{"jobs": [JobSummary, ...], "total": 12, "limit": 50, "offset": 0}
```
`JobSummary`:
```json
{"id": "20261122-185849-65a6bd", "source": "https://...", "status": "queued|running|done|failed",
 "error": null, "created_at": 1763837929.1,
 "clip_count": 2, "langs": ["en", "hi"],
 "rendered": 3,            // clip-langs with final set and no error
 "failed": 1,              // clip-langs with error
 "interrupted": false,     // job.error == "interrupted"
 "task": Task | null}      // the active (queued/running) task for this job, else null
```

`GET /api/jobs/{job_id}` - 200 `JobDetail` = `JobSummary` plus:
```json
{"stages": {"ingest": StageState, "transcribe": ..., "moments": ..., "cut": ...},
 "options": {"clips": 3, "min_seconds": null, "max_seconds": null, "langs": ["en"]} | null,
 "clips": [{
   "index": 0,
   "moment": {"start": 127.66, "end": 157.88, "duration": 30.22, "hook": "...", "reason": "...", "score": 0.93},
   "cut": true,                                  // clips/NN/cut.mp4 exists, size > 0
   "langs": {"en": {
      "stages": {"script": StageState, "voice": ..., "captions": ..., "render": ...},
      "error": null,
      "final": true,                             // final.mp4 exists, size > 0 AND ClipLangState.final set
      "video_url": "/api/jobs/<id>/clips/0/en/video",        // null when final is false
      "download_url": "/api/jobs/<id>/clips/0/en/download",  // null when final is false
      "script": ClipScript | null,               // script.json (null if missing/unreadable)
      "metadata": {"language","title","description","hashtags"} | null,
      "voice": {"duration": 25.96, "clip_duration": 30.24, "tempo": 1.0, "shortened": false,
                "trimmed": false, "lengthened": false} | null,
      "voice_stale": false,                      // script narration hash != voice.json narration_hash
      "task": Task | null}}}]}                   // active rerender task for this clip-lang
```
`StageState` = `{"status": "pending|running|done|failed|skipped", "progress": 0.0..1.0, "message": ""}`.
Progress is whatever the worker last saved to job.json (pipeline saves every 5 % or 1 s).

`GET /api/jobs/{job_id}/log?tail=200` - `tail` 1..2000. 200 `{"lines": ["[id] voice 00/en 40% ...", ...]}`
from `data/jobs/<id>/web.log` (written by the worker; `[]` if absent).

`POST /api/jobs/{job_id}/resume` - no body. Re-queues `run_job` for an existing job with its
`options.json` (or, when absent, langs = the langs present in `job.clips`). 409 if the job is `done` with
no failed clip-lang and no interruption (`"job is already complete"`). If a task for this job is already
queued/running -> **200** with that task (idempotent). Else **202**. Response: `{"job": JobSummary, "task": Task}`.

### 4.4 Clip review
`GET /api/jobs/{job_id}/clips/{clip}/{lang}/script` - 200 `ClipScript`
`{"narration": str, "title": str, "description": str, "hashtags": [str]}`; 404 if script.json missing.

`PUT /api/jobs/{job_id}/clips/{clip}/{lang}/script` - body `ClipScriptIn` (`extra="forbid"`):
- `narration`: str, 1..5000 chars after strip (whitespace-only -> 422)
- `title`: str, 1..200 chars after strip (stored normalized to <= 70 by `normalize_script`)
- `description`: str, 0..5000 chars
- `hashtags`: list of 0..8 str (`MAX_HASHTAGS` from `stages.script`, the same limit `normalize_script`
  keeps), each 1..100 chars
Effect: `normalize_script` then `stages.script.save_script` (atomic) to script.json, and
`stages.render.write_metadata` to metadata.json **only if final.mp4 exists** (title/description edits need no
re-render). Does NOT touch job.json. 409 `"clip is being rendered; try again when it finishes"` if the
running task is `run_job` for this job or `rerender` for this clip-lang. 200:
```json
{"script": ClipScript, "voice_stale": true}
```

`POST /api/jobs/{job_id}/clips/{clip}/{lang}/rerender` - no body. 409 if `clips/NN/cut.mp4` or script.json is
missing (`"clip is not ready to re-render"`) or a `run_job` task for this job is queued/running.
A queued (not yet running) rerender for the same (job, clip, lang) -> **200** with that same task (idempotent).
Otherwise **202** `{"task": Task}`. A running rerender for the same key does not block a new one: the new one
is queued after it (the user may have saved new edits).

`GET /api/jobs/{job_id}/clips/{clip}/{lang}/video` - `final.mp4` as `video/mp4` with HTTP Range support
(`Accept-Ranges: bytes`; `Range: bytes=a-b` -> 206 + `Content-Range`; unsatisfiable -> 416; no Range ->
200 full). Use Starlette `FileResponse` (1.x supports Range). `Cache-Control: no-store` (re-renders replace
the file). 404 if final.mp4 missing. Token via header or `?token=`.

`GET /api/jobs/{job_id}/clips/{clip}/{lang}/download` - same file with
`Content-Disposition: attachment; filename="<job>_clip<NN>_<lang>.mp4"` (ASCII only).

### 4.5 Tasks / queue
`Task`:
```json
{"id": "32 hex", "kind": "run_job|rerender", "job_id": "...", "clip": 0 | null, "lang": "en" | null,
 "state": "queued|running|done|failed|cancelled",
 "position": 0,            // 0 = running now, 1.. = place in the waiting line, null when finished
 "error": null, "created_at": 1763.0, "started_at": null, "finished_at": null}
```
`GET /api/queue` - 200 `{"running": Task | null, "queued": [Task, ...], "recent": [Task, ...]}` (recent =
last 20 finished, newest first).
`GET /api/tasks/{task_id}` - 200 `Task`; 404 unknown (tasks live in memory only).
`POST /api/tasks/{task_id}/cancel` - queued -> removed, state `cancelled`, 200 `Task`. Running -> cooperative
cancel requested, 202 `Task` (it becomes `cancelled` when the pipeline stops; the job is marked
failed/"interrupted" by the pipeline and can be resumed). Finished -> 409.

### 4.6 Settings
`GET /api/settings` - 200 `Settings.model_dump()` (the whole tree, section 8).
`GET /api/settings/schema` - 200 `Settings.model_json_schema()` (pydantic; carries `minimum`, `maximum`,
`enum`, `default`, nested `$defs`). The frontend builds the form from this plus the field labels it owns.
`PUT /api/settings` - body = the FULL settings object. Validation: unknown keys anywhere -> 422 (backend checks
recursively against model fields, since `Settings` itself ignores extras), then `Settings.model_validate`
-> 422 with dotted `errors`. Cross-field: `clips.min_seconds > clips.max_seconds` -> 422
`errors["clips.max_seconds"]`; empty `languages` -> 422. Success: atomic write of `data/settings.json`
(`stages._atomic.write_text_atomic`), 200 with the saved settings. Takes effect for the NEXT task (the worker
loads settings at the start of every task).
`GET /api/voicebox/profiles` - 200 `{"profiles": [{"id": str, "name": str, "language": str|null}, ...]}` from
`VoiceboxClient.from_settings(settings.voicebox, timeout=10).list_profiles()` (fields mapped leniently:
`id`/`profile_id`, `name`, `language`). Unreachable/any VoiceboxError -> **503**
`{"message": "Voicebox is not reachable at <url> - is it running?", "errors": {}}`.

### 4.7 Doctor
`GET /api/doctor` - 200 `{"checks": [{"name": str, "level": "ok|warn|fail", "detail": str}, ...],
"problems": int, "warnings": int}` from `cli.run_doctor(load_settings())` (the exact `autoclipper doctor`
checks). Runs in a thread (`run_in_threadpool`), can take a few seconds.

## 5. Worker - `autoclipper/web/worker.py`

```python
TaskKind = Literal["run_job", "rerender"]
TaskState = Literal["queued", "running", "done", "failed", "cancelled"]

@dataclass
class Task:
    id: str; kind: TaskKind; job_id: str; clip: int | None = None; lang: str | None = None
    state: TaskState = "queued"; error: str | None = None
    created_at: float; started_at: float | None = None; finished_at: float | None = None
    def to_dict(self, position: int | None) -> dict     # the Task JSON of section 4.5

@dataclass
class JobOptions:
    clips: int | None = None; min_seconds: float | None = None; max_seconds: float | None = None
    langs: list[str] | None = None

class TaskCancelled(BaseException): ...   # BaseException: the pipeline records it as "interrupted"

class Worker:
    def __init__(self, *,
                 settings_loader: Callable[[], Settings] = load_settings,
                 deps_factory: Callable[[Settings], Deps | None] = lambda s: None,
                 run_job: Callable[..., Any] = pipeline.run_job,
                 rerender: Callable[..., Any] = pipeline.rerender,
                 history: int = 20): ...
    def start(self) -> None          # recover_interrupted(), clean upload *.part, start the daemon thread (idempotent)
    def stop(self, timeout: float = 10.0) -> None   # stop taking tasks; cancel the running one; join(timeout)
    def recover_interrupted(self) -> list[str]
    def submit_job(self, source: str, *, options: JobOptions, upload: Path | None = None) -> Task
    def submit_resume(self, job_id: str) -> tuple[Task, bool]      # (task, created); existing active task -> (it, False)
    def submit_rerender(self, job_id: str, clip: int, lang: str) -> tuple[Task, bool]   # queued same key -> (it, False)
    def cancel(self, task_id: str) -> Task | None
    def status(self, task_id: str) -> Task | None
    def active_task_for(self, job_id: str, clip: int | None = None, lang: str | None = None) -> Task | None
    def position(self, task_id: str) -> int | None     # 0 running, 1.. waiting, None finished/unknown
    def snapshot(self) -> dict                           # {"running": dict|None, "queued": [...], "recent": [...]}
    def wait_idle(self, timeout: float = 30.0) -> bool   # tests: True when nothing queued or running
```
- One `threading.Lock` guards the task table and the waiting deque; a `threading.Condition` wakes the thread.
  FIFO: tasks run in submit order. One task at a time.
- **Per task** the thread: `settings = settings_loader()`; `deps = deps_factory(settings)`; opens
  `data/jobs/<id>/web.log` (append, utf-8); `log(line)` appends the line (and to the python logger), and
  **raises `TaskCancelled` if this task's cancel flag is set** (the pipeline calls `log` from its progress
  callbacks, so ffmpeg / Voicebox polling stop within a progress tick; `ffmpeg.run` kills its child on a
  BaseException from on_progress).
  - `run_job`: `run_job(source, settings=settings, deps=deps, langs=o.langs, count=o.clips,
    min_seconds=o.min_seconds, max_seconds=o.max_seconds, job_id=task.job_id, log=log)` - always with
    `job_id` (the job was created at submit; resume uses the same call).
  - `rerender`: `rerender(job_id, clip, lang, settings=settings, deps=deps, log=log)`.
  - `PipelineError`/any `Exception` -> task `failed`, `error = str(e)` (first line, <= 500 chars); the job's
    own failure is already in job.json. `TaskCancelled` -> `cancelled`. Never let an exception kill the thread.
- **Real vs fake deps:** production uses the default `deps_factory` (returns None) so `pipeline._LazyDeps`
  builds the real clients from the settings loaded for THAT task, lazily (no Voicebox/LLM client until a stage
  needs it). Tests pass `deps_factory=lambda s: Deps(llm=FakeLLM(), voicebox=FakeVoicebox(),
  transcriber=FakeTranscriber())` (conftest makes the real factories raise), or inject `run_job`/`rerender`
  stubs (e.g. a function that blocks on an Event) to test queue order, positions, idempotency and cancel fast.
- **Crash recovery** (`recover_interrupted`, called by `start()` BEFORE the thread runs): for every job whose
  job.json has `status` `"running"` or `"queued"`: every stage with status `running` -> `failed`, message
  `"interrupted"`; `job.status = "failed"`, `job.error = "interrupted"`; save via `JobStore.load(id).save()`
  (the worker is the writer at this point). Skip (log a warning) a job whose `job.lock` is live (section 10);
  only when there is NO job.lock, skip a job whose job.json mtime is < 30 s old (`RECOVER_MIN_AGE`). Returns the ids it marked. The UI shows "Interrupted - Resume".
- `submit_job` with `upload`: create the job, `os.replace(upload, job_dir / f"source{ext}")`, then remove the
  empty upload dir. On any failure after `JobStore.create`, the job is saved `failed` with the error (never
  left `queued` with nothing in the queue).

## 6. Static file layout (frontend)

```
autoclipper/web/static/index.html   # shell; <meta name="autoclipper-token" content="__AUTOCLIPPER_TOKEN__">;
                                    # <link rel="stylesheet" href="/static/styles.css">; <script src="/static/app.js" defer>
autoclipper/web/static/app.js       # vanilla ES2020, no modules bundler, no CDN, no inline handlers (CSP)
autoclipper/web/static/styles.css   # CSS variables, prefers-color-scheme dark/light + manual toggle (localStorage)
```
Hash routes: `#/new`, `#/jobs`, `#/jobs/<id>`, `#/jobs/<id>/clips/<n>/<lang>`, `#/settings`, `#/doctor`.
- Every `fetch` sends `X-Auth-Token`; JSON bodies `Content-Type: application/json`. Show `message` and the
  per-field `errors` from any non-2xx response next to the fields. 401/403 -> banner "reload the page".
- New job: drag/drop or pick a file (check extension + size client-side from settings, still trust the server),
  upload with `XMLHttpRequest` to `/api/uploads?filename=...` showing upload %; or a URL field. Then
  `POST /api/jobs` with `Idempotency-Key`. Options: clips, min, max, langs checkboxes (defaults from settings).
- Jobs list + detail: poll the visible job every 1.5 s (`GET /api/jobs/{id}`) and `GET /api/queue` every 2 s;
  stop polling when the tab is hidden (`visibilitychange`) and when nothing is queued/running. Show per job
  stage and per clip x lang stage status + progress bars + messages + errors, queue position, Resume button
  for failed/interrupted jobs, Cancel for queued/running tasks.
- Clip review: `<video controls preload="metadata" src="video_url?token=...">` (append `&v=<render finished_at>`
  to bust cache after a re-render), editable narration (textarea, word count + estimated seconds using
  `settings.script.wps_<lang>`), title (counter /70), description, hashtags (chips or comma text). Save (PUT),
  Re-render (POST, then poll the task; disable while queued/running), Download link (`download_url?token=`).
  Show `voice_stale` ("narration changed - re-render to regenerate the voice").
- Settings: form generated from `/api/settings/schema` + `/api/settings`, grouped by section, number inputs with
  min/max/step, selects for enums, checkboxes for bools. Profile ids for `voicebox.english.profile_id` /
  `voicebox.hindi.profile_id` are dropdowns filled from `/api/voicebox/profiles` (fallback: free text when 503).
  `captions.position_pct` gets a slider with a small 9:16 preview box. Save = PUT the whole object.
- Doctor panel: table of checks with ok/warn/fail badges and a Re-run button.
- Fonts: `font-family: "Noto Sans", "Noto Sans Devanagari", system-ui, sans-serif` everywhere; editors and
  previews with `lang="hi"` content use `"Noto Sans Devanagari", "Noto Sans", sans-serif` (system fonts; no
  webfont download). Set `lang` attributes on Hindi fields.
- Responsive down to 360 px wide, keyboard accessible, visible focus, no horizontal page scroll.

## 7. Tests (backend) - `tests/test_web_*.py`

pytest + `fastapi.testclient.TestClient(app, base_url="http://127.0.0.1:8765")` (Host must pass the guard).
The conftest isolation applies (data dir in tmp_path, no network, real factories raise). Never start uvicorn
on a port in tests; never touch the real `data/`. For every route: happy path; 422 cases (missing fields,
wrong types, empty and very long strings, negative / zero / huge numbers, bad enums, extra keys); 401 (no
token) and 403 (wrong token, bad Host, cross Origin on writes); 404 (unknown job/clip/lang/task/upload);
exact JSON structure (keys AND types); disk side effects (job.json created/status, options.json, upload moved,
settings.json changed, script.json/metadata.json changed, idempotency.json). Range: 206 with correct
`Content-Range` and body bytes. Worker: FIFO order, positions, rerender idempotency, resume idempotency,
cancel queued + running (stub that calls `log` in a loop), crash recovery marks running -> interrupted, a
failing task does not kill the thread, one real end-to-end task with fakes from `tests/fakes.py`.
`autoclipper serve --help` parses (cli test), without running a server.

## 8. New settings fields (product adds them to `config.py`; the frontend reads them via the schema)

All have defaults, so existing `data/settings.json` files still load (missing keys take defaults; nothing
is renamed or removed).

```python
class AudioSettings(BaseModel):              # existing fields unchanged
    original_volume: float = Field(0.15, ge=0, le=1)     # NOW: original level WHILE the voice plays (ducked)
    original_volume_after: float = Field(1.0, ge=0, le=2) # NEW: original level when no voice plays (before/after)
    duck_ramp_seconds: float = Field(0.4, ge=0, le=2)    # NEW: ramp time between the two levels
    loudnorm: bool = True                                 # NEW: single-pass loudnorm on the final mix
    target_lufs: float = Field(-14.0, ge=-30, le=-8)      # NEW: loudnorm integrated target
    voice_volume / max_speedup: unchanged

class ScriptSettings(BaseModel):             # NEW section Settings.script
    wps_en: float = Field(3.0, ge=1.0, le=6.0)   # measured: chatterbox EN ~3.0 words/s in the sample job
    wps_hi: float = Field(3.35, ge=1.0, le=6.0)  # measured: chatterbox HI ~3.36 words/s in the sample job
    fill_ratio: float = Field(0.88, ge=0.5, le=1.0)   # narration target = clip * fill_ratio (the LLM overshoots ~5 %)
    lengthen_below: float = Field(0.75, ge=0.3, le=0.95)  # voice < this x clip -> one lengthen retry
    def wps(self, lang: str) -> float

class CaptionSettings(BaseModel):            # existing fields unchanged
    position_pct: float | None = Field(72.0, ge=5, le=95)  # NEW: vertical CENTER of the caption line, % from top;
                                                           # None -> legacy bottom-anchored margin_v
    margin_v: int = 520                                    # kept; used only when position_pct is None

class WebSettings(BaseModel):                # NEW section Settings.web (used by the backend)
    max_upload_mb: int = Field(4096, ge=1, le=102400)
    allowed_extensions: list[str] = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ogv"]
        # validator: each lower-case, starts with ".", 2..10 chars, [a-z0-9]; 1..30 items

class Settings(BaseModel):
    ... existing ...
    script: ScriptSettings = Field(default_factory=ScriptSettings)
    web: WebSettings = Field(default_factory=WebSettings)
```
Also add `Field(..., ge/le)` bounds where step-1 fields have none (frontend number inputs read them):
`captions.font_size` 20..200, `captions.words_per_line` 1..8, `voicebox.poll_interval` 0.1..10,
`voicebox.timeout_seconds` 10..7200, `clips.width`/`height` 240..3840 (even). Enum-like strings that are free
today (`whisper.model`, `whisper.device`, `whisper.compute_type`, `VoiceProfile.engine`) stay `str` (no
breaking change); the frontend offers suggestions via `<datalist>`: whisper.model
`tiny|base|small|medium|large-v3`, device `auto|cpu|cuda`, compute_type `auto|int8|float16|int8_float16`,
engine `chatterbox|chatterbox_turbo|kokoro|qwen`. README "Settings reference" gets the new rows (product).

Until the product builder lands section 8, backend and frontend must tolerate its absence: backend reads
`getattr(settings, "web", None)` with the defaults above as fallback; frontend falls back to wps 3.0 and hides
fields the schema does not contain.

## 9. Product fixes (product builder; stage files only)

1. **Ducking (render.py).** `render_final` builds its own graph (do not change `ffmpeg.mix_burn`): voice active
   window `[vs, ve]` = `[0, end of speech]`, where end of speech comes from `words.json` last word end if present
   (same dir as the voice), else `ffmpeg.duration(voice)` with trailing silence stripped (silencedetect
   -45 dB, 0.3 s). Original gain is a `volume=...:eval=frame` piecewise-linear envelope: `original_volume_after`
   outside, `original_volume` inside, linear ramps of `duck_ramp_seconds` (ramp down ends at vs, ramp up starts
   at ve; ramp down is skipped when vs == 0). Then amix normalize=0 -> (`loudnorm=I=<target>:TP=-1.5:LRA=11`,
   `aresample=48000`, when `audio.loudnorm`) -> `_LIMITER`. No voice -> original at `original_volume_after`.
   Output duration == cut duration, h264/aac as before. Tests: measure with `astats`/`volumedetect` on
   segments (before/during/after) using fakes (`make_video` tone + `make_tone` voice): tail level about the
   original, during level ducked, peak <= -0.2 dBFS; graph-string unit test for the envelope.
2. **Narration length (script.py + llm.py + voice.py + pipeline).** `target_words(duration, lang, *, wps=None,
   fill_ratio=FILL_RATIO)`; `write_script(..., script_settings: ScriptSettings | None = None)` passes
   `target_seconds = duration * fill_ratio` and the word count to `llm.write_script`. The prompt states the clip
   length, the target spoken duration, the rate (words per second for that language) and "between
   round(target*0.95) and round(target*1.05) words". `AnthropicLLM.lengthen(lang, narration, target_words)` +
   `LENGTHEN_SYSTEM`. `make_voice(..., lengthen: LengthenFn | None = None, lengthen_below: float = 0.75,
   fill_ratio: float = 0.88)`: if `d < clip * lengthen_below` and lengthen given: target =
   `ceil(words * clip * fill_ratio / d)`, one retry, regenerate. If the longer take does not fit within
   `max_speedup`, keep the ORIGINAL take (keep its file) - never trim because of a lengthen. At most one
   lengthen; never both lengthen and shorten. `VoiceResult.lengthened: bool = False`. LLM errors in lengthen
   -> keep the original take, log a warning. Pipeline: `_run_voice(..., allow_lengthen: bool)` - True from
   `run_job`, **False from `rerender`** (user-edited text is respected); lengthen uses
   `getattr(deps.llm, "lengthen", None)`; narration write-back to script.json as for shorten.
   Compatibility: `target_words(duration, lang)` and `write_script(...)` called WITHOUT the new arguments keep
   the step-1 constants (`WORDS_PER_SECOND`, `FILL_RATIO`), so existing tests in tests/test_llm_moments_script.py
   and test_pipeline_e2e.py stay green unchanged; the pipeline always passes `settings.script`. Do not edit
   other builders' or step-1 test files; if a step-1 test must change, note it in TEAM_LOG with the reason.
3. **Caption position (captions.py).** With `position_pct` set: style Alignment 5, each Dialogue starts with
   `{\an5\pos(<width/2>,<round(height*pct/100)>)}`; with None: today's Alignment 2 + margin_v. Tests check
   both header and events, and a real burn shows the text centred near 72 % (optional, slow-free with a tiny
   ffmpeg render).
4. **Hindi spelling (llm.py `language_rules("hi")`).** One or two sentences: standard Hindi spelling
   (e.g. अट्ठाईस, not अठ्ठाईस); avoid Urdu nuktas (क़, ज़, फ़) unless the word is normally written with them.

## 10. Additions after the step-2 review (approved by the lead; implemented by the fixer)

These are part of the contract now. Tests: `tests/test_web_review_fixes.py` plus the existing `test_web_*.py`.

**Auth and page**
- `GET /` injects the token only when the TCP peer (`request.client.host`) is loopback (`auth.is_loopback`).
  The Host header is client-chosen, so it is not enough. Other peers get the page with an empty token, and
  the page shows its "no access token" banner. `serve --host 0.0.0.0` therefore cannot be used from other
  machines.
- uvicorn's access log goes through `auth.TokenRedactFilter` (installed by `cmd_serve` via
  `install_access_log_filter()`): `token=<value>` is logged as `token=***`.
- `/static/{file}` accepts GET and HEAD and sends `Cache-Control: no-cache` (an upgraded install picks up new JS).
- `create_app(..., static_dir: Path | None = None, stop_on_signal: bool = False)`. `static_dir` is for
  tests. When `GET /` has no index.html, it serves a tiny fallback page that still has the token meta.
  `stop_on_signal=True` (used by `cmd_serve`) chains SIGINT/SIGTERM in the lifespan so `worker.begin_stop()`
  runs the moment the signal arrives, before uvicorn's graceful wait.
- HEAD is also accepted on the `video` and `download` routes. `?token=` works for GET/HEAD there only.

**Errors and validation**
- Malformed JSON is always 422 `{"message": "validation failed", "errors": {"body": ["invalid JSON"]}}`.
  This includes bodies `json.loads` rejects with something other than JSONDecodeError, such as a 5000-digit
  int or 100000-deep nesting. FastAPI would send 400 for those.
- `POST /api/jobs` `url`: the port must be numeric and in 1..65535 (port 0 is rejected too) (`urlsplit(url).port` is evaluated),
  otherwise 422 `errors.url`.
- `GET /api/settings` when settings.json is invalid gives 500 with the dotted field errors (the page can
  still PUT a fix). `PUT /api/settings` validates with `strict=True` (`"78"` for an int is 422), rejects
  unknown keys (422 on the dotted key), and then checks, with no write on any error:
  - `voicebox.url`: http/https with a host, a valid port if one is given, at most 2048 chars;
  - `captions.margin_v`: 0..`clips.height`;
  - `captions.primary_color` / `highlight_color`: `^&H[0-9A-Fa-f]{8}\Z` (the defaults pass; a trailing newline is rejected);
  - at most 200 chars: `llm.model`, `llm.api_key_env`, `captions.font_en`/`font_hi`, `whisper.model`/
    `device`/`compute_type`/`language`, and `voicebox.{english,hindi}.{profile_id,engine}`.

**Uploads and media**
- Each upload dir keeps `upload.json` `{"filename", "size", "created_at"}` next to `source<ext>`. The job
  source becomes `upload:<original name>`. Allowed Content-Types are missing, `application/octet-stream`,
  `video/*` and `application/ogg`; multipart and anything else get 415.
- A malformed `Range` is ignored (full 200). An unsatisfiable one gets a JSON 416 with
  `Content-Range: bytes */<size>`.

**Jobs, tasks and ownership (job.lock)**
- `data/jobs/<id>/job.lock` = `{"pid": int, "host": str, "started": float}` (module `autoclipper/joblock.py`,
  `hold_job_lock(job_id)`). The worker writes it around `run_job`/`rerender` in `_execute`. The CLI `run`
  (which now creates a new job first, the same way the worker does) and `rerender` also write it. All of
  them remove it in `finally`. A lock is live when `host == socket.gethostname()` and the pid exists
  (`os.kill(pid, 0)`, or OpenProcess on Windows). A dead-pid, other-host or unreadable lock is stale and is
  overwritten. Taking a lock that another live process holds raises `JobLocked`: the CLI exits 1, and a
  worker task fails without touching job.json.
- `POST .../resume` and `POST .../rerender` return **409** `"job is being run by another process (pid N)..."`
  when the job has a live lock and the worker has no task for it. Only when there is no job.lock at all,
  resume keeps the old fallback: 409 for a `running` job whose job.json changed less than 30 s ago.
- Resume idempotency only looks at `run_job` tasks. A queued or running rerender does not block a resume;
  the resume is queued after it.
- Cancelling a queued `run_job` makes the worker thread mark that job.json `failed`/"interrupted" (worker =
  writer) between tasks, so the UI offers Resume. The same happens when a run_job ends before the pipeline
  could record it (a settings or deps failure).
- `Worker.stop()` first calls `begin_stop()` (sets `stopping` and the running task's cancel flag), then joins.
  When the thread has exited, `stop()` drains the pending finalize list and marks every still-queued task
  `cancelled` ("server stopped"), with its run_job job.json `failed`/"interrupted".
- When a task ends with an Exception or a normal return while the worker is stopping or its cancel flag is
  set, the task is `cancelled` and a `queued`/`running` job.json becomes `failed`/"interrupted". It is not
  shown as an ffmpeg error.
- `log` raises TaskCancelled only once. It does not raise again while that TaskCancelled is being handled
  (`sys.exc_info`), so the pipeline's own `except BaseException` can save job.json.
- Task history: a finished task that drops out of `recent` (`history`, default 20) is removed from the task
  table. `GET /api/tasks/{id}` is then 404. Cancel events are removed when a task finishes or a queued task is
  cancelled.
- An unreadable job.json shows in the list and detail as `failed` / "job.json unreadable". Other routes
  give 409 for it.
- UI: Resume is also offered for `queued`/`running` jobs that have no task. The server's 409 guards still
  protect a live CLI run.

**Pipeline (product, approved)**
- `run_job` lets the voice stage lengthen a short take only when `regenerated or not voice.json exists`:
  a new or regenerated script may be lengthened, but a resume never rewrites a narration the user edited.
  `rerender` never lengthens.


## 11. Voice-over on/off ("captions only"), additive (feature builder, 2026-11-23)

- **Setting** `voiceover: bool = true` (top level of `Settings`, shown in Settings > General as "Voice-over
  narration"). `PUT /api/settings` is strict: a non-boolean is 422 `errors.voiceover`.
- **Job model** `Job.voiceover: bool | None = None` in job.json: the job's mode, fixed at creation (the
  setting at that moment unless overridden). None = a job made before the option existed = **on** (lead fix:
  a Settings change never switches an existing job). Effective mode = `pipeline.job_voiceover(job)`;
  `pipeline.settings_for_job` gives settings with it.
- **POST /api/jobs** `options.voiceover: bool | null` (strict; `"false"`/`1` -> 422 `errors.options.voiceover`).
  null = the current setting. The resolved bool is written to `options.json`, to `job.json` at creation, and
  passed to `run_job(..., voiceover=...)` by the worker. The web form always sends the checkbox value.
  CLI `run` (new job, no flag) stores the setting in job.json the same way.
- **GET /api/jobs/{id}** adds `voiceover: bool` (effective) and `voiceover_override: bool | null` (job.json's
  raw value; null only for legacy jobs); every
  clip-lang adds `voiceover: bool` (effective) and `rendered_voiceover: bool | null` (from render.json; null
  without a final). `render_stale` is also true when the two differ.
- **PUT .../script**: an empty narration is accepted (200) when the job's voice-over is off, or when the saved
  script has no narration yet (re-render writes one); otherwise 422 `errors.narration` as before. `voice_stale` (web and publish) is false for a final rendered without voice.
- **Pipeline, voice-over off:** script = `write_metadata_script` (LLM `write_publish_metadata`, narration "");
  voice stage `skipped` / message `voice-over off` (no Voicebox client is built); captions =
  `stages/original_captions.make_original_captions` (Whisper words re-based, or LLM `translate_segments` per
  clip-lang cached in `caption_translation.json`), stamp `captions.json` `{"voiceover": false, "source": ...}`;
  render has no voice input (original at `audio.original_volume_after`, loudnorm + limiter); render.json gets
  `"voiceover": false` (absent = on, so every older render stays current).
- **Staleness:** captions are redone when `captions.json`'s mode differs from the effective mode (absent stamp
  = voice-over); the render when render.json's mode differs. Turning the voice-over on for a script with an
  empty narration writes one (`add_narration`, title/description/hashtags kept). voice.wav is never deleted by
  a mode switch, so switching back reuses a matching voice.
