# Auto Clipper roadmap

Priorities set by the team lead on 2026-11-23, after steps 1–4 and the first real run on a loud gaming video.

- **Mandatory**: needed before Auto Clipper is used for real channels.
- **Good to have**: clear quality or time wins for a Shorts/Reels workflow.
- **Nice to have**: worth doing later.

Effort: **S** about an hour, **M** half a day, **L** a day or more. **You** means the item needs the owner (accounts, listening, a Windows PC).

## Mandatory

| # | Task | Why | Effort |
|---|------|-----|--------|
| M1 | Finish and merge audio modes (PR #2): loudness-correct mix, short-voice handling, Demucs vocal separation, mode switch | Fixes the messy voice-over mix on loud sources | in progress |
| M2 | Fix Hindi narration length at the source: calibrate Hindi words per second on real output, generate long narrations sentence by sentence (no single 10-minute Voicebox call), shorter retry timeout, clear warning when still short | Hindi clips come out 10 s long on a 38 s clip; trimming hides it | M |
| M3 | Test every audio/video change on a loud, talk-heavy gaming clip as well as the Reagan sample | The mix bug passed every test on the quiet sample | S |
| M4 | First real publish, private/unlisted only: one English and one Hindi clip to YouTube, Facebook and Instagram; confirm the Instagram token type | Uploads are only tested against fakes | M, **you** connect accounts |
| M5 | Native Hindi speaker review of voice, captions and translated captions | Nobody on the team can judge naturalness | **you** |
| M6 | Run the README setup on a Windows PC (uv, FFmpeg, fonts, Node 22, HyperFrames, Demucs, Voicebox) and fix what breaks | Windows steps are written but never run | M, **you** have the PC |
| M7 | GitHub Actions CI: run the test suite on Linux and Windows for every pull request; turn on branch protection for `main` | The git workflow relies on tests passing before merge | S, **you** enable protection |
| M8 | Correct the "AI-generated" label: on when a synthetic voice is used, off (or your choice) in original-audio mode | Platform policy; today it is always on | S |
| M9 | Disk clean-up: delete or archive jobs from the UI, show disk use per job, optional auto-delete of source videos and stems after N days | Each job keeps the full source video, cuts, stems and renders | S |
| M10 | True-peak limiter so finals stay at or below -1.5 dBTP | One render peaked at -0.9 dBTP | S |

## Good to have

| # | Feature | Why it suits Shorts/Reels | Effort |
|---|---------|---------------------------|--------|
| G1 | Speaker/face tracking reframe: keep the face or the action centred in 9:16 instead of a fixed centre crop | Gaming and talking-head videos often have the subject off-centre or a facecam in a corner | L |
| G2 | Facecam + gameplay split layout (facecam on top, game below) | The standard layout for gaming Shorts | M |
| G3 | Moment review before rendering: see Claude's picks with score and reason, adjust start/end on a timeline, add or drop moments, then render | Saves rendering clips you would throw away | M |
| G4 | Hook text overlay for the first 2–3 seconds (from the moment's hook line) | Raises retention in the first seconds | S |
| G5 | Caption style presets and keyword emphasis (colour or emoji on key words) | Captions are the main visual of these formats | M |
| G6 | Scheduled publishing: pick a time per platform and channel, with daily limits | Posting all clips at once hurts reach | M |
| G7 | Cover/thumbnail frame picker per clip | Reels and Shorts use the cover in grids | S |
| G8 | Batch input: several URLs or a playlist in one go | Turns one session into a week of content | S |
| G9 | Your own voice: record a profile per language in Voicebox and pick it in Settings | The test narrator is a placeholder | S, **you** record |
| G10 | Background music library (royalty-free) with automatic ducking | Common in narrated Shorts; pairs with "voices removed" | M |
| G11 | Job-done notifications (desktop notification, optional webhook) | Jobs take 15–40 minutes | S |
| G12 | GPU support (CUDA) for Whisper, Demucs and Voicebox when a GPU is present | 5–10x faster on a machine with a GPU | M |
| G13 | Auto-resume interrupted jobs when the server starts again | Today a restart needs a manual Resume | S |

## Nice to have

| # | Feature | Why | Effort |
|---|---------|-----|--------|
| N1 | Bengali as a third language (voice if Voicebox supports it, otherwise captions-only mode) | Another audience from the same video | M |
| N2 | TikTok publishing | A fourth platform for the same clips | M |
| N3 | Performance feedback: pull views and retention per clip and show which moments worked | Lets you tune moment picking over time | L |
| N4 | A/B title variants per clip | Better packaging | S |
| N5 | Branding: logo watermark, intro/outro stingers | Channel identity | S |
| N6 | Speaker-coloured captions (diarization) for multi-person videos | Clearer conversations | M |
| N7 | Mobile-friendly UI and safe remote access (LAN with login) | Review clips from your phone | M |
| N8 | Docker image for Linux | One-command setup | M |
| N9 | Copyright risk hints: flag clips with recognised music | Avoids takedowns | L |

## Suggested order

1. Finish M1, then M2 and M3 together (all audio quality).
2. M7 and M9 (CI, disk), then M8 and M10.
3. G3 and G4 (better clips before rendering), then G1 or G2 for gaming videos.
4. M4, M5 and M6 whenever you have the accounts, a Hindi listener and a Windows PC.
5. G6 and G8 when you start posting regularly.

Each item is its own branch and pull request (see CONTRIBUTING.md). Items without dependencies on each other can run in parallel teams.
