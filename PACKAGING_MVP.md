# MVP Packaging Guide

## What This Delivers
- Local HTTP app server for the ClassIn EDB MVP
- Browser UI connected to the real export pipeline
- Double-click local launch for development
- User-facing Windows `.exe` and macOS `.app` packaging with PyInstaller
- Bundled React browser runtime and a prebuilt app bundle so the core UI can open without CDN access

## Main Entry Points
- Local app server: `app_server.py`
- Windows local launcher: `run_local_app.ps1`
- macOS local launcher: `run_local_app.command`
- Windows packaging script: `package_mvp.ps1`
- Windows installer script: `package_windows_installer.ps1`
- macOS packaging script: `package_macos_app.sh`

## Local Run
```powershell
cd C:\Projects\Class_project\edb_make
.\run_local_app.ps1 -InstallDeps
```

If dependencies are already installed:
```powershell
.\run_local_app.ps1
```

With `-InstallDeps`, the Windows launcher creates `.venv` when needed and installs
dependencies there instead of modifying the global Python environment. It prefers
the project virtual environment, then the Windows `py -3` launcher, then
`python.exe`, and requires Python 3.11 or newer. A custom interpreter can be
selected with `-PythonExe C:\path\to\python.exe`.
Unless the caller already configured `PYTHONUTF8`, the launcher enables Python
UTF-8 mode so Korean paths and diagnostics remain readable when output is
redirected to CI, an editor, or a support log.

Default app URL:
```text
http://127.0.0.1:8765/
```

## macOS Double-Click Run
For day-to-day local use, packaging is not required. Double-click:

```text
run_local_app.command
```

The launcher requires Python 3.11 or newer because the current code uses `enum.StrEnum`. It checks the existing `.venv` before use; if `.venv` is missing or was created with Python older than 3.11, it recreates `.venv` with a suitable `python3` interpreter, installs `requirements-local.txt` only when needed, starts `app_server.py`, and opens the browser at `http://127.0.0.1:8765/`.

If Python 3.11+ is not installed, the launcher shows a Korean error message and waits for Enter before closing.

If the local server is already running, it only opens the browser instead of starting a duplicate server.

## In-App Flow
1. Start the local app server
2. Open the browser UI
3. Click `Choose source`
4. Pick an image or PDF
5. Set `subject`, `OCR`, and output folder name
6. Click `Run export`
7. Review source/problem/board previews
8. Open the generated `.edb` from the inspector or header

## PyInstaller Packaging
Windows packages must be built on Windows. macOS `.app` bundles must be built on macOS.

### Windows `.exe`
Build the installable Windows setup file on Windows:
```powershell
.\package_windows_installer.ps1 -Clean -InstallPyInstaller
```

With in-app update metadata:
```powershell
.\package_windows_installer.ps1 -Clean -InstallPyInstaller `
  -Version 0.1.1 `
  -UpdateFeedUrl "https://example.com/classin-edb/update.json" `
  -DownloadUrl "https://example.com/ClassInEDBMVP-Setup.exe"
```

Expected installer:
```text
dist\ClassInEDBMVP-Setup.exe
```

This `ClassInEDBMVP-Setup.exe` is the canonical Windows download artifact for each release and is the only Windows file that needs to be delivered to installer users. Do not distribute `dist\ClassInEDBMVP\ClassInEDBMVP.exe` by itself, or a zip containing only that executable: the folder-style launcher depends on the adjacent `_internal` directory. The setup file is rebuilt for every release, so its version, size, checksum, and binary contents will change even when the download filename stays the same.

That installer creates a Start menu shortcut and can create a desktop shortcut. Clicking the installed app opens the browser at the local app.

For external Windows testing, sign both the packaged app binaries and the final installer with an Authenticode code-signing certificate:
```powershell
.\package_windows_installer.ps1 -Clean -InstallPyInstaller -Sign `
  -SignCertificatePath "C:\secure\ClassInEDB-CodeSigning.pfx" `
  -SignCertificatePassword $env:WINDOWS_CERT_PASSWORD
```

If the certificate is already installed in the Windows certificate store, you can sign by thumbprint, subject, or automatic selection:
```powershell
.\package_windows_installer.ps1 -Clean -InstallPyInstaller -Sign `
  -SignCertificateThumbprint "CERTIFICATE_THUMBPRINT"

.\package_windows_installer.ps1 -Clean -InstallPyInstaller -Sign `
  -SignCertificateSubject "Your Publisher Name"

.\package_windows_installer.ps1 -Clean -InstallPyInstaller -Sign `
  -SignCertificateAutoSelect
```

The script locates `signtool.exe` from `PATH` or the Windows SDK. It signs `.exe`, `.dll`, and `.pyd` files in the packaged app folder before building the installer, then signs and verifies `dist\ClassInEDBMVP-Setup.exe`.
The installer build prints the final setup file size and SHA-256 hash. The Inno Setup definition uses high-compression LZMA2 and closes the running app before replacing the complete PyInstaller `_internal` payload, so DLLs and Python extensions removed in a newer release cannot survive an in-place upgrade. User settings, API keys, uploads, and outputs remain untouched in the separate runtime folder under the Windows Documents known folder. If `-SignTool` or `-InnoSetupCompiler` is supplied explicitly, a missing or non-file path fails the build instead of silently selecting a different installed tool.

Installer display metadata can be overridden without changing the packaged executable name:
```powershell
.\package_windows_installer.ps1 -Clean -InstallPyInstaller `
  -AppDisplayName "ClassIn EDB" `
  -AppPublisher "ClassIn EDB"
```

If you only want the raw packaged app folder, install PyInstaller if needed:
```powershell
.\package_mvp.ps1 -InstallPyInstaller
```

Or package directly if PyInstaller is already installed:
```powershell
.\package_mvp.ps1
```

Useful options:
```powershell
# Create a single executable file instead of a directory
.\package_mvp.ps1 -OneFile

# Keep a console window for debugging
.\package_mvp.ps1 -Console

# Reuse an existing frontend bundle instead of running Node
.\package_mvp.ps1 -SkipFrontendBuild

# Use a dedicated external output; the script marks it as safe for later reuse
.\package_mvp.ps1 -OutputDir (Join-Path $env:TEMP "ClassInEDB-dist-smoke") -Clean -Zip
```

Windows portable archives are written as `ClassInEDBMVP-Portable.zip` to distinguish them from the recommended `ClassInEDBMVP-Setup.exe`. Each portable zip includes `EXTRACT_BEFORE_RUNNING.txt` at its top level. Users must extract the archive completely before launching `ClassInEDBMVP\ClassInEDBMVP.exe`; running the folder-style executable directly inside a zip can leave `_internal\python*.dll` unavailable in Windows' temporary extraction directory.

The packaging scripts remove deterministic previous outputs for the same app name before writing a new package. PyInstaller work files stay under the selected output directory and are removed after a successful build. Use a dedicated external `-OutputDir` when you need to keep older artifacts side by side.
Inside the repository, packaging output is restricted to the exact top-level `dist` directory. Outside the repository, a non-empty directory is refused unless a prior packaging run created `.edb-packaging-output`; this applies with or without `--clean`/`-Clean`. Cleanup also refuses the filesystem root, user home, project root, ancestors, `.git`, and source directories. Release-metadata generation independently enforces an exact output-directory name, allowed parent, sentinel, and file allowlist before replacement.
Before release sanity checks, clear old ignored local app outputs with `python scripts/clean_local_artifacts.py --yes`. The cleanup tool targets root-level `dist*`, `build`, and `tmp_validation_*` artifacts by default so stale test packages such as `dist_sizecheck` cannot be mistaken for the current UI; it also removes stale legacy UI bridge files under `ui_prototype`. Generated EDB exports are opt-in, while `.app_runtime` user state and `.venv` are always protected.

Expected output:
- **Default**: a folder containing the executable and dependencies: `dist\ClassInEDBMVP\`
- **Single file**: a standalone executable: `dist\ClassInEDBMVP.exe`

Typical packaged launch target:
- Default mode: `dist\ClassInEDBMVP\ClassInEDBMVP.exe`
- Standalone mode: `dist\ClassInEDBMVP.exe`

The default Windows build is windowed, so no console appears. Logs are written
under the current Windows Documents known folder; OneDrive and policy-based
Documents-folder redirection are respected:
```text
<Windows Documents>\ClassInEDBMVP\.app_runtime\app.log
```

Set `EDB_APP_HOME` to override the packaged app home. Environment variables in
the value are expanded, for example
`EDB_APP_HOME=%LOCALAPPDATA%\ClassInEDBMVP`.

### macOS `.app`
Install PyInstaller if needed and build:
```zsh
./package_macos_app.sh --install-pyinstaller --clean --zip
```

With in-app update metadata:
```zsh
./package_macos_app.sh --install-pyinstaller --clean --dmg --zip \
  --version 0.1.1 \
  --bundle-id "local.classin.edbmvp" \
  --update-feed-url "https://example.com/classin-edb/update.json" \
  --download-url "https://example.com/ClassInEDBMVP-macOS.dmg"
```

If PyInstaller is already installed:
```zsh
./package_macos_app.sh --clean --zip
```

Expected output:
```text
dist/ClassInEDBMVP.app
dist/ClassInEDBMVP-macOS.zip
```

The macOS wrapper removes stale same-name app folders, `.app` bundles, zip archives, DMGs, notary-upload zips, and previous PyInstaller work files before each build. After verifying the `.app`, it removes PyInstaller's sibling collect folder and temporary work directory so the output directory does not expose an extra runnable-looking copy.
The app bundle is re-verified after signing/stapling before archive creation. Generated zip archives and DMGs are checked for non-empty output; zip archives are inspected for the expected app entry, and DMGs are verified with `hdiutil verify` plus a mounted app-bundle contents check.
Notarized builds additionally require the final app to report a Developer ID Application authority and hardened-runtime flag, then run `stapler validate` on the stapled app and DMG.
The wrapper prints the final zip/DMG file size and SHA-256 hash after all signing/notarization steps that can mutate the artifact.

The default macOS build is windowed and ad-hoc signed when `codesign` is available. Logs are written under:
```text
~/Documents/ClassInEDBMVP/.app_runtime/app.log
```

For external macOS testing without Gatekeeper blocking, build with a real Apple Developer ID Application certificate and notarize the app/DMG:
```zsh
./package_macos_app.sh --clean --dmg --zip \
  --version 0.1.1 \
  --bundle-id "com.yourcompany.classin-edb" \
  --sign-identity "Developer ID Application: Your Company (TEAMID)" \
  --notarize \
  --notary-key "/secure/AuthKey_KEYID.p8" \
  --notary-key-id "KEYID" \
  --notary-issuer "ISSUER_UUID"
```

If the Developer ID certificate is installed in Keychain, `--sign-identity auto` selects the first `Developer ID Application` identity. You can also use a saved notarytool profile:
```zsh
xcrun notarytool store-credentials "classin-edb-notary" \
  --apple-id "developer@example.com" \
  --team-id "TEAMID" \
  --password "APP_SPECIFIC_PASSWORD"

./package_macos_app.sh --clean --dmg --zip \
  --sign-identity auto \
  --notarize \
  --notary-profile "classin-edb-notary"
```

The unsigned/ad-hoc DMG is fine for internal development, but a downloaded public macOS app needs Developer ID signing, notarization, and stapling to open cleanly on other Macs.

## In-App Updates
The app uses a semi-automatic update flow:
1. The installed app keeps user settings and API keys under the user's app runtime folder.
2. `칠판 설정` shows the current app version and an `업데이트 확인` button.
3. If the configured update feed reports a newer version, the app opens the configured download page in the browser.
4. The user installs the new `.dmg` or `Setup.exe` over the previous app. Existing API keys and session data stay in the runtime folder.

Update feed and download URLs must use HTTPS. Plain HTTP is accepted only for loopback development URLs such as `http://127.0.0.1:9999/update.json`.

Default update metadata lives in:
```text
app_update_config.json
```

The packaged app reads `app_update_config.json` from bundled resources, then allows a local override at:
```text
<Windows Documents>\ClassInEDBMVP\app_update_config.json
~/Documents/ClassInEDBMVP/app_update_config.json
```
Local overrides may use equivalent snake_case keys such as `download_url`; the runtime normalizes them to the canonical camelCase metadata keys before checking updates. If both alias forms are present with different values, update status becomes `invalid_config` instead of guessing between old and new release metadata.

Prefer the packaging scripts for release builds because they generate build-scoped update metadata and run the post-build package verifier. If you run `pyinstaller ClassInEDBMVP.spec` directly, the spec resolves assets relative to the spec file location and writes generated update metadata inside PyInstaller's work path; set the same metadata through environment variables such as `EDB_PACKAGE_APP_ID`, `EDB_PACKAGE_APP_VERSION`, `EDB_PACKAGE_UPDATE_FEED_URL`, and `EDB_PACKAGE_DOWNLOAD_URL`, then run `scripts/verify_packaged_app.py` on the built app folder.
Packaging scripts and direct `ClassInEDBMVP.spec` builds share the same metadata generator. Equivalent project `app_update_config.json` aliases are normalized into canonical camelCase keys before embedding metadata, and conflicting alias values fail the build instead of carrying old release URLs forward.

Update feed JSON shape:
```json
{
  "schemaVersion": 1,
  "appId": "ClassInEDBMVP",
  "appName": "ClassInEDBMVP",
  "channel": "stable",
  "version": "0.1.1",
  "publishedAt": "2026-06-19T00:00:00+00:00",
  "summary": "Bug fixes and packaging improvements.",
  "releaseNotesUrl": "https://example.com/releases/0.1.1",
  "manifestUrl": "https://example.com/releases/0.1.1/manifest.json",
  "manifestSha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "platforms": {
    "windows": {
      "version": "0.1.1",
      "downloadUrl": "https://example.com/ClassInEDBMVP-Setup.exe",
      "releaseNotesUrl": "https://example.com/releases/0.1.1",
      "fileName": "ClassInEDBMVP-Setup.exe",
      "artifactType": "setup-exe",
      "arch": "x64",
      "sizeBytes": 12345678,
      "sha256": "1111111111111111111111111111111111111111111111111111111111111111"
    },
    "macos": {
      "version": "0.1.1",
      "downloadUrl": "https://example.com/ClassInEDBMVP-macOS.dmg",
      "releaseNotesUrl": "https://example.com/releases/0.1.1",
      "fileName": "ClassInEDBMVP-macOS.dmg",
      "artifactType": "dmg",
      "arch": "arm64",
      "sizeBytes": 12345678,
      "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
    }
  }
}
```

Generate the feed after building release artifacts:
```zsh
python3 scripts/build_update_feed.py \
  --version 0.1.1 \
  --channel stable \
  --summary "Packaging and updater fixes." \
  --update-feed-url "https://example.com/classin-edb/update.json" \
  --release-notes-url "https://example.com/releases/0.1.1" \
  --manifest-url "https://example.com/releases/0.1.1/manifest.json" \
  --macos-url "https://example.com/ClassInEDBMVP-macOS.dmg" \
  --macos-file dist/ClassInEDBMVP-macOS.dmg \
  --windows-url "https://example.com/ClassInEDBMVP-Setup.exe" \
  --windows-file dist/ClassInEDBMVP-Setup.exe \
  --manifest-output dist/manifest.json \
  --checksums-output dist/checksums.txt \
  --output dist/update.json
```

Upload `dist/update.json` to the URL used by `--update-feed-url`, and keep `dist/manifest.json` plus `dist/checksums.txt` with the same release assets.
Update feed, manifest, release notes, and artifact download URLs must be HTTPS, except for loopback HTTP used in local testing. When a platform artifact file is supplied, its matching `--macos-url` or `--windows-url` is required. Runtime update checks also reject feed download URLs that do not expose a platform artifact extension such as `.dmg`, `.zip`, or `.exe`, or whose URL file name disagrees with an explicit feed `fileName`/`file_name`. This keeps installed apps from seeing an available update with no usable download action. The generated feed uses camelCase keys, and the runtime applies the same artifact validation to equivalent snake_case metadata aliases such as `artifact_type`, `file_name`, `manifest_sha256`, and `size_bytes`. If both alias forms are present in a feed, their values must match so the runtime never has to guess between old and new release metadata.
macOS update artifacts may use `dmg` or `zip`; Windows update artifacts must use `setup-exe`.
If a feed includes `appId` or `appName`, the installed app rejects it unless those identifiers match the packaged app metadata. This prevents an old or unrelated release channel from appearing as an available update.
Packaged builds must include `appId`, `appName`, and `version` in `app_update_config.json`; `scripts/verify_packaged_app.py` fails the artifact if those required update identifiers are missing or mismatched, if multiple packaged `app_update_config.json` files disagree, if camelCase/snake_case update metadata aliases conflict, if packaged update/download/release-note URLs do not use HTTPS or loopback HTTP, or if wrapper-supplied expected URLs are not actually embedded in the package.
The feed builder fails if `appId`, `appName`, `channel`, or release `version` is empty, so generated release metadata cannot silently lose its identity.
`manifestSha256` and artifact `sha256` values must be 64-character lowercase SHA-256 hex strings when present, and `sizeBytes` must be a positive integer.
If `--manifest-sha256` is supplied while generating a manifest, the builder verifies it against the generated `manifest.json` and fails on mismatch.
The feed builder also rejects known platform artifact mismatches, such as a `.exe` local file or download URL passed as a macOS DMG, a download URL without the expected artifact file extension, a download URL file name that disagrees with the supplied local artifact file, or the same local file reused for multiple platforms.

The GitHub Actions workflow in `.github/workflows/build-installers.yml` builds the macOS DMG/zip and Windows Setup.exe on matching runners. `production_release=true` (the default) fails closed unless the protected real-document corpus gate, explicit `license_compliance_approved` attestation, macOS Developer ID/notary secrets, and Windows Authenticode certificate all pass. Setting production mode to false is an explicit internal-test override that permits ad-hoc/unsigned artifacts. Release inputs are validated before runner-heavy jobs begin, and generated feed architecture comes from the actual build runners instead of a fixed assumption.
All Python build/test installs use exact hashed locks. GitHub Actions are pinned to full commit SHAs, and Windows downloads an exact Inno Setup 6.7.1 Chocolatey package only after verifying its nupkg SHA-256. Each packaged app embeds an SPDX SBOM, copied license files, exact dependency/tool fingerprints, and Git provenance. Platform evidence files bind the final installer hashes to that metadata and are rechecked after artifact download.
CI launches the macOS app executable and both the Windows portable and silently installed executables, verifies `/api/health`, `/api/runtime-diagnostics`, and the UI shell, then requires clean API shutdown. Public macOS builds additionally require valid Developer ID/hardened-runtime signatures, stapled tickets, and Gatekeeper acceptance; public Windows builds require every signable app binary and final installer to report a valid Authenticode signature.
The production corpus gate runs only on a self-hosted runner labeled `edb-quality-corpus`; `EDB_QUALITY_CORPUS_MANIFEST` and `EDB_QUALITY_CORPUS_ROOT` must be configured as repository variables or protected runner environment variables. The public CI synthetic corpus step is named and treated only as a harness smoke test, not production quality evidence. Every private run uses fresh `mktemp` work/report directories under `RUNNER_TEMP`; an `always()` cleanup validates the exact prefixes and removes rendered pages, OCR results, observations, and reports on success or failure. Reports are not uploaded or retained; only their SHA-256 digests remain in the workflow summary.
`update.json`, `manifest.json`, and `checksums.txt` are generated only when both platform download URLs are supplied. The two URLs must be provided together; production mode also requires the update-feed URL.
When `package_windows_installer.ps1` wraps an existing app folder, the installer version is derived from packaged `app_update_config.json` unless `-Version` is explicitly supplied; the packaged-app verifier fails if they disagree.

For signed public builds, configure these repository secrets:
```text
MACOS_CERTIFICATE_P12_BASE64
MACOS_CERTIFICATE_PASSWORD
MACOS_CODESIGN_IDENTITY
APPLE_NOTARY_KEY_ID
APPLE_NOTARY_ISSUER_ID
APPLE_NOTARY_KEY_P8_BASE64
WINDOWS_CERTIFICATE_PFX_BASE64
WINDOWS_CERTIFICATE_PASSWORD
```

Optional repository variable:
```text
WINDOWS_SIGN_TIMESTAMP_URL
```

If signing secrets are missing, disable `production_release` only for an intentional internal test; macOS then remains ad-hoc signed and Windows remains unsigned. Production mode intentionally fails instead of publishing untrusted artifacts.

## Optional Upscayl Redistribution Gate

Packaged apps discover a separately installed Upscayl runtime by default and do not redistribute it. To opt into a reviewed bundle, pass `--bundle-upscayl` on macOS, `-BundleUpscayl` on Windows, or set `EDB_BUNDLE_UPSCAYL=1` for a direct PyInstaller spec build.

Opt-in bundling fails unless `resources/upscayl/LICENSE`, `THIRD_PARTY_NOTICES.md`, and `CORRESPONDING_SOURCE.txt` are non-empty. The final package verifier checks the same files. This is an accidental-bundling safety gate, not a legal conclusion; AGPL corresponding-source scope and every model/binary license still require release-specific review.

The base Python package also includes dependencies with copyleft or dual-license considerations, notably PyMuPDF and pyhwp. Complete the technical/legal checklist in `docs/RELEASE_LICENSE_REVIEW.md` before asserting production license approval.

## Included Runtime Assets
- `ui_prototype\index.html`
- `ui_prototype\board.html`
- `ui_prototype\reorder.js`
- `ui_prototype\review_filters.js`
- `ui_prototype\publish_summary.js`
- `ui_prototype\publish_guard.js`
- `ui_prototype\app.bundle.js`
- `ui_prototype\vendor\react.production.min.js`
- `ui_prototype\vendor\react-dom.production.min.js`
- `app_update_config.json`
- `release_metadata/dependency-inventory.json`
- `release_metadata/sbom.spdx.json`
- `release_metadata/THIRD_PARTY_NOTICES.md`
- `release_metadata/release-provenance.json`
- `release_metadata/metadata-manifest.json` and copied `license-files/`
- `scripts\render_hwp_with_rhwp_core.mjs`
- `assets\app_icon.png`

## Notes
- `assets\app_icon.ico` and `assets\app_icon.icns` are packaging icon inputs for Windows/macOS builds; runtime package resources only include `assets\app_icon.png`.
- `ui_prototype\app.bundle.js` is generated from `art.jsx`, `tweaks-panel.jsx`, and `app.jsx` by `scripts\build_frontend_bundle.mjs`. The build-time Babel transformer lives under `scripts\vendor`, outside the browser UI asset tree. Packaging requires Node.js even when rebuilding is skipped: `scripts\build_frontend_bundle.mjs --check` reproduces the expected Babel output in memory and rejects any tracked bundle body drift. The builder also updates `board.html` to load the bundle with a source-digest cache-bust value.
- `scripts\verify_frontend_package.py` runs the deterministic bundle check before PyInstaller packaging, including direct `ClassInEDBMVP.spec` builds, and fails if required UI/runtime input assets are missing, packaging manifests omit them, `app.bundle.js` bytes differ from the current generated output, its source digest is stale, `board.html` points at a legacy runtime, browser-side Babel returns under `ui_prototype\vendor`, or old prototype files such as `ui_prototype\app.js`/`prototype_data.js` have returned.
- `scripts\verify_packaged_app.py` runs after folder-style packaging, source-package fallback builds, and Windows installer reuse of an existing app folder to confirm the final artifact contains the current prebuilt UI with bundle digest metadata, matching app/version update metadata, safe and expected packaged update URLs, matching macOS `Info.plist` bundle id/version metadata when present, HWP render helper, and the required source-package Python runtime modules when applicable, without browser-side Babel, build-time frontend tooling, legacy UI data files, or local runtime/session outputs.
- macOS and Windows packaging wrappers pass the current checkout as `--source-root`; a stale `dist` or external output therefore fails when its frontend source digest or the actual `app.bundle.js` byte SHA-256 differs from the checkout, even if a copied digest header and board cache-bust agree with each other. The Windows source-package fallback also requires `bug_reporting.py` and runs an isolated `app_server` import smoke. Windows `-OneFile` builds reuse `scripts/smoke_packaged_app.py` to launch with an isolated temporary `EDB_APP_HOME`, verify health, served `board.html`, its cache-busted bundle content/digest, update metadata, and clean shutdown before packaging continues.
- `scripts\build_app_update_config.py` is the single generator for packaged app update metadata across macOS, Windows, and direct PyInstaller spec builds.
- `scripts\clean_local_artifacts.py` removes ignored root-level packaging leftovers that can look like runnable current builds. Its default set covers `dist*`, `build`, `tmp_validation_*`, and stale legacy UI bridge files under `ui_prototype`; generated EDB exports require an explicit flag, while `.app_runtime` user state and `.venv` are always protected.
- Packaging wrappers also verify that generated distribution archives and installers are real non-empty files before reporting success or signing them. PowerShell packaging converts non-zero native command exits from Python, Node.js, PyInstaller, and Inno Setup into build failures so stale artifacts are not carried forward. Windows zip verification checks the root executable for PyInstaller one-file builds, the packaged executable under the app folder for PyInstaller folder builds, and `source-package\app_update_config.json` for source-package fallback builds. Windows signing fails if a requested package folder contains no signable `.exe`, `.dll`, or `.pyd` artifacts.
- Browser-side Babel is not included in packaged builds.
- Development runs write default and relative UI-named outputs under `.app_runtime\outputs` in the project folder.
- Packaged runs write default and relative UI-named outputs under `<Windows Documents>\ClassInEDBMVP\.app_runtime\outputs` on Windows and `~/Documents/ClassInEDBMVP/.app_runtime/outputs` on macOS.
- Uploaded files are cached in `.app_runtime\uploads` under the active app home.
- `generated_session.js` is kept and overwritten only as an empty compatibility bridge; latest-session restore uses `.app_runtime\latest_session.json` and session history. Legacy CLI bridge output stays under the selected output folder and must not recreate project `ui_prototype\generated_session.js` or `prototype_data.js`.
- The browser UI talks to the local server over HTTP and does not call Python directly.
- Double-clicking the packaged app opens the browser automatically. If the app server is already running, it opens the browser instead of starting a duplicate server.
- API keys are entered in the app's `칠판 설정` panel and stored locally under the app runtime folder.
- Updates are semi-automatic: the app checks configured release metadata and opens the installer download page; it does not self-replace in the background.
- Use the top-bar power button to stop the local app server after use.
- The current `.edb` export is still the MVP image-based board export, not the final mixed text/image writer.

## Known Limits
- OCR quality depends on optional local OCR dependencies.
- Packaged builds still rely on Python-side native dependencies like Pillow, PyMuPDF, and OpenCV.
- HWP conversion can still depend on external converters such as LibreOffice, Chrome, `hwpilot`, or configured command-line tools depending on the input path.
- The UI is connected to the MVP pipeline, but it is not yet a full production desktop shell.
