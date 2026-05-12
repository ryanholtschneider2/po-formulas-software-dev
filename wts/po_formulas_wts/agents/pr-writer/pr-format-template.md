# PR Format Template

Generates a PR body. Return ONLY the content in the template below (do not
include the `===== PR Template =====` fence lines). Format as GitHub-flavored
markdown. Assume the reader knows the existing codebase but not what was
added in this PR. Keep prose tight — section bodies should be 1-4 sentences
unless the section explicitly calls for evidence dumps.

## Section ordering and burden of proof

**The author runs the smoke checks. The reviewer reads the evidence.** The
goal of a PR body is to convince a busy reviewer the change works without
making them re-derive everything from the diff. If a reviewer would still
need to bootstrap a cluster or hunt for screenshots to be confident, the
PR body isn't done.

Required section order (Ryan's preference, last updated 2026-05-12):

1. **Summary** — 1-3 sentences. What problem this solves, for whom.
2. **How to verify** — reviewer-runnable steps. Paste-and-run commands or
   exact navigation paths.
3. **Test plan** — `- [ ]` checkboxes for what the author committed to
   verifying before merge.
4. **Test results & evidence** — what the author actually saw. Boxes
   from Test Plan checked off (`- [x]`), screenshots, smoke-script
   output, demo video links, pytest summary lines, JWT-claim dumps.
   **This is the author's burden of proof, not the reviewer's.** Empty
   Test Results means the PR isn't ready. Unchecked boxes must be
   labeled `- [ ] deferred — see #NNN` or `- [ ] N/A because <reason>`.
5. **Changes / new features** — bulleted by component or area. What
   actually moved on disk.
6. **Future work** _(optional)_ — concrete follow-ups with beads /
   issue links. Omit if none.
7. **Discussion** _(optional)_ — architecture overview, data flow, new
   patterns. Use only if the change is large enough that the reviewer
   needs context the diff doesn't provide.

Sections 1-5 are required. Sections 6-7 are conditional.

## Required gates before opening / marking PR ready

Every PR — regardless of size — must clear these BEFORE the PR is opened
(or moved out of draft). Evidence for each goes inline under **Test
results & evidence**. No exceptions; defer with `- [ ] N/A because …`
only when the gate genuinely doesn't apply (e.g. demo-video on a pure
backend change).

| Gate | What it is | Evidence to inline |
|---|---|---|
| **Local lint** | `make lint` (or repo's lint-all target) on the changed scope. Fix locally — don't farm to remote CI. | Last 10–20 lines of output: `ruff … 0 errors`, `tsc … no diagnostics`. |
| **Local tests** | `make test-unit` (+ `make test-e2e` if scope warrants) locally. | Pytest summary line: `=== 124 passed in 32.1s ===`. |
| **Local real-env smoke** | Run the repo's smoke walkthrough against the actual local env (minikube + ingress + auth-service, not mocks). Verifies the change holds against real network / DB / cluster wiring — not just isolated unit assertions. | Verdict from the smoke harness's `report.md` (`Verdict: PASS`) plus the artifact-bundle path. |
| **Demo video** (UI changes) | Generated via the `/create-demo-video` skill from the smoke walkthrough screenshots + narration. Mandatory for any user-facing UI change; skippable on backend-only PRs. | Link / drag-drop of the `demo_final.mp4` produced by the skill (lands under `research/<branch-or-pr>/demo-<utc>/`). |
| **Remote CI** | GitHub Actions green on the branch's latest SHA. **Local gates first; CI is the safety net, not the iteration loop.** | PR check status (`gh pr checks <N>` all green) or a screenshot of the Actions tab. |

The order matters: local lint → local tests → local smoke → demo video
→ push → remote CI. Catching a failure locally costs seconds; catching
it after push costs a CI cycle and pollutes the PR's check history.

**Smoke-walkthrough artifacts** land under
`research/<branch-or-pr-slug>/manual-smoke-<utc>/` (the harness's default
since the `--slug` / `--git-common-dir` refactor on 2026-05-12). Demo
videos sit alongside as `research/<branch-or-pr-slug>/demo-<utc>/`.
Both are reviewer-clickable artifacts — link them from the PR body,
don't make the reviewer rerun the harness.

**Demo-video → narration source-of-truth**: the `/create-demo-video`
skill reuses the smoke walkthrough's screenshots as the visual base
and narrates each scene against the PR's Test Plan checkboxes. This
means the smoke harness, the demo video, and the PR body are all
keyed to the same checklist — one source of truth. If you change a
Test Plan item, regenerate the demo.

## On demo videos and burden of proof

Demo videos and screenshots are author-supplied evidence, not
reviewer-required setup. If a feature has a UI, a 30-second screen
capture (or a single annotated screenshot) goes in Test Results &
Evidence. Reviewers should not have to spin up minikube to confirm "the
button now exists." Same rule for backend changes: paste the curl
output, the pytest summary line, the kubectl-logs grep result — whatever
shows the assertion was actually verified.

If evidence can't be produced, say so explicitly in Test Results:
"verified in-cluster against minikube only; staging verification
deferred." The reviewer's job is spot-checks and follow-up questions —
not re-running the smoke suite from scratch.

## Embedding images and video inline (so the reviewer doesn't download)

The goal is **inline-rendered evidence in the PR body or a PR comment**
— not an Actions-artifact zip the reviewer has to download. We do this
via a per-PR GitHub Release that holds the assets, with markdown
linking to the asset URLs. **For private repos, this requires the
reviewer to be logged in with repo read access** — which they always
are when reviewing — so the inline `<img>` / `<video>` tags resolve
through their session cookie. Anonymous / curl / Bearer-token fetches
of the same URL will 404, which is fine.

**Use the helper script** at `scripts/pr-walkthrough/embed.sh` (lives
in any repo that uses this convention). It handles release creation,
upload, and PR comment formatting in one shot:

```bash
# Capture screenshots / video into /tmp/<something>/ — Playwright,
# manual screenshots, ffmpeg, ImageMagick, whatever.

# Then upload + post to the current branch's PR:
./scripts/pr-walkthrough/embed.sh \
  --title "Login redirect + org-scoped view" \
  --comment \
  /tmp/walkthrough/login.png \
  /tmp/walkthrough/dashboard.png \
  /tmp/walkthrough/flow.webm

# Other modes:
./scripts/pr-walkthrough/embed.sh foo.png            # prints markdown snippet to stdout
./scripts/pr-walkthrough/embed.sh --append foo.png   # appends to PR body
./scripts/pr-walkthrough/embed.sh --pr 42 foo.png    # explicit PR number
```

The script creates a **prerelease** tagged `pr-walkthrough-<pr-number>`
to hold the files. Why prerelease (not draft): draft-release asset
URLs use a synthetic `untagged-<hash>` slug — predictable URLs are
only available once the release is published. Prereleases are
de-emphasized in the Releases tab but still browseable to
collaborators.

**Supported formats:** `.png`, `.jpg`, `.gif`, `.webp`, `.svg`
(images); `.mp4`, `.mov`, `.webm` (video). Per-asset limit ~10MB —
re-encode if larger (`ffmpeg -i in.webm -crf 32 -preset slow out.mp4`).
Videos render with `<video controls playsinline>`, no autoplay.

**Capture conventions** (so the author doesn't reinvent each time):

- UI walkthroughs: Playwright trace mode (`page.video({ dir: ... })`)
  produces a `.webm`. Pass it to `embed.sh` and it renders as a
  `<video>` element in the PR.
- Frame stills: `mcp__playwright__playwright_screenshot` or
  `page.screenshot({ path, fullPage: true })`. PNG.
- Backend-only PRs: skip the walkthrough video; smoke output (plain
  text in a fenced code block) is the evidence — `embed.sh` is only
  for binary assets.

**Don't use GitHub Actions `artifact` uploads as the evidence
surface** — those are download-zips. Fine as a backup, never the
primary handoff.

**Don't commit screenshots to the repo** — they pollute git history
and bloat the PR diff. The release-store keeps them out of the
working tree and lets us garbage-collect on PR close (see
`scripts/pr-walkthrough/cleanup.sh` — TODO).

**Drag-drop into the PR body** also works and uses GitHub's
`user-attachments` CDN, which doesn't even require auth to view. That
path is manual but has the broadest reader-side compatibility. Use it
when the reviewer might not be logged in (rare for internal repos).

===== PR Template =====

# **Title:** <imperative summary>

## Summary

_1-3 sentences. What problem this solves, for whom. Skip "this PR…"
phrasing — go straight to the substantive change._

## How to verify

_Reviewer-runnable steps. Prefer paste-and-run commands. Reference any
helper scripts (`./scripts/...`) the PR ships._

```bash
# Example
./scripts/smoke-foo/setup.sh
./scripts/smoke-foo/check.sh
```

Expected: _what the reviewer should see on success, in one line._

## Test plan

- [ ] _Specific assertion the author committed to verifying before merge._
- [ ] _Next assertion._
- [ ] _Edge case or regression check._
- [ ] **Local lint**: `make lint` (or scoped equivalent) clean.
- [ ] **Local tests**: `make test-unit` (+ `make test-e2e` where scope warrants) green.
- [ ] **Local real-env smoke**: walkthrough harness PASS against minikube (`research/<branch-slug>/manual-smoke-<utc>/report.md`).
- [ ] **Demo video** _(UI only)_: generated via `/create-demo-video` from the smoke screenshots — `research/<branch-slug>/demo-<utc>/demo_final.mp4`.
- [ ] **Remote CI**: `gh pr checks` all green on latest SHA.

## Test results & evidence

_What the author actually verified. Check off each box from the Test
Plan with `- [x]` and attach evidence inline._

- [x] _Assertion_ → `<paste of relevant output, screenshot link, or
  `pytest -v` summary line>`
- [x] _Next assertion_ → _evidence_
- [ ] _Deferred check_ → see #NNN for follow-up

Screenshots / demo _(if UI; drag-drop into the PR body so they render inline — see "Embedding images and video" above)_:

<details><summary>Walkthrough</summary>

_<paste image(s) and/or video here via drag-drop; GitHub hosts on
user-attachments CDN and renders inline. Use a `<details>` block so
the PR body stays scannable when there are 2+ shots.>_

</details>

## Changes

- **Component/Area Name**: _Brief description of what changed_
- **Component/Area Name**: _Brief description of what changed_
- **Component/Area Name**: _Brief description of what changed_

## Future work _(optional — omit if none)_

- _Concrete follow-up, with beads ID or issue link_

## Discussion _(optional — omit unless the change is architectural)_

_Professional but accessible explanation of how the new pieces fit. Use
when the reviewer needs context on patterns or data flow that aren't
obvious from the diff alone._

===== END PR TEMPLATE =====

## Instructions

Base the body on the diff between the branch and the user-provided base
branch (or `origin/HEAD` by default).

**CRITICAL: Analyze BOTH modified AND newly added files**

```bash
# 1. Default branch
git symbolic-ref --short refs/remotes/origin/HEAD

# 2. ALL changed files (M, A, D)
git diff --name-status <base-branch>

# 3. Full diff
git diff <base-branch>

# 4. For files marked 'A' (Added), use Read on the whole file —
#    diffs of new files don't show the full picture.
```

### Process

1. Examine `git diff --name-status` and categorize:
   - `M` modified → analyze hunks
   - `A` added → Read the whole file
   - `D` deleted → note what's gone
2. Read every added file in full.
3. Group hunks by component/area for the Changes section.
4. Hunt for non-git context (recent <14d):
   ```bash
   find . -maxdepth 3 -name "*.md" -mtime -14 \
     -not -path "*/node_modules/*" -not -path "*/.venv/*"
   find . -name "*plan*" -mtime -14 | head -10
   find . -name "*lint-results*" -o -name "*summary*" -mtime -14 | head -10
   ```
   Read any plan / lint-result / summary files — they often have the
   "why" for the diff.
5. If the user provides an issue number, `gh issue view N` for context.

### Filling each section

- **Summary**: lead with the problem, not the diff. "X breaks under Y;
  this fixes it by Z." Not "This PR adds…".
- **How to verify**: paste-and-run commands. If the PR ships a smoke
  script, name it. For browser navigation, give the exact path:
  `Login → /maps → search 'CO'`.
- **Test plan**: checkboxes the AUTHOR committed to. Don't promise more
  than was actually tested.
- **Test results & evidence**: load-bearing section. Every Test Plan box
  gets a result. Inline the proof — pytest line, curl status,
  screenshot, JWT-claim dump. No evidence = not done.
- **Changes**: one bullet per component. Reference paths.
- **Future work**: only concrete follow-ups, with beads / issue links.
  Don't list aspirational ideas.
- **Discussion**: skip unless the change introduces new architecture.

### Save the body

After generating, save to `{feature_name}_pr.md` derived from the branch
name or primary feature:

```
rh/thinking_messages → thinking_messages_pr.md
feature/file-viewer  → file_viewer_pr.md
```

This lets Ryan edit / iterate / re-use the body without re-running the
generator.

### Examples

**Good Summary:**
> Polymer auth-service hardcoded `org_id=default_org` for every Keycloak
> user, collapsing all tenants into one. This reads the `org_id` JWT
> claim (with chart-side protocol-mapper backing) so per-user org
> attributes drive tenant scoping.

**Bad Summary:**
> This PR adds multi-tenancy support to auth-service.

**Good How to verify:**
> ```bash
> ./scripts/smoke-move-ub/keycloak-up.sh
> ./scripts/smoke-move-ub/check.sh
> ```
> Expected: 10/10 pytest cases green, including
> `test_keycloak_direct_grant_issues_token_for_smoke_user`.

**Bad How to verify:**
> Run the tests.

**Good Test results & evidence:**
> - [x] JWT carries `org_id` claim →
>   ```
>   org_id: org_01KBJGW0EKAR3Q2R8ZYTK6MQ6X
>   email : ryan@jataware.com
>   ```
> - [x] handle_callback resolves from claim, not default →
>   `PASS — claim took precedence over sentinel default` (in-pod
>   programmatic verifier with `AUTH_SERVICE_KEYCLOAK_DEFAULT_ORG_ID=sentinel_default_must_not_be_used`)
> - [x] Smoke harness end-to-end →
>   ```
>   ==== ALL CHECKS PASSED ====
>   10 passed in 10.84s
>   ```

**Bad Test results & evidence:**
> Tested locally, works.

## Tone

Professional, neutral, descriptive. Avoid "This PR adds…" / "This PR
implements…" — go straight to the substantive thing. Don't editorialize
about quality. Don't claim "production-ready" or "fully tested" — show
the evidence and let the reviewer judge.

**No Claude attribution** in PR titles, bodies, or anywhere else — strip
the default Claude Code "🤖 Generated with..." footers. Same rule as
commit attribution in `~/.claude/CLAUDE.md`.
