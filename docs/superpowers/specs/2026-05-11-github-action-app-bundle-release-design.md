# GitHub Action: release `bfagent.app` bundle as notarized DMG

Date: 2026-05-11
Status: Approved

## Goal

Update `.github/workflows/release.yml` so that pushing a `v*` tag produces
a notarized, stapled `bfagent.app` bundle delivered inside a signed DMG —
matching the local `make app` flow that is now working.

The current workflow signs three loose Mach-O binaries (`bfagent`,
`bfagent-cli`, `bfagent-app`) and ships them flat inside the DMG. That
release artifact is being retired in favor of a single `.app` bundle that
carries a stapled notary ticket.

## Non-goals

- No change to the tag trigger pattern (`v*`).
- No change to target architecture (`aarch64-apple-darwin` only).
- No change to the certificate-import logic, which was debugged across
  recent commits and is known to work.
- No change to the `release` job (Ubuntu, checksums, GitHub Release).

## Workflow shape

Single macOS job (`build`) running on `macos-latest`:

1. **Checkout** + Python 3.11 + Rust toolchain with
   `aarch64-apple-darwin` target. (Unchanged.)
2. **Build the bundle:** `cd app && make app`. This invokes `make
   package` and then assembles `app/bfagent.app/` per the new Makefile
   target.
3. **Import codesigning certificate:** the existing step is preserved
   verbatim. It already handles password trimming, openssl diagnostics,
   and the keychain partition list.
4. **Create entitlements.plist:** preserved as-is — JIT,
   allow-unsigned-executable-memory, disable-library-validation. These
   are needed by the wry/WKWebView binary.
5. **Codesign the bundle, deepest-first.** `codesign` requires nested
   Mach-Os to be signed before the enclosing bundle:
   - `app/bfagent.app/Contents/MacOS/bfagent` — hardened runtime, no
     entitlements.
   - `app/bfagent.app/Contents/MacOS/bfagent-cli` — hardened runtime,
     no entitlements.
   - The bundle itself: `codesign --force --options runtime
     --entitlements entitlements.plist --timestamp --sign "$ID"
     app/bfagent.app`. This signs the main executable
     (`bfagent-app`, per `CFBundleExecutable`) with the JIT
     entitlements.
   - Verify: `codesign --verify --deep --strict app/bfagent.app` and
     `codesign -dv --verbose=2 app/bfagent.app`.
6. **Notarize the `.app`:**
   - `ditto -c -k --keepParent app/bfagent.app app/bfagent.app.zip`
     (notarytool requires an archive).
   - `xcrun notarytool submit app/bfagent.app.zip --apple-id ...
     --password ... --team-id ... --wait --timeout 20m`.
   - `xcrun stapler staple app/bfagent.app`.
   - `xcrun stapler validate app/bfagent.app`.
7. **Build the DMG containing the stapled `.app`:**
   - Stage `dist/dmg-staging/bfagent.app` (copied from the stapled
     bundle; the stapled ticket lives inside the bundle, so a copy
     preserves it).
   - `hdiutil create -volname bfagent -srcfolder dist/dmg-staging -ov
     -format UDZO dist/bfagent-macos-arm64.dmg`.
   - `codesign --force --sign "$ID" --timestamp
     dist/bfagent-macos-arm64.dmg`.
   - No DMG notarization. The `.app` inside is already stapled, which
     satisfies Gatekeeper offline; skipping the second notary roundtrip
     keeps CI faster.
8. **Upload artifact:** `bfagent-macos-arm64.dmg` (name unchanged so
   the downstream `release` job continues to work).
9. **Cleanup keychain:** preserved as-is, runs on `if: always()`.

## Repository housekeeping

- The uncommitted `app/Makefile` change that introduces the `app:`
  target must be committed before the new workflow can run; CI will
  fail at step 2 otherwise.
- Add `app/bfagent.app/` to `.gitignore`. It is a build artifact and
  is currently untracked in the working tree.

## Rationale for chosen options

- **DMG contains only `bfagent.app`:** simplest install UX (drag to
  `/Applications`). CLI binaries remain reachable inside the bundle at
  `bfagent.app/Contents/MacOS/bfagent-cli`.
- **Notarize and staple the `.app`, sign-only the DMG:** a stapled
  `.app` survives extraction from the DMG and validates with Gatekeeper
  offline on any machine. Stapling the DMG too would add a second
  notary submission for no additional UX benefit.
- **Replace `release.yml` in place:** the loose-binary flow is no
  longer the artifact we ship. Keeping it as a parallel job would just
  burn CI minutes.

## Risks

- **Notarization can take minutes.** The `--wait --timeout 20m` keeps
  the job blocking but bounded. Most submissions complete in well under
  five minutes; the timeout is defensive.
- **`hardened runtime` failures surface only at notarization or
  runtime.** The `codesign --verify --deep --strict` check after
  signing catches most issues earlier.
- **Stapler ticket preservation when copying into the DMG staging
  directory.** The ticket lives inside `Contents/CodeResources` as
  extended attributes; macOS's `ditto` preserves xattrs reliably. The
  staging copy will use `ditto app/bfagent.app dist/dmg-staging/bfagent.app`
  rather than `cp -R` to avoid any tooling differences across runner
  images.
