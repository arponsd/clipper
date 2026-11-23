# Auto Clipper - Build Contract (Step 4: auto-publish)

Owner: Team Lead. Builders implement exactly these names, routes, shapes and limits. Additive extras are
fine; renames/removals need a TEAM_LOG note and lead sign-off. `docs/CONTRACT.md` and
`docs/CONTRACT_WEB.md` still hold (pathlib, `encoding="utf-8"`, `ensure_ascii=False`, single writer per
file, clean JSON errors, tests never hit the network).

Worktree: `/home/ptl-admin/projects/auto-clipper-step4`, branch `step4-publish`, based on 6288baf (steps 1+2).
Step 3 (HyperFrames captions) is being finished in the main checkout and also edits `config.py`, `cli.py`,
`server.py`, `app.js`, `styles.css`, `README.md`. **Keep every edit to those shared files a small,
self-contained insertion** (one import + one line where possible) so the later merge is trivial; all real
code goes into the NEW files listed in section 0.

## 0. Hard rules (apply to code AND to builders)

- Builders never post, upload or publish anything, never call a real YouTube/Facebook/Instagram/Google
  endpoint, never do a real OAuth login, never create an account or app. Tests use `httpx.MockTransport`
  (no new dependency: `httpx` is already in pyproject; no google-api-python-client, no facebook SDK).
- Real posting happens only later, by the user, with their own credentials, by pressing Publish (web) or
  `autoclipper publish ... --yes` (CLI) after setting `publish.dry_run = false`.
- `publish.dry_run` defaults to **true**; YouTube privacy defaults to **private**; every platform target is
  **disabled** by default. A fresh install can therefore only produce dry-run plans.
- Secrets (OAuth client secret, refresh/access tokens, Page/IG tokens, resumable session URIs) are never
  returned by any API, never written to job dirs, plans, logs, web.log, the server log or exceptions.
- Tests: `TMPDIR=~/.cache/ac-step4 uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-step4/bt-<role>`
  (mkdir -p first; /tmp is full). Manual runs only with `AUTOCLIPPER_DATA_DIR=~/.cache/ac-step4/data`.
  A test server, if any, on 127.0.0.1:8767 and stopped afterwards. Never touch 127.0.0.1:8765 or Voicebox.
  No git commit. No `.env` reads.

## 1. File ownership (no overlaps)

| Builder | Files |
|---|---|
| **core** | NEW `autoclipper/publish/__init__.py`, `settings.py`, `secrets.py`, `accounts.py`, `oauth_google.py`, `base.py`, `youtube.py`, `facebook.py`, `instagram.py`, `metadata.py`, `records.py`, `runner.py`, `service.py`, `cli.py`; `autoclipper/config.py` (ONE import + ONE field, section 3); `autoclipper/web/worker.py` (publish task kind, section 8); `autoclipper/cli.py` (ONE import + ONE call registering the `publish` / `accounts` subcommands, section 10); NEW `tests/publish_fakes.py`, `tests/test_publish_*.py` |
| **web** | NEW `autoclipper/web/publish_routes.py`; `autoclipper/web/server.py` (ONE import + ONE `install_publish_routes(app, ...)` call + the `publish_http` kwarg of `create_app`, section 9); NEW `autoclipper/web/static/publish.js`, `publish.css`; `static/index.html` (two tags + nav link), `static/app.js` (route table entries + one hook call, section 11); `tests/test_web_publish*.py`; `README.md` (section 13) |
| lead (done) | this contract, TEAM_LOG |

**Agreed interface** (the only shared surface): `autoclipper/publish/service.py` signatures and exceptions
(section 7), `Worker.submit_publish` / `Task.platforms` / `Worker.active_publish` (section 8), the JSON shapes in
sections 5-6, and `tests/publish_fakes.py` (core writes it first; web tests import it, do not edit it -
ask core in TEAM_LOG). Web never imports platform clients directly; core never imports FastAPI.

`models.py`, `pipeline.py`, `jobstore.py`, `stages/*`, `ffmpeg.py`, `llm.py`, `voicebox.py` are NOT edited
(publish reads `ffmpeg.probe` only). `pyproject.toml` unchanged (no new deps needed).

## 2. Researched platform APIs (official docs, fetched 2026-11-23)

Graph API latest is **v27.0** (released 2026-11-04; v26.0 2026-07-29). All Meta calls use one configurable
version, default `v27.0`. Tokens are sent as headers, never as query parameters (keeps them out of
URLs/logs): Google `Authorization: Bearer <access>`, Graph `Authorization: Bearer <token>`, rupload
`Authorization: OAuth <token>`.

### 2.1 YouTube Data API v3 - `videos.insert` (resumable)
Source: developers.google.com/youtube/v3/docs/videos/insert, /guides/using_resumable_upload_protocol, /docs/videos.
1. Access token: `POST https://oauth2.googleapis.com/token` form `client_id, client_secret,
   grant_type=refresh_token, refresh_token` -> `{access_token, expires_in, token_type:"Bearer"}`.
2. Start session: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status&notifySubscribers=<true|false>`
   headers `Authorization`, `Content-Type: application/json; charset=UTF-8`, `X-Upload-Content-Length: <bytes>`,
   `X-Upload-Content-Type: video/mp4`; body `{"snippet": {...}, "status": {...}}` -> **200** + `Location:` session URI.
3. Upload: `PUT <session URI>` in chunks of `publish.chunk_mb` MiB (a multiple of 256 KiB; all but the last
   equal), `Content-Length: <chunk>`, `Content-Range: bytes <first>-<last>/<total>` -> **308** (with `Range:
   bytes=0-<n>`) until the last chunk -> **200/201** with the `video` resource (`id`).
4. Resume: `PUT <session URI>` `Content-Length: 0`, `Content-Range: bytes */<total>` -> 308 (+`Range`, maybe
   `Retry-After`) or the final 200/201. Retry on no response / 500, 502, 503, 504 with exponential backoff;
   404 = session expired -> new session; any other 4xx = permanent.
- Scopes: `https://www.googleapis.com/auth/youtube.upload` (upload) + `https://www.googleapis.com/auth/youtube.readonly`
  (only to show the connected channel via `GET https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true`, 1 unit).
- Limits: file <= 256 GB, MIME `video/*` or `application/octet-stream`. Quota: insert is "100 calls per day"
  in the Video Uploads bucket (1 unit each). Errors: `uploadLimitExceeded` (400), `quotaExceeded` /
  `rateLimitExceeded` / `userRateLimitExceeded` (403/429), `invalidTitle`, `invalidDescription`, `invalidTags`,
  `invalidCategoryId` (400), `forbiddenPrivacySetting` (403), 401 `invalid_grant` on refresh = reconnect.
- Fields: `snippet.title` <= 100 chars, no `<` `>`; `snippet.description` <= 5000 **bytes** UTF-8, no `<` `>`;
  `snippet.tags` total <= 500 chars (commas count; a tag with a space counts its two quotes);
  `snippet.categoryId`; `snippet.defaultLanguage` / `defaultAudioLanguage` (= clip lang);
  `status.privacyStatus` `private|unlisted|public` (API default is public, **we always send it**);
  `status.selfDeclaredMadeForKids`, `status.containsSyntheticMedia` (realistic altered/synthetic disclosure).
  The docs now say unverified API projects are not restricted to private viewing; we still default private.
- **Shorts:** a vertical or square video up to **3 minutes** is classified as a Short (YouTube Help
  15424877). Preflight: duration <= 180 s and height >= width; our renders are 1080x1920. Watch URL:
  `https://www.youtube.com/shorts/<id>`.
- OAuth (installed app, loopback): `https://accounts.google.com/o/oauth2/v2/auth?client_id&redirect_uri=http://127.0.0.1:<port>/<path>&response_type=code&scope=<2 scopes space-separated>&state&code_challenge&code_challenge_method=S256&access_type=offline&prompt=consent`
  (`localhost` also accepted). Code exchange `POST https://oauth2.googleapis.com/token` `client_id,
  client_secret, code, code_verifier, grant_type=authorization_code, redirect_uri`. Revoke `POST
  https://oauth2.googleapis.com/revoke` form `token=<refresh>`. PKCE verifier 43..128 unreserved chars,
  challenge `BASE64URL(SHA256(verifier))`. An External app in "Testing" status gets refresh tokens that
  expire in **7 days** (README tells the user to publish the consent screen or reconnect weekly). Max 100
  refresh tokens per Google account per client (oldest silently invalidated).

### 2.2 Facebook Page Reels - `video_reels` (Graph v27.0)
Source: developers.facebook.com/docs/video-api/guides/reels-publishing.
1. `POST https://graph.facebook.com/v27.0/{page-id}/video_reels` form `upload_phase=start` -> `{"video_id", "upload_url"}`.
2. `POST https://rupload.facebook.com/video-upload/v27.0/{video-id}` headers `Authorization: OAuth <page token>`,
   `offset: 0` (or resume offset), `file_size: <bytes>`, `Content-Type: application/octet-stream`, body = file
   -> `{"success": true}`.
3. Status: `GET https://graph.facebook.com/v27.0/{video-id}?fields=status` -> `status.video_status`
   (`processing|ready|upload_failed|error|...`), `status.uploading_phase.bytes_transfered` (resume offset).
4. `POST .../{page-id}/video_reels` form `video_id, upload_phase=finish, video_state=PUBLISHED, description, title,
   is_ai_generated=true|false` (setting `facebook.is_ai_generated`, default true; lead fix F4). Sent ONCE (no auto-retry)
   -> `{"success": true}`; then poll step 3 until `publishing_phase.status == "complete"` (or video_status
   `ready`), error states -> failed. URL `https://www.facebook.com/reel/<video_id>`.
- Token: **Page access token** of a user with CREATE_CONTENT on the Page; permissions `pages_show_list`,
  `pages_read_engagement`, `pages_manage_posts`.
- Specs: mp4, H.264/H.265, **9:16**, 1080x1920 recommended (min 540x960), 24-60 fps, **3-90 s**, AAC-LC stereo
  48 kHz >= 128 kbps. Rate: **30 API-published reels per Page per 24 h moving window**.

### 2.3 Instagram Reels - content publishing (Facebook Login host, Graph v27.0)
Source: developers.facebook.com/docs/instagram-platform/content-publishing and .../ig-user/media.
1. Quota: `GET https://graph.facebook.com/v27.0/{ig-user-id}/content_publishing_limit?fields=quota_usage,config`
   (100 API-published posts per 24 h moving window).
2. Container: `POST https://graph.facebook.com/v27.0/{ig-user-id}/media` form `media_type=REELS,
   upload_type=resumable, caption, share_to_feed=true|false` -> `{"id": <container>, "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/<container>"}`.
3. Upload: `POST https://rupload.facebook.com/ig-api-upload/v27.0/{container-id}` headers `Authorization: OAuth <token>`,
   `offset: 0`, `file_size: <bytes>`, body = file -> `{"success": true}`.
4. Poll `GET .../{container-id}?fields=status_code,status` starting at `poll_interval_seconds` (10 s) and
   doubling up to 60 s (docs: about once a minute, at most 5 minutes), total `poll_timeout_seconds` (default
   300; lead fix F6) until `FINISHED`
   (`ERROR`/`EXPIRED` -> failed; containers expire after 24 h).
5. `POST .../{ig-user-id}/media_publish` form `creation_id=<container>` -> `{"id": <media-id>}` (sent ONCE, no
   auto-retry; on any error the container `status_code` is read: `PUBLISHED` = live, the media id is recovered
   from `GET .../{ig-user-id}/media?fields=id,caption,permalink,timestamp` by exact caption + time, else
   `failed` with error code `maybe_published` and nothing is re-posted without `force`);
   `GET .../{media-id}?fields=permalink` -> URL.
- Needs an Instagram **professional** (business/creator) account linked to the Page. Permissions
  `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` (+`ads_management`/`ads_read` with a
  Business Manager role). The docs specify a user token; a Page token from the same user is accepted on
  graph.facebook.com, so the connect form takes an optional separate IG token, default = the Page token.
- Specs: MOV/MP4, H.264/HEVC, AAC <= 48 kHz, 23-60 fps, width <= 1920, **3 s - 15 min**, **<= 300 MB**, 9:16
  recommended. Caption <= 2200 chars, <= 30 hashtags, <= 20 @mentions. 400 containers / 24 h.
- Meta errors: code 190 (token expired/invalid -> reconnect), 10/200 (permission), 4/17/32/613 (rate
  limits), code 9 subcode 2207042 (IG publish limit), 2207026 (unsupported video format), `is_transient` true
  -> retry.

## 3. Settings - `autoclipper/publish/settings.py` (core), wired in `config.py`

`config.py` gets exactly `from .publish.settings import PublishSettings` and, as the LAST `Settings` field,
`publish: PublishSettings = Field(default_factory=PublishSettings)`. `publish/settings.py` imports only
pydantic/re (no import of `config`, avoids a cycle). Old settings.json files load (all defaults).

```python
_ID_YT = r"\AUC[A-Za-z0-9_-]{22}\Z"   # channel id; "" = not mapped yet
_ID_NUM = r"\A[0-9]{1,32}\Z"           # Page id / IG user id; "" = not mapped yet
class YouTubeTarget(BaseModel):
    enabled: bool = False
    channel_id: str = ""                      # filled by connect; publish refuses if account differs
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = Field("22", pattern=r"\A[0-9]{1,3}\Z")   # 22 = People & Blogs
    made_for_kids: bool = False
    contains_synthetic_media: bool = True     # AI narration voice -> disclose by default
    notify_subscribers: bool = False
    add_shorts_hashtag: bool = True           # append "#Shorts" to the description if absent
class FacebookTarget(BaseModel):
    enabled: bool = False
    page_id: str = ""
class InstagramTarget(BaseModel):
    enabled: bool = False
    ig_user_id: str = ""
    share_to_feed: bool = True
class LangTargets(BaseModel):
    youtube: YouTubeTarget = Field(default_factory=YouTubeTarget)
    facebook: FacebookTarget = Field(default_factory=FacebookTarget)
    instagram: InstagramTarget = Field(default_factory=InstagramTarget)
class PublishSettings(BaseModel):
    dry_run: bool = True                                   # GLOBAL: nothing is sent while true
    graph_api_version: str = Field("v27.0", pattern=r"\Av[1-9][0-9]?\.[0-9]\Z")
    en: LangTargets = Field(default_factory=LangTargets)
    hi: LangTargets = Field(default_factory=LangTargets)
    allow_same_account: bool = False   # en and hi on the same channel/page/IG -> preflight problem unless true
    chunk_mb: int = Field(8, ge=1, le=256)                 # YouTube chunk = chunk_mb * 1 MiB (multiple of 256 KiB)
    max_retries: int = Field(3, ge=0, le=10)               # transient errors per request
    poll_interval_seconds: float = Field(10.0, ge=1, le=60)
    poll_timeout_seconds: float = Field(300.0, ge=60, le=3600)
    def target(self, lang: str, platform: str) -> BaseModel
```
Validators: the id patterns above (`\Z`, not `$`); `channel_id`/`page_id`/`ig_user_id` "" allowed.
The generic Settings page (`#/settings`) must NOT render the `publish` section (the Accounts page owns it);
`PUT /api/settings` keeps accepting it (whole object, strict) so the step-2 contract stays true; a body WITHOUT
`publish` keeps the stored section (app.js never sends it), and the write holds `service._settings_lock` (SEC-4/F2).

## 4. Secrets - `autoclipper/publish/secrets.py` (core)

`secrets_dir() = data_dir() / "secrets"`, created with mode 0o700 on POSIX. On Windows (chmod is meaningless and
the data dir may be anywhere) it gets an explicit owner-only ACL once per process:
`icacls <dir> /inheritance:r /grant:r <DOMAIN>\<USER>:(OI)(CI)F`, failure logged (lead fix SEC-3). Files (JSON, utf-8):

| File | Content |
|---|---|
| `google_client.json` | `{"client_id", "client_secret", "project_id"}` from the user's `client_secret.json` `"installed"` block |
| `youtube_<lang>.json` | `{"refresh_token", "scopes": [...], "channel_id", "channel_title", "connected_at"}` |
| `meta_<lang>.json` | `{"page_id", "page_name", "page_token", "ig_user_id"\|null, "ig_username"\|null, "ig_token"\|null, "connected_at"}` |
| `publish_ledger.jsonl` | NOT secret, but lives here: one line per live publish `{"t", "platform", "account_id", "lang"}` (rate windows) |

```python
def write_secret(name: str, data: dict) -> Path   # name in a fixed allowlist; tmp via os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)
                                                  # in the same dir, fsync, os.replace; chmod 0o600 after replace
def read_secret(name: str) -> dict | None         # missing/unreadable -> None (warning logged WITHOUT contents)
def delete_secret(name: str) -> bool
def redact(text: str) -> str                      # replaces every known secret value (and Bearer/OAuth <x>) by "***"
```
Rules: only `accounts.py`/`oauth_google.py`/platform clients read secrets; nothing else; `__repr__` of
account objects hides tokens; `PublishError` messages pass through `redact`; set `logging.getLogger("httpx")`
to WARNING inside publish (its INFO lines print URLs). Tests assert file mode `0o600` on POSIX and that no
secret value appears in any API response, plan file, status file, web.log or caplog.

## 5. Files per clip-lang (`clips/NN/<lang>/publish/`, core writes)

- `<platform>.json` - the **request plan** (dry-run output; a live run rewrites it with `"dry_run": false`
  right before sending). Single writer: the dry-run request thread holds the worker lock (section 8), the
  live run is the worker/CLI.
- `<platform>.status.json` - live state, single writer = the live runner. Read-only for everyone else.

`Plan` (one platform):
```json
{"platform": "youtube", "lang": "hi", "job_id": "...", "clip": 1, "dry_run": true, "created_at": 1763.0,
 "api_version": "v3",                       // "v27.0" for facebook / instagram
 "account": {"id": "UC...", "name": "My Hindi channel"} | null,
 "video": {"path": "clips/01/hi/final.mp4", "size": 11156919, "duration": 28.84, "width": 1080,
           "height": 1920, "sha256": "64 hex"},
 "metadata": {...platform payload, section 6...},
 "requests": [{"step": "start_session", "method": "POST", "url": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status&notifySubscribers=false",
               "headers": {"Authorization": "Bearer ***", "X-Upload-Content-Length": "11156919", "X-Upload-Content-Type": "video/mp4", "Content-Type": "application/json; charset=UTF-8"},
               "body": {...} | null, "note": "returns Location: <session URI>"}, ...],
 "limits": {"max_duration": 180, "max_bytes": 274877906944, "rate": "100 uploads/day (quota)"},
 "problems": ["not connected", "duration 95.0 s > 90 s (Facebook Reels limit)"],   // [] = would be sent
 "warnings": ["privacy is private: only you will see it"]}
```
Every token in `headers` is the literal `***`; ids/URLs that depend on a response are written as
`{video_id}` / `<session URI>` placeholders. A plan never contains a secret.

`Status`:
```json
{"platform": "facebook", "lang": "hi", "job_id": "...", "clip": 1,
 "state": "queued|uploading|processing|publishing|published|failed|interrupted",
 "progress": 0.0, "message": "uploading 40%", "attempts": 1,
 "error": null | {"kind": "auth|quota|rate_limit|invalid|unsupported_video|transient|processing|network|unknown",
                  "message": "Facebook: the Page has reached 30 API-published reels in 24 h; try after 14:05",
                  "code": "190" | "quotaExceeded" | null, "retry_after": 3600 | null},
 "remote_id": null | "1234", "url": null | "https://www.facebook.com/reel/1234",
 "account": {"id": "...", "name": "..."}, "video_sha256": "...", "video_size": 11156919,
 "idempotency_key": "..." | null, "started_at": 1763.0, "updated_at": 1763.0, "published_at": null | 1763.0,
 "resume": {...}}          // INTERNAL (session URI, video_id, container id, offset); stripped from every API/CLI output
```
- "published record" = status with `state == "published"` and `remote_id`. A later publish of that
  clip/lang/platform without `force` does not send anything and returns this record (`already_published`).
- `uploading|processing|publishing` with no active publish task = shown as `interrupted` (computed on
  read, the file is not rewritten by readers). A new run with a `resume` block resumes: YouTube probes the
  session URI (`bytes */total`; a 200/201 recovers the already-created video id => no duplicate), Facebook
  reads `bytes_transfered` and continues, Instagram reuses a container younger than 23 h if `FINISHED`.
- Commit point (lead fixes SEC-1/SEC-2/F1): before IG `media_publish` / FB `finish` the resume record gets
  `"sent": true`. From then on the runner never deletes it on error (only success or `force` clears it), and
  the next run checks the platform first: an IG container in `PUBLISHED` (any age, any render) is never
  re-published; an FB video whose `publishing_phase` started skips `finish` and only polls. A clip/lang/platform
  whose post may be live is never posted again without `force`.
- Preflight problem `narration changed since the last render - re-render the clip before publishing` when
  script.json's narration hash differs from voice.json's (web `voice_stale`; lead fix F3).
- `stale`: published `video_sha256` != current final.mp4 sha256 -> view flag `published_render_outdated: true`.

## 6. Metadata mapping - `autoclipper/publish/metadata.py` (core)

Input `metadata.json` `{"language","title","description","hashtags"}` (+ fallback to script.json when missing
-> problem "metadata.json missing - re-render"). Hashtags normalised to `#` + text without spaces/`#`.
- **youtube**: `title` = title with `<`/`>` removed, <= 100 chars (cut on a word boundary + "…" if needed);
  `description` = description + "\n\n" + hashtags joined by " " (+ " #Shorts" when `add_shorts_hashtag`
  and absent), `<`/`>` removed, cut to 5000 UTF-8 bytes on a code-point boundary; `tags` = hashtags without
  `#`, kept in order while the YouTube count (len + commas + 2 for a tag with a space) <= 500;
  `categoryId`, `defaultLanguage` = `defaultAudioLanguage` = lang; status `privacyStatus`,
  `selfDeclaredMadeForKids`, `containsSyntheticMedia`.
- **facebook**: `title` = title (<= 255 chars), `description` = description + "\n\n" + hashtags.
- **instagram**: `caption` = title + "\n\n" + description + "\n\n" + hashtags, at most 30 hashtags and
  2200 chars (description cut first, hashtags kept), `share_to_feed`.
Pure functions, unit-tested with the sample hi/en metadata (Devanagari byte counting!).

## 7. Core API - `autoclipper/publish/` (agreed interface = `service.py`)

```python
PLATFORMS = ("youtube", "facebook", "instagram"); LANGS = ("en", "hi")
class PublishError(RuntimeError):       # base.py; message already redacted
    kind: str; platform: str; code: str | None; retry_after: float | None; transient: bool
@dataclass
class PublishItem:                       # base.py: what gets posted
    job_id: str; clip: int; lang: str; video: Path; size: int; duration: float; width: int; height: int
    sha256: str; metadata: dict          # metadata.json content
class Publisher(Protocol):               # base.py; youtube.py / facebook.py / instagram.py implement it
    platform: str
    def plan(self, item: PublishItem, target, account: dict | None) -> dict          # no network, Plan shape
    def preflight(self, item, target, account) -> list[str]                          # problems, no network
    def publish(self, item, target, account, *, status: StatusWriter,
                on_progress: Callable[[float, str], None]) -> dict                   # {"remote_id","url"}
def make_publisher(platform: str, settings: PublishSettings, *, http: httpx.Client | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> Publisher
```
- Clients use one `httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)` given in
  (tests: `httpx.Client(transport=httpx.MockTransport(fake))`). Uploads stream the file (no full read into RAM).
- Error mapping (`base.classify`): network/timeout/5xx -> `transient` (retry `max_retries`, backoff 2^n s,
  honour `Retry-After`), 401/`invalid_grant`/Meta 190 -> `auth` ("<platform> login expired - reconnect on
  the Accounts page"), YouTube `quotaExceeded`/`uploadLimitExceeded` -> `quota` ("YouTube daily upload quota
  used up; it resets at midnight Pacific time"), 429/`rateLimitExceeded`/Meta 4,17,32,613/IG 2207042 ->
  `rate_limit`, Meta 2207026 -> `unsupported_video`, other 4xx -> `invalid` (upstream message, redacted).
- Local rate guard (`records.window_count`): from `publish_ledger.jsonl`, preflight problem when Facebook
  >= 30 per Page / 24 h, Instagram: `content_publishing_limit` checked live at run start (not in dry-run).

`service.py` (called by web routes and CLI; raises the exceptions below, never FastAPI types):
```python
class PublishInputError(Exception):  errors: dict[str, list[str]]   # -> 422
class PublishConflict(Exception):    message: str; errors: dict     # -> 409
class PublishNotFound(Exception)                                    # -> 404
class PublishUpstreamError(Exception)                               # -> 503 (platform unreachable)
class OAuthStateError(Exception)                                    # bad/expired/used state -> 400 page

def accounts_status(settings: Settings) -> dict                    # section 9.1 body
def set_google_client(data: object) -> dict                        # validates + writes google_client.json
def clear_google_client() -> None
def begin_youtube_connect(lang: str, redirect_uri: str) -> dict    # {"auth_url","state","expires_in":600,"redirect_uri"}
def complete_youtube_connect(state: str, code: str | None, error: str | None, *,
                             http: httpx.Client | None = None) -> dict   # AccountStatus; fills settings channel_id
def connect_meta(lang: str, *, page_id: str, page_token: str, ig_user_id: str | None,
                 ig_token: str | None, http: httpx.Client | None = None) -> dict   # {"facebook": AS, "instagram": AS}
def disconnect(lang: str, platform: str, *, revoke: bool = True, http=None) -> None
def get_publish_settings() -> dict;  def put_publish_settings(body: object) -> dict
def publish_view(job_id: str, clip: int, lang: str, settings: Settings, *, active: dict | None) -> dict  # 9.3
def dry_run(job_id: str, clip: int, lang: str, platforms: list[str], settings: Settings) -> dict
def check_live(job_id, clip, lang, platforms, settings, *, force: bool) -> tuple[list[str], dict]
    # (platforms to run, {platform: published Status already there}); PublishConflict with per-platform
    # problems when any requested platform is not ready (dry_run on, disabled, not connected, account
    # mismatch, same account for en/hi, final.mp4 missing, limits)
```
`runner.run_publish(job_id, clip, lang, *, platforms, force, settings, log, http=None, sleep=time.sleep,
idempotency_key=None) -> dict` (`{platform: Status}`) - runs platforms in the given order, one failing
platform does not stop the next, re-checks everything (`check_live`), writes status atomically, appends the
ledger on success, calls `log` (=> TaskCancelled works between chunks/polls). Pending OAuth states live in
`oauth_google.PENDING` (in-memory, lock, TTL 600 s, max 10, single use; verifier never leaves the process).
`oauth_google.run_loopback_flow(lang, *, open_browser=True, timeout=300)` = CLI flow: a one-shot
`http.server` on `127.0.0.1:<random port>`, path `/`, then `complete_youtube_connect`.

## 8. Worker - `autoclipper/web/worker.py` (core)

- `TaskKind = Literal["run_job", "rerender", "publish"]`; `Task.platforms: list[str] | None = None`,
  `Task.force: bool = False`; `to_dict` adds `"platforms"` (null for other kinds; additive to the step-2 Task).
- `Worker(..., publish: Callable[..., Any] = runner.run_publish)`.
- `submit_publish(job_id, clip, lang, platforms, *, force=False, idempotency_key=None) -> tuple[Task, bool]`:
  `WorkerConflict` if a `run_job` of the job or a `rerender` of that clip-lang is queued/running; an active
  publish for the same clip-lang with the same platform set -> `(it, False)`; different set -> `WorkerConflict`.
- `submit_rerender` additionally conflicts with an active publish of that clip-lang ("clip is being published").
- `active_publish(job_id, clip, lang) -> Task | None`; `exclusive(...)` is unchanged for script PUT (a
  running publish does not block script edits); new `exclusive_publish(job_id, clip, lang)` context manager
  for the dry-run writer (conflicts with an active publish/rerender of that clip-lang or run_job).
- `_execute` publish branch: inside `hold_job_lock(job_id)`: `self._publish(job_id, clip, lang,
  platforms=..., force=..., settings=settings, log=log_line, idempotency_key=...)`. Task `failed` with the
  first failing platform's message when all platforms failed; `done` otherwise (per-platform detail is in
  the status files). TaskCancelled -> cancelled; status files left `interrupted`-resumable.

## 9. HTTP API (web; routes in `autoclipper/web/publish_routes.py`)

`server.py`: `create_app(..., publish_http: Callable[[], httpx.Client] | None = None)`, then
`install_publish_routes(app, state, publish_http=publish_http)`. Path params reuse the step-2 validators
(`job_id`, `clip` 0..999, `lang` en|hi). `platform` param: `youtube|facebook|instagram` (422 otherwise;
disconnect also accepts `meta` = facebook+instagram). Token, Host and Origin guards apply to all `/api/*`
(401 missing, 403 wrong/host/origin). Error body always `{"message", "errors"}`; 500 never leaks.

| Method | Path | Success | Errors |
|---|---|---|---|
| GET | `/api/publish/settings` | 200 `PublishSettings` dump | - |
| PUT | `/api/publish/settings` | 200 saved (only `publish` section of settings.json replaced, atomic) | 422 unknown key / bad type (strict) / bad id pattern |
| GET | `/api/publish/accounts` | 200 Accounts (9.1) | - |
| PUT | `/api/publish/google-client` | 200 `{"configured": true, "client_id_hint": "…apps.googleusercontent.com"}` | 422 not an object, no `installed` block (a `web` client -> "create a Desktop app client"), missing/empty `client_id`/`client_secret`, body > 64 KiB (413) |
| DELETE | `/api/publish/google-client` | 204 | - |
| POST | `/api/publish/accounts/{lang}/youtube/connect` | 200 `{"auth_url","state","expires_in","redirect_uri"}` | 409 no Google client; 409 request Host not loopback |
| GET | `/oauth/google/callback?state&code\|error` | 200 small HTML "Connected <channel> for Hindi - you can close this tab" | 400 HTML (bad/expired/used state, `error=access_denied`), 503 HTML (Google unreachable). PUBLIC (no token; the one-time state is the guard), Host guard applies, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, same CSP, no script |
| POST | `/api/publish/accounts/{lang}/meta/connect` | 200 `{"facebook": AS, "instagram": AS}` | 422 bad body (`page_id` digits 1..32, `page_token` 20..1024 chars no whitespace, `ig_user_id` digits or null, `ig_token` like page_token or null; extra keys), 422 `errors.page_token` "rejected by Facebook (code 190)", 422 `errors.ig_user_id` "not the Instagram account linked to this Page"; 503 unreachable |
| DELETE | `/api/publish/accounts/{lang}/{platform}?revoke=true` | 204 (idempotent; file removed; settings id kept, enabled untouched) | 422 bad platform |
| GET | `/api/jobs/{job_id}/clips/{clip}/{lang}/publish` | 200 View (9.3) | 404 job/clip/lang |
| POST | `/api/jobs/{job_id}/clips/{clip}/{lang}/publish` | dry-run 200 `{"dry_run": true, "plans": {p: Plan}}`; live 202 `{"dry_run": false, "task": Task, "already_published": {p: Status}}`; live with nothing left to send / same key replay / same active task -> 200 same shape | 404; 422 body; 409 (below) |

Publish POST body `PublishIn` (`extra="forbid"`, strict): `platforms`: 1..3 unique of the enum;
`dry_run`: bool | null (null = `settings.publish.dry_run`); `confirm`: bool = false; `force`: bool = false.
Header `Idempotency-Key` (step-2 format; **required for a live publish**, 422 `errors["Idempotency-Key"]`).
- Effective dry-run = `body.dry_run` if not null, else settings. `dry_run:false` while settings say true ->
  **409** "publishing is in dry-run mode - turn dry run off on the Accounts page".
- Live: `confirm` must be true -> else 422 `errors.confirm` "set confirm to true to publish for real".
- 409 "clip has no final render yet" (final.mp4/metadata.json missing), 409 when a run_job/rerender is
  active, 409 `check_live` problems `{"message": "cannot publish", "errors": {"youtube": ["not connected"]}}`.
- Dry-run never needs connection (plan lists `problems`), never touches the network, writes `<platform>.json`.
- Idempotency: key stored in `data/web/idempotency.json` as `"publish:<key>"` with the canonical
  `job/clip/lang/body`: same -> 200 same task; different body -> 409 (step-2 text).

9.1 Accounts: `{"dry_run": true, "google_client": {"configured": bool, "client_id_hint": str | null},
"langs": {"en": {"youtube": AS, "facebook": AS, "instagram": AS}, "hi": {...}}}` where
`AS = {"connected": bool, "enabled": bool, "account_id": str | null, "account_name": str | null,
"connected_at": float | null, "problems": [str]}` (problems e.g. "settings channel differs from the
connected one", "same account as en"). Nothing else - no token, no client secret, no scopes beyond names.

9.3 View: `{"dry_run": bool, "final": bool, "final_sha256": str | null, "task": Task | null,
"platforms": {"youtube": {"enabled", "connected", "account": {"id","name"} | null, "plan": Plan | null,
"status": Status-without-resume | null, "published_render_outdated": bool, "problems": [str]}, ...}}`.
`JobDetail.clips[].langs[lang]` gains `"publish_url": "/api/jobs/<id>/clips/<n>/<lang>/publish"` (additive).

## 10. CLI (core) - `autoclipper/publish/cli.py`, registered from `cli.py` by `register(sub, common)`

```
autoclipper publish <job> --clip N --lang en|hi --platform youtube|facebook|instagram|all [--platform ...]
                    [--dry-run] [--force] [--yes]
autoclipper accounts                                   # status table (= GET /api/publish/accounts), exit 0
autoclipper accounts google-client <client_secret.json>
autoclipper accounts connect youtube --lang hi [--no-browser]     # loopback flow, prints the URL
autoclipper accounts connect meta --lang hi --page-id ID [--ig-user-id ID]
                    # tokens read with getpass (or env AUTOCLIPPER_PAGE_TOKEN / AUTOCLIPPER_IG_TOKEN), never argv
autoclipper accounts disconnect youtube|facebook|instagram|meta --lang hi [--no-revoke]
```
`publish`: default = settings dry_run; `--dry-run` forces a plan and prints it (tokens `***`); a live run
needs `dry_run=false` in settings AND `--yes` (otherwise prints what would be posted and exits 2), holds
`hold_job_lock`, prints per-platform progress and the final URLs. Exit 0 all ok / already published,
1 any platform failed, 2 usage/not ready (`error: <message>`, no traceback).

## 11. UI (web) - `static/publish.js`, `publish.css`

- `index.html`: `<link rel="stylesheet" href="/static/publish.css">`, `<script src="/static/publish.js" defer>`
  before app.js, nav link `#/accounts` (`data-nav="accounts"`). `publish.js` exposes `window.ACPublish =
  {viewAccounts(ctx), panel(ctx, jobId, clip, lang, container)}` using app.js's exported helpers (app.js
  adds `window.ACApp = {api, el, $main, ...}` - one line); app.js adds the `#/accounts` route and calls
  `ACPublish.panel(...)` at the end of `viewClip`. CSP unchanged (no inline code).
- Accounts page: dry-run switch (big notice "Dry run is ON - nothing is posted" / red "Dry run is OFF -
  Publish posts for real"), Google client upload (file input -> FileReader -> PUT JSON), per language a card
  with YouTube (Connect opens `auth_url` in a new tab, then polls accounts every 2 s for up to 10 min /
  Disconnect), Facebook+Instagram form (page id, token as `type=password autocomplete=off`, IG id, optional IG
  token; cleared after submit), per platform enabled + YouTube privacy/category/kids/synthetic/notify, IG
  share_to_feed; mapping summary "English -> <channel>, Hindi -> <channel>"; problems shown.
- Clip review panel: checkboxes per platform (disabled with the reason when not enabled/connected), "Preview
  (dry run)" button -> shows each plan (metadata, request list, file size) in `<details>`; "Publish" button
  only when dry_run is off -> confirm dialog (`<dialog>`) listing platform, account name, privacy, title,
  and "This posts publicly to …" for public, with a typed confirmation for `force`; POST with a fresh
  `Idempotency-Key`; then polls `GET .../publish` every 2 s while a task is active: per-platform state,
  progress bar, message, error kind/message (quota/rate-limit texts verbatim), links to the published URL.
  Hindi text with `lang="hi"`. Responsive to 360 px, keyboard accessible.

## 12. Tests

Fakes (core, `tests/publish_fakes.py`): `FakeGoogle` (token, refresh, revoke, channels, resumable start ->
Location, chunk PUT -> 308/201, `bytes */N` probe, injectable 5xx/404/403 quotaExceeded), `FakeGraph`
(video_reels start/finish, rupload with offset/file_size checks, status sequence, IG limit, container ->
status IN_PROGRESS..FINISHED/ERROR, media_publish, permalink, error codes 190/4/9-2207042/2207026), each as
an `httpx.MockTransport` handler recording requests; `make_final(dir, seconds, w, h)` (tiny ffmpeg video
or a stub probe). Asserts on recorded requests: exact URLs/version, headers, form fields, no token in URLs.

Core (`tests/test_publish_*.py`): settings defaults/validation/round-trip and old settings.json loads;
secrets 0600 + atomic + redact; metadata mapping limits (100 chars, 5000 bytes Devanagari, 500 tag rule,
2200/30 hashtags); plan = exact shape, no network (MockTransport that fails on any call), no secrets;
each client happy path; chunking 256 KiB multiples; resume (YouTube 308 Range, 404 -> new session, crash
after upload -> recovered id no duplicate; FB offset); transient retry then success; every error kind ->
message; IG polling timeout/ERROR; idempotency (published record -> no request; force -> new); account
mismatch / same account; worker publish task (FIFO, conflicts with rerender/run_job, same-set reuse,
cancel between chunks, failure does not kill the thread); CLI parse + dry-run output + live refusal
without `--yes` / with dry_run on; OAuth PKCE (challenge math, state single use, TTL, error=access_denied).

Web (`tests/test_web_publish*.py`, TestClient base_url `http://127.0.0.1:8765`, `publish_http` = fakes):
for every route happy path; 422 (missing/extra keys, wrong types, empty/very long strings, bad enums,
duplicate platforms, 4 platforms, bad ids, oversized client JSON); 401 no token, 403 wrong token / bad
Host / cross Origin on writes; 404 unknown job/clip/lang; exact JSON structure (keys AND types) of
Accounts, View, Plan, Status, Task with platforms; side effects (settings.json publish section, secrets
files created/removed with 0600, plan files, status files, idempotency.json, no request made in dry-run,
409 matrix of section 9); callback page (good state -> secret written, reused state -> 400, no token in
HTML); a full live publish through the worker with fakes for all three platforms, then a second POST
returning `already_published`; grep of responses/logs for every secret value = none.

## 13. README (web builder)

New section "Publishing (step 4)": what is posted where (English clips -> the en channel/Page/IG,
Hindi -> the hi ones; nothing is posted while dry run is on; YouTube uploads default private), and
step-by-step credential setup with Windows (PowerShell paths `%USERPROFILE%`) and Linux commands:
- YouTube: Google Cloud project -> enable YouTube Data API v3 -> OAuth consent screen (External, add
  yourself as test user; "Testing" refresh tokens last 7 days, publish the app to avoid it) -> Credentials
  -> OAuth client ID type **Desktop app** -> download `client_secret.json` -> Accounts page or `autoclipper
  accounts google-client`. Connect English and Hindi separately, choosing the right channel (brand account)
  in Google's chooser each time. Quota note.
- Facebook/Instagram: Meta developer app (Business type) with Facebook Login; Instagram professional
  account linked to the Page; Graph API Explorer -> user token with `pages_show_list,
  pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publish` -> exchange for a
  long-lived token (`GET https://graph.facebook.com/v27.0/oauth/access_token?grant_type=fb_exchange_token&client_id=…&client_secret=…&fb_exchange_token=…`,
  ~60 days) -> `GET /v27.0/me/accounts` gives the Page id and a Page token that does not expire ->
  `GET /v27.0/{page-id}?fields=instagram_business_account` gives the IG user id. Paste them on the
  Accounts page (or CLI connect). Limits: FB 30 reels / 24 h, 3-90 s; IG 100 posts / 24 h, <= 300 MB.
- Where secrets live (`data/secrets/`, 0600; Windows ACL note), how to disconnect/revoke, and that the
  app never shows a token again.

## 14. Order of work

1. core: `publish/settings.py` + config wiring + `service.py` stubs with the final signatures +
   `tests/publish_fakes.py` FIRST; note "interface ready" in TEAM_LOG. 2. In parallel: core clients/runner/
   worker/CLI; web routes/UI/README against the stubs. 3. Each runs its own tests with the TMPDIR/basetemp
   command; full suite green before sign-off. 4. Lead review.
