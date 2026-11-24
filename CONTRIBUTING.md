# Contributing: git workflow

This is how every change to Auto Clipper is made, by people and by agent teams alike.

## Branches

- `main` is always working and tested. Nobody commits to it directly; changes land through pull requests.
- Every task gets its own branch, created from `main`:

  | Prefix   | Use for                    | Example                  |
  |----------|----------------------------|--------------------------|
  | `feat/`  | new behaviour              | `feat/audio-modes`       |
  | `fix/`   | bug fixes                  | `fix/title-quotes`       |
  | `docs/`  | documentation only         | `docs/git-workflow`      |
  | `chore/` | tooling, dependencies      | `chore/bump-yt-dlp`      |
  | `test/`  | tests only                 | `test/publish-contract`  |

- Independent tasks run in parallel, each on its own branch in its own worktree, so they never edit the same checkout:

  ```bash
  git fetch origin
  git worktree add ../auto-clipper-<slug> -b feat/<slug> origin/main
  ```

- A task that depends on unfinished work branches from that work's branch instead of `main`
  (for example `feat/mix-presets` from `feat/audio-modes`). Its pull request targets the parent branch,
  or waits and is rebased onto `main` once the parent has merged.
- Two tasks that change the same files are not run in parallel; the second starts after the first merges.

## Commits

- Small and focused. The first line is an imperative summary of at most 72 characters
  ("Add Demucs background separation"), then a blank line and a body explaining what and why.
- Never commit secrets or local data: `.env`, `data/` (jobs, tokens, `data/secrets/`) and `.venv/` are ignored; keep them that way.
- Agent commits end with the `Co-Authored-By` trailer for the model that wrote them.

## Before opening a pull request

1. Bring the branch up to date: `git fetch origin && git rebase origin/main` (or merge `origin/main` if the branch is already shared), and resolve conflicts on the branch.
2. Run the full test suite and make sure it passes:

   ```bash
   mkdir -p ~/.cache/ac-tests
   TMPDIR=~/.cache/ac-tests uv run pytest -q -p no:cacheprovider --basetemp ~/.cache/ac-tests/bt
   ```

3. For anything that changes video or audio output, do one real render and look at frames and loudness.

4. CI (`.github/workflows/ci.yml`) runs the same suite on Linux and Windows for the pull request; both checks must be green before merging.

## CI and branch protection for `main`

CI runs on every pull request and every push to `main`: jobs `tests (ubuntu-latest)` and `tests (windows-latest)`, read-only token, no secrets. A newer push to a pull request cancels its superseded run; runs on `main` are never cancelled. See README "Continuous integration" for the exact commands.

Branch protection is turned on by the repository owner (agents never change repository settings). On GitHub, in `arponsd/clipper`:

1. Settings -> Rules -> Rulesets -> New ruleset -> New branch ruleset (or the classic Settings -> Branches -> Add branch protection rule).
2. Name it `main`, Enforcement status **Active**, Target branches -> Add target -> Include default branch (or the pattern `main`).
3. Tick **Restrict deletions** and **Block force pushes**.
4. Tick **Require a pull request before merging** (this also stops direct pushes to `main`). Approvals can stay at 0 for a single-person repo.
5. Tick **Require status checks to pass**, optionally "Require branches to be up to date before merging", then Add checks: `tests (ubuntu-latest)` and `tests (windows-latest)`. They appear in the search once the workflow has run at least once (open any pull request first). Require `tests (windows-latest)` only after it has been green once (the Windows job had never run when CI was added), otherwise a Windows-only failure would block the first pull request; until then require `tests (ubuntu-latest)` alone.
6. Leave the bypass list empty (or add only yourself, if you want an emergency override), and save.

Classic branch rules: the same with "Require a pull request before merging", "Require status checks to pass before merging" (the two checks above) and "Do not allow bypassing the above settings".

## Pull requests

- Push the branch and open a pull request against `main` (`gh pr create --base main`).
- The description says what changed, how it was tested (test count, real renders) and any risk or follow-up.
- Review: the reviewer agents (or a person) review the diff; findings are fixed on the same branch.
- Merge: the team lead merges with a merge commit (`gh pr merge --merge --delete-branch`) once tests pass,
  then removes the worktree (`git worktree remove ../auto-clipper-<slug>`).
- Never force-push `main`. Force-pushing your own unshared branch after a rebase is fine (`--force-with-lease`).

## After a merge

- Everyone working on another branch rebases onto the new `main` before their own pull request.
- The running app on `127.0.0.1:8765` is restarted from `main` only, after the merge.
