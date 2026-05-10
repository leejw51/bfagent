# GitHub Action `.app` Bundle Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `.github/workflows/release.yml` so that pushing a `v*` tag produces a notarized, stapled `bfagent.app` bundle inside a signed DMG, matching the local `make app` flow.

**Architecture:** Single macOS job builds via `make app`, codesigns the bundle deepest-first, notarizes+staples the `.app`, then drops the stapled bundle into a signed DMG. The `release` job (Ubuntu) is unchanged — it consumes the same artifact name.

**Tech Stack:** GitHub Actions (`macos-latest`), Apple `codesign`, `xcrun notarytool`, `xcrun stapler`, `hdiutil`, `ditto`. No new dependencies.

**Reference spec:** `docs/superpowers/specs/2026-05-11-github-action-app-bundle-release-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/Makefile` | Commit existing uncommitted change | Provides `make app` target the workflow depends on |
| `.gitignore` | Modify | Ignore the local `app/bfagent.app/` build artifact |
| `.github/workflows/release.yml` | Modify | Swap loose-binary signing/DMG flow for `.app` bundle flow |

There is no application source code change. All edits are to build/CI configuration. There are no automated tests for the workflow itself; verification is by inspection of YAML structure plus a real tag-triggered run after merge.

---

## Task 1: Commit the existing `make app` Makefile change

The workflow will call `make app`, which only exists in the working tree. Commit it first so the workflow has something to run against once `release.yml` is updated.

**Files:**
- Modify (already in working tree, just needs commit): `app/Makefile`

- [ ] **Step 1: Confirm the diff is exactly the `make app` target addition**

Run: `git diff app/Makefile`

Expected: a diff that adds `APP_BUNDLE`, `APP_BUNDLE_ID`, an `app:` target depending on `package`, an `INFO_PLIST_BODY` heredoc, the help-text line, the `.PHONY` entry for `app`, and adds `$(APP_BUNDLE)` to the `clean` target. No other changes.

If the diff contains anything else, stop and ask.

- [ ] **Step 2: Stage and commit just the Makefile**

```bash
git add app/Makefile
git commit -m "$(cat <<'EOF'
Add make app target that wraps binaries in bfagent.app bundle

The release workflow needs a single notarizable unit; a Mach-O can't
carry a stapled ticket but a .app bundle can.

EOF
)"
```

- [ ] **Step 3: Verify the commit landed and tree is clean of `app/Makefile`**

Run: `git status app/Makefile && git log -1 --stat app/Makefile`

Expected: `app/Makefile` no longer appears in `git status`. Most recent commit touching it is the one just created.

---

## Task 2: Ignore the local `bfagent.app/` build artifact

The local `make app` run already produced `app/bfagent.app/`, which appears as untracked in `git status`. Add it to `.gitignore` so it doesn't get committed accidentally.

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Append the bundle ignore pattern**

Edit `.gitignore`. Find the existing block that already ignores compiled binaries:

```
# Compiled binaries
app/bfagent
app/bfagent-app
app/bfagent-cli
app/bin/
```

Replace it with:

```
# Compiled binaries
app/bfagent
app/bfagent-app
app/bfagent-cli
app/bin/
app/bfagent.app/
```

- [ ] **Step 2: Verify the bundle no longer shows as untracked**

Run: `git status`

Expected: `app/bfagent.app/` is gone from the "Untracked files" section. `.gitignore` shows as modified.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "$(cat <<'EOF'
Ignore local bfagent.app build artifact

The bundle is reproduced by make app on every release build; no need
to track it in the repo.

EOF
)"
```

---

## Task 3: Update the `Build binaries` step to build the bundle

The first surgical change to `release.yml`: have CI assemble the `.app` bundle instead of stopping at three loose binaries.

**Files:**
- Modify: `.github/workflows/release.yml:31-33`

- [ ] **Step 1: Edit the build step**

Find this block in `.github/workflows/release.yml`:

```yaml
      # ── Build all three binaries via the app Makefile ─────────────────
      - name: Build binaries
        working-directory: app
        run: make package
```

Replace with:

```yaml
      # ── Build the bfagent.app bundle (also produces the three binaries) ─
      - name: Build app bundle
        working-directory: app
        run: make app
```

- [ ] **Step 2: Verify the bundle path the rest of the workflow will reference**

Run: `grep -n "bfagent.app" .github/workflows/release.yml || true`

Expected: only the new step references `app` so far (no existing `bfagent.app` references in the file). Subsequent tasks will add references at fixed paths under `app/bfagent.app/`.

- [ ] **Step 3: No commit yet** — keep all `release.yml` edits in a single commit at the end of Task 6 so the file is never in a half-converted state on `main`.

---

## Task 4: Replace the codesign step with bundle-aware signing

Loose-binary signing must become deepest-first bundle signing.

**Files:**
- Modify: `.github/workflows/release.yml:121-148` (the `Codesign binaries` step)

- [ ] **Step 1: Replace the entire `Codesign binaries` step**

Find this block:

```yaml
      # ── Sign all three binaries ──────────────────────────────────────
      - name: Codesign binaries
        run: |
          BFAGENT="app/bin/bfagent"
          BFAGENT_CLI="app/bin/bfagent-cli"
          BFAGENT_APP="app/bin/bfagent-app"

          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp \
            "$BFAGENT"

          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp \
            "$BFAGENT_CLI"

          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --entitlements entitlements.plist \
            --timestamp \
            "$BFAGENT_APP"

          echo "Signed bfagent:"
          codesign -dv --verbose=2 "$BFAGENT" 2>&1 | head -5
          echo "Signed bfagent-cli:"
          codesign -dv --verbose=2 "$BFAGENT_CLI" 2>&1 | head -5
          echo "Signed bfagent-app:"
          codesign -dv --verbose=2 "$BFAGENT_APP" 2>&1 | head -5
```

Replace with:

```yaml
      # ── Codesign the .app bundle (deepest-first) ─────────────────────
      # codesign requires nested Mach-Os to be signed before the
      # enclosing bundle, otherwise the outer signature won't seal.
      # The bundle's main executable (bfagent-app, per CFBundleExecutable)
      # gets the JIT entitlements when we sign the bundle as a whole.
      - name: Codesign app bundle
        run: |
          set -e
          APP="app/bfagent.app"

          echo "[codesign] signing nested CLI launchers (no entitlements)"
          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp \
            "$APP/Contents/MacOS/bfagent"

          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp \
            "$APP/Contents/MacOS/bfagent-cli"

          echo "[codesign] signing bundle (entitlements apply to main executable)"
          codesign --force --options runtime \
            --sign "$APPLE_SIGNING_IDENTITY" \
            --entitlements entitlements.plist \
            --timestamp \
            "$APP"

          echo "[codesign] verifying bundle"
          codesign --verify --deep --strict --verbose=2 "$APP"
          codesign -dv --verbose=2 "$APP" 2>&1 | head -10
```

- [ ] **Step 2: Sanity-check the YAML indentation matches surrounding steps**

Run: `grep -nE '^      - name:' .github/workflows/release.yml`

Expected: every job step starts with exactly `      - name:` (six leading spaces). The new `Codesign app bundle` step must align with the others.

- [ ] **Step 3: No commit yet.**

---

## Task 5: Replace the `Build and notarize DMG` step

Notarize+staple the `.app`, then drop the stapled bundle into a signed DMG (no DMG notarization).

**Files:**
- Modify: `.github/workflows/release.yml:154-186` (the `Build and notarize DMG` step)

- [ ] **Step 1: Replace the entire step**

Find this block:

```yaml
      # ── Build + sign + notarize + staple DMG ─────────────────────────
      # DMGs (unlike raw Mach-O) accept a stapled notary ticket, so
      # Gatekeeper can verify offline and Finder won't show the
      # "Apple cannot check..." dialog on first launch.
      - name: Build and notarize DMG
        env:
          APPLE_ID: ${{ secrets.APPLE_ID }}
          APPLE_APP_SPECIFIC_PASSWORD: ${{ secrets.APPLE_APP_SPECIFIC_PASSWORD }}
          TEAM_ID: ${{ secrets.TEAM_ID }}
        run: |
          mkdir -p dist/dmg-staging
          cp app/bin/bfagent       dist/dmg-staging/bfagent
          cp app/bin/bfagent-cli   dist/dmg-staging/bfagent-cli
          cp app/bin/bfagent-app   dist/dmg-staging/bfagent-app
          chmod +x dist/dmg-staging/bfagent dist/dmg-staging/bfagent-cli dist/dmg-staging/bfagent-app

          DMG="dist/bfagent-macos-arm64.dmg"
          hdiutil create -volname "bfagent" \
            -srcfolder dist/dmg-staging \
            -ov -format UDZO \
            "$DMG"

          codesign --force --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp "$DMG"

          echo "Submitting DMG for notarization..."
          xcrun notarytool submit "$DMG" \
            --apple-id "$APPLE_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --team-id "$TEAM_ID" \
            --wait --timeout 20m

          echo "Stapling DMG..."
          xcrun stapler staple "$DMG"
          xcrun stapler validate "$DMG"

          rm -rf dist/dmg-staging
```

Replace with:

```yaml
      # ── Notarize + staple the .app, then drop into signed DMG ────────
      # The .app carries the stapled ticket; once stapled, the bundle
      # validates with Gatekeeper offline even after extraction from
      # the DMG. The DMG itself only needs to be signed, not notarized.
      - name: Notarize app and build DMG
        env:
          APPLE_ID: ${{ secrets.APPLE_ID }}
          APPLE_APP_SPECIFIC_PASSWORD: ${{ secrets.APPLE_APP_SPECIFIC_PASSWORD }}
          TEAM_ID: ${{ secrets.TEAM_ID }}
        run: |
          set -e
          APP="app/bfagent.app"
          ZIP="app/bfagent.app.zip"

          echo "[notarize] zipping bundle for submission"
          ditto -c -k --keepParent "$APP" "$ZIP"

          echo "[notarize] submitting to Apple (this can take several minutes)"
          xcrun notarytool submit "$ZIP" \
            --apple-id "$APPLE_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --team-id "$TEAM_ID" \
            --wait --timeout 20m

          echo "[notarize] stapling ticket to bundle"
          xcrun stapler staple "$APP"
          xcrun stapler validate "$APP"

          echo "[dmg] staging stapled bundle"
          mkdir -p dist/dmg-staging
          # ditto preserves the xattrs that hold the stapled ticket
          ditto "$APP" "dist/dmg-staging/bfagent.app"

          DMG="dist/bfagent-macos-arm64.dmg"
          echo "[dmg] building $DMG"
          hdiutil create -volname "bfagent" \
            -srcfolder dist/dmg-staging \
            -ov -format UDZO \
            "$DMG"

          echo "[dmg] signing DMG"
          codesign --force --sign "$APPLE_SIGNING_IDENTITY" \
            --timestamp "$DMG"

          rm -rf dist/dmg-staging "$ZIP"
```

- [ ] **Step 2: Verify the artifact name and upload step still match**

Run: `grep -n "bfagent-macos-arm64.dmg" .github/workflows/release.yml`

Expected: two references — one in the new step (the `DMG=` line) and one in the `Upload release artifact` step (`path: dist/bfagent-macos-arm64.dmg`). The Upload step needs no change.

- [ ] **Step 3: No commit yet.**

---

## Task 6: Local YAML validation, then commit `release.yml`

The three workflow edits land together as a single commit so `main` is never in a half-converted state.

**Files:**
- Modify: `.github/workflows/release.yml` (already edited in Tasks 3–5)

- [ ] **Step 1: Validate YAML syntax locally**

Run:

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))" && echo OK
```

Expected: `OK`. Any YAML parse error must be fixed before committing.

- [ ] **Step 2: Eyeball the diff one more time**

Run: `git diff .github/workflows/release.yml`

Expected diff hunks:
1. `Build binaries` → `Build app bundle` running `make app`.
2. `Codesign binaries` → `Codesign app bundle` (deepest-first signing of `app/bfagent.app`).
3. `Build and notarize DMG` → `Notarize app and build DMG` (notarize+staple `.app`, then `ditto` into staging, `hdiutil`, sign DMG, no DMG notarization).

No other steps change. Cert import, entitlements creation, upload, keychain cleanup, and the `release` job are untouched.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "$(cat <<'EOF'
Release bfagent.app bundle as notarized DMG in CI

Build the .app bundle via make app, codesign deepest-first, notarize
and staple the bundle, then ship it inside a signed DMG. A stapled
.app survives extraction from the DMG and validates with Gatekeeper
offline, so the DMG only needs signing — not a second notary roundtrip.

EOF
)"
```

---

## Task 7: Verify with a real tagged release

Workflow correctness can only be confirmed end-to-end against Apple's signing/notary services. This task is the validation gate.

- [ ] **Step 1: Push the prep commits to `main`**

Run: `git push origin main`

Expected: push succeeds. The workflow does not run on push to `main` — only on `v*` tags — so this is safe.

- [ ] **Step 2: Cut a release tag**

Pick the next version (the Makefile currently declares `VERSION := 0.3.0`; coordinate the tag with the user if a bump is needed). Then:

```bash
git tag -a v0.3.0 -m "Release v0.3.0"
git push origin v0.3.0
```

If the user wants a different version or a `-rc` tag for safety (e.g. `v0.3.0-rc1`), use that instead.

- [ ] **Step 3: Watch the run**

Run: `gh run watch --exit-status`

Or open the Actions tab on GitHub. Expected: the `build` job passes through every step. Pay particular attention to:
- `Build app bundle` produces `app/bfagent.app/`
- `Codesign app bundle` ends with `--verify --deep --strict` printing `valid on disk` and `satisfies its Designated Requirement`
- `Notarize app and build DMG` shows `notarytool` returning `status: Accepted` and `stapler validate` printing `The validate action worked!`
- The `release` job uploads `bfagent-macos-arm64.dmg` and `checksums.txt` to a new GitHub Release.

- [ ] **Step 4: Smoke-test the released DMG**

Download the DMG from the GitHub Release on a Mac that has never seen these binaries (or after `xattr -dr com.apple.quarantine` is *not* run — we want the quarantine bit so we're testing the real Gatekeeper flow). Then:

```bash
hdiutil attach ~/Downloads/bfagent-macos-arm64.dmg
cp -R "/Volumes/bfagent/bfagent.app" /Applications/
hdiutil detach "/Volumes/bfagent"
spctl --assess --type execute --verbose /Applications/bfagent.app
```

Expected from `spctl`: `accepted` and `source=Notarized Developer ID`. Then double-click `bfagent.app` from `/Applications/`. The Tauri window should open without a Gatekeeper warning.

- [ ] **Step 5: If anything fails**

Do not "fix forward" by re-tagging blindly. Read the failing step's logs, diagnose, edit `release.yml` (or the Makefile if the bundle layout is wrong), commit to `main`, then push a fresh tag (`v0.3.1`, `v0.3.0-rc2`, etc.). Apple notary submissions are billed per submission and can take minutes — get the diagnosis right before re-running.

---

## Out of scope

- Universal binary (x86_64 + arm64) builds. Current target is `aarch64-apple-darwin` only; adding x86_64 is a separate plan.
- Sparkle/auto-update integration.
- Code-signing certificate rotation tooling.
- Changing the GitHub Release notes generation (`generate_release_notes: true` stays).
