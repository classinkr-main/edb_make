#!/bin/zsh
set -euo pipefail

APP_NAME="ClassInEDBMVP"
APP_ID=""
OUTPUT_DIR="dist"
APP_VERSION=""
BUNDLE_ID="local.classin.edbmvp"
UPDATE_FEED_URL=""
DOWNLOAD_URL=""
RELEASE_NOTES_URL=""
SIGN_IDENTITY="${MACOS_CODESIGN_IDENTITY:-}"
ENTITLEMENTS_PATH=""
NOTARIZE=0
NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-}"
NOTARY_KEY="${APPLE_NOTARY_KEY:-}"
NOTARY_KEY_ID="${APPLE_NOTARY_KEY_ID:-}"
NOTARY_ISSUER="${APPLE_NOTARY_ISSUER:-}"
NOTARY_APPLE_ID="${APPLE_ID:-}"
NOTARY_PASSWORD="${APPLE_APP_PASSWORD:-}"
NOTARY_TEAM_ID="${APPLE_TEAM_ID:-}"
CLEAN=0
ZIP=0
DMG=0
CONSOLE=0
INSTALL_PYINSTALLER=0
SKIP_FRONTEND_BUILD=0
BUNDLE_UPSCAYL=0
PYTHON_EXE=""

usage() {
  cat <<'EOF'
Usage: ./package_macos_app.sh [options]

Options:
  --name NAME              App bundle name. Default: ClassInEDBMVP
  --app-id ID              Stable update feed app identifier
  --version VERSION        App version written into bundled update metadata
  --bundle-id ID           macOS bundle identifier. Default: local.classin.edbmvp
  --update-feed-url URL    JSON update feed checked by the in-app updater
  --download-url URL       Fallback installer/download page URL
  --release-notes-url URL  Fallback release notes URL
  --sign-identity ID       Developer ID Application identity, or "auto". Default: ad-hoc test signing
  --entitlements PATH      Optional entitlements plist used when Developer ID signing
  --notarize               Submit the signed app/DMG to Apple Notary and staple tickets
  --notary-profile NAME    notarytool keychain profile name
  --notary-key PATH        App Store Connect API key .p8 path
  --notary-key-id ID       App Store Connect API key ID
  --notary-issuer ID       App Store Connect issuer ID
  --apple-id EMAIL         Apple ID for notarytool password auth
  --apple-password PASS    App-specific password for notarytool password auth
  --team-id ID             Apple Developer Team ID for password auth
  --output-dir DIR         Output directory. Default: dist
  --python PATH            Python executable to use. Default: .venv/bin/python or python3
  --clean                  Remove previous build output first
  --zip                    Create a zip archive next to the app bundle
  --dmg                    Create a drag-and-drop DMG installer
  --console                Keep a console build for debugging
  --install-pyinstaller    Install PyInstaller before packaging
  --skip-frontend-build     Skip rebuild; Node still verifies deterministic bundle output
  --bundle-upscayl         Bundle resources/upscayl after license-compliance validation
  -h, --help               Show this help
EOF
}

require_nonempty_file() {
  local path="$1"
  local label="$2"
  if [[ ! -s "$path" ]]; then
    echo "$label was not created or is empty: $path" >&2
    exit 1
  fi
}

file_size_bytes() {
  local file_path="$1"
  local size
  if size="$(stat -f "%z" "$file_path" 2>/dev/null)"; then
    echo "$size"
  else
    wc -c < "$file_path" | tr -d '[:space:]'
  fi
}

print_file_artifact_summary() {
  local file_path="$1"
  local label="$2"
  local sha
  if [[ ! -f "$file_path" ]]; then
    return
  fi
  sha="$(shasum -a 256 "$file_path" | awk '{ print $1 }')"
  echo "$label: $file_path"
  echo "$label size: $(file_size_bytes "$file_path") bytes"
  echo "$label sha256: $sha"
}

require_zip_entry() {
  local zip_path="$1"
  local entry="$2"
  if ! command -v zipinfo >/dev/null 2>&1; then
    echo "zipinfo is required to inspect zip archive contents." >&2
    exit 1
  fi
  if ! zipinfo -1 "$zip_path" | awk -v expected="$entry" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'; then
    echo "Zip archive is missing expected entry: $entry" >&2
    exit 1
  fi
}

verify_packaged_app_root() {
  local package_root="$1"
  local verify_args=(
    "$package_root" \
    --expected-app-id "$EFFECTIVE_APP_ID" \
    --expected-app-name "$APP_NAME" \
    --expected-version "$EFFECTIVE_APP_VERSION" \
    --expected-update-feed-url "$EFFECTIVE_UPDATE_FEED_URL" \
    --expected-download-url "$EFFECTIVE_DOWNLOAD_URL" \
    --expected-release-notes-url "$EFFECTIVE_RELEASE_NOTES_URL" \
    --expected-bundle-id "$BUNDLE_ID" \
    --source-root "$PROJECT_ROOT"
  )
  if [[ -n "${EDB_RELEASE_GIT_COMMIT:-}" ]]; then
    verify_args+=(--expected-git-commit "$EDB_RELEASE_GIT_COMMIT")
  fi
  "$PYTHON_EXE" "$PROJECT_ROOT/scripts/verify_packaged_app.py" "${verify_args[@]}"
}

verify_dmg_contains_app() {
  local dmg_path="$1"
  local app_name="$2"
  local mount_root
  local attach_plist
  local mount_point

  mount_root="$(mktemp -d "$RESOLVED_OUTPUT_DIR/${app_name}.mount.XXXXXX")"
  attach_plist="$mount_root/attach.plist"
  mount_point=""

  if ! hdiutil attach -plist -readonly -nobrowse -mountroot "$mount_root" "$dmg_path" > "$attach_plist"; then
    rm -rf "$mount_root"
    echo "Could not mount DMG for inspection: $dmg_path" >&2
    exit 1
  fi

  if ! mount_point="$("$PYTHON_EXE" - "$attach_plist" <<'PY'
import plistlib
import sys
from pathlib import Path

data = plistlib.loads(Path(sys.argv[1]).read_bytes())
for entity in data.get("system-entities", []):
    mount_point = entity.get("mount-point")
    if mount_point:
        print(mount_point)
        raise SystemExit(0)
raise SystemExit(1)
PY
)"; then
    hdiutil detach "$mount_root" -quiet >/dev/null 2>&1 || true
    rm -rf "$mount_root"
    echo "Could not locate mounted DMG volume: $dmg_path" >&2
    exit 1
  fi

  if [[ ! -f "$mount_point/$app_name.app/Contents/Info.plist" ]]; then
    hdiutil detach "$mount_point" -quiet >/dev/null 2>&1 || true
    rm -rf "$mount_root"
    echo "DMG is missing expected app bundle: $app_name.app/Contents/Info.plist" >&2
    exit 1
  fi

  hdiutil detach "$mount_point" -quiet
  rm -rf "$mount_root"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --app-id)
      APP_ID="${2:-}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --version)
      APP_VERSION="${2:-}"
      shift 2
      ;;
    --bundle-id)
      BUNDLE_ID="${2:-}"
      shift 2
      ;;
    --update-feed-url)
      UPDATE_FEED_URL="${2:-}"
      shift 2
      ;;
    --download-url)
      DOWNLOAD_URL="${2:-}"
      shift 2
      ;;
    --release-notes-url)
      RELEASE_NOTES_URL="${2:-}"
      shift 2
      ;;
    --sign-identity)
      SIGN_IDENTITY="${2:-}"
      shift 2
      ;;
    --entitlements)
      ENTITLEMENTS_PATH="${2:-}"
      shift 2
      ;;
    --notarize)
      NOTARIZE=1
      shift
      ;;
    --notary-profile)
      NOTARY_PROFILE="${2:-}"
      shift 2
      ;;
    --notary-key)
      NOTARY_KEY="${2:-}"
      shift 2
      ;;
    --notary-key-id)
      NOTARY_KEY_ID="${2:-}"
      shift 2
      ;;
    --notary-issuer)
      NOTARY_ISSUER="${2:-}"
      shift 2
      ;;
    --apple-id)
      NOTARY_APPLE_ID="${2:-}"
      shift 2
      ;;
    --apple-password)
      NOTARY_PASSWORD="${2:-}"
      shift 2
      ;;
    --team-id)
      NOTARY_TEAM_ID="${2:-}"
      shift 2
      ;;
    --python)
      PYTHON_EXE="${2:-}"
      shift 2
      ;;
    --clean)
      CLEAN=1
      shift
      ;;
    --zip)
      ZIP=1
      shift
      ;;
    --dmg)
      DMG=1
      shift
      ;;
    --console)
      CONSOLE=1
      shift
      ;;
    --install-pyinstaller)
      INSTALL_PYINSTALLER=1
      shift
      ;;
    --skip-frontend-build)
      SKIP_FRONTEND_BUILD=1
      shift
      ;;
    --bundle-upscayl)
      BUNDLE_UPSCAYL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

if [[ -n "$ENTITLEMENTS_PATH" && "$ENTITLEMENTS_PATH" != /* ]]; then
  ENTITLEMENTS_PATH="$PROJECT_ROOT/$ENTITLEMENTS_PATH"
fi
if [[ -n "$ENTITLEMENTS_PATH" && ! -f "$ENTITLEMENTS_PATH" ]]; then
  echo "Entitlements file not found: $ENTITLEMENTS_PATH" >&2
  exit 2
fi

if [[ "$SIGN_IDENTITY" == "auto" ]]; then
  SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '"' '/Developer ID Application/ { print $2; exit }')"
  if [[ -z "$SIGN_IDENTITY" ]]; then
    echo "No Developer ID Application identity was found in the keychain." >&2
    exit 2
  fi
fi

if [[ "$NOTARIZE" == "1" ]]; then
  if [[ -z "$SIGN_IDENTITY" || "$SIGN_IDENTITY" == "-" ]]; then
    echo "--notarize requires --sign-identity with a Developer ID Application certificate." >&2
    exit 2
  fi
  if ! command -v xcrun >/dev/null 2>&1; then
    echo "xcrun is required for Apple notarization." >&2
    exit 2
  fi
fi

if [[ -z "$PYTHON_EXE" ]]; then
  if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PYTHON_EXE="$PROJECT_ROOT/.venv/bin/python"
  else
    PYTHON_EXE="$(command -v python3)"
  fi
fi

RESOLVED_OUTPUT_DIR="$("$PYTHON_EXE" - "$PROJECT_ROOT" "$OUTPUT_DIR" "$CLEAN" <<'PY'
import sys
from pathlib import Path
import re

project_root = Path(sys.argv[1]).resolve()
raw_output = Path(sys.argv[2]).expanduser()
will_clean = sys.argv[3] == "1"
output = (raw_output if raw_output.is_absolute() else project_root / raw_output).resolve()
protected = {Path(output.anchor), Path.home().resolve(), project_root}
if output in protected or output in project_root.parents or any(part.lower() == ".git" for part in output.parts):
    raise SystemExit(f"Refusing unsafe packaging output directory: {output}")
try:
    relative = output.relative_to(project_root)
except ValueError:
    relative = None
if relative is not None:
    top_level = relative.parts[0] if relative.parts else ""
    if top_level != "dist":
        raise SystemExit(
            f"Refusing project-internal packaging output outside the exact dist allowlist: {output}"
        )
elif output.exists() and any(output.iterdir()):
    sentinel = output / ".edb-packaging-output"
    if not sentinel.is_file():
        raise SystemExit(
            f"Refusing to clean non-empty unmarked external packaging output: {output}"
        )
print(output)
PY
)"

if [[ "$INSTALL_PYINSTALLER" == "1" ]]; then
  "$PYTHON_EXE" -m pip install --disable-pip-version-check --require-hashes -r "$PROJECT_ROOT/requirements-release-bootstrap.lock"
  "$PYTHON_EXE" -m pip install --disable-pip-version-check --require-hashes --no-build-isolation -r "$PROJECT_ROOT/requirements-release.lock"
fi

LICENSE_VERIFY_ARGS=(
  --root "$PROJECT_ROOT"
  --require-release-policy
  --require-locked-environment
  --reject-unlocked-environment
)
if [[ "$BUNDLE_UPSCAYL" == "1" ]]; then
  LICENSE_VERIFY_ARGS+=(--bundle-upscayl)
fi
"$PYTHON_EXE" "$PROJECT_ROOT/scripts/verify_release_licenses.py" "${LICENSE_VERIFY_ARGS[@]}"

if [[ "$CLEAN" == "1" ]]; then
  rm -rf "$RESOLVED_OUTPUT_DIR"
fi
mkdir -p "$RESOLVED_OUTPUT_DIR"
echo "generated; safe to replace" > "$RESOLVED_OUTPUT_DIR/.edb-packaging-output"
SPEC_DIR="$RESOLVED_OUTPUT_DIR/_pyinstaller_spec"
mkdir -p "$SPEC_DIR"
WORK_DIR="$RESOLVED_OUTPUT_DIR/_pyinstaller_build"
APP_PATH="$RESOLVED_OUTPUT_DIR/$APP_NAME.app"
APP_DIR_PATH="$RESOLVED_OUTPUT_DIR/$APP_NAME"
ZIP_PATH="$RESOLVED_OUTPUT_DIR/$APP_NAME-macOS.zip"
DMG_PATH="$RESOLVED_OUTPUT_DIR/$APP_NAME-macOS.dmg"
APP_NOTARY_ZIP="$RESOLVED_OUTPUT_DIR/$APP_NAME-notary-upload.zip"
RELEASE_METADATA_DIR="$RESOLVED_OUTPUT_DIR/release-metadata"

rm -rf "$WORK_DIR" "$APP_PATH" "$APP_DIR_PATH" "$ZIP_PATH" "$DMG_PATH" "$APP_NOTARY_ZIP" "$RELEASE_METADATA_DIR"
find "$RESOLVED_OUTPUT_DIR" -maxdepth 1 -type d -name "$APP_NAME.dmg.*" -exec rm -rf {} +
find "$RESOLVED_OUTPUT_DIR" -maxdepth 1 -type d -name "$APP_NAME.mount.*" -exec rm -rf {} +

APP_UPDATE_CONFIG="$SPEC_DIR/app_update_config.json"
EDB_PACKAGE_APP_ID="$APP_ID" \
EDB_PACKAGE_APP_NAME="$APP_NAME" \
EDB_PACKAGE_APP_VERSION="$APP_VERSION" \
EDB_PACKAGE_UPDATE_FEED_URL="$UPDATE_FEED_URL" \
EDB_PACKAGE_DOWNLOAD_URL="$DOWNLOAD_URL" \
EDB_PACKAGE_RELEASE_NOTES_URL="$RELEASE_NOTES_URL" \
"$PYTHON_EXE" "$PROJECT_ROOT/scripts/build_app_update_config.py" "$PROJECT_ROOT/app_update_config.json" "$APP_UPDATE_CONFIG"

EFFECTIVE_APP_ID="$("$PYTHON_EXE" - "$APP_UPDATE_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("appId") or config.get("appName") or "ClassInEDBMVP"))
PY
)"

EFFECTIVE_APP_VERSION="$("$PYTHON_EXE" - "$APP_UPDATE_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("version") or "0.1.0"))
PY
)"

EFFECTIVE_UPDATE_FEED_URL="$("$PYTHON_EXE" - "$APP_UPDATE_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("updateFeedUrl") or ""))
PY
)"

EFFECTIVE_DOWNLOAD_URL="$("$PYTHON_EXE" - "$APP_UPDATE_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("downloadUrl") or ""))
PY
)"

EFFECTIVE_RELEASE_NOTES_URL="$("$PYTHON_EXE" - "$APP_UPDATE_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("releaseNotesUrl") or ""))
PY
)"

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}" \
"$PYTHON_EXE" "$PROJECT_ROOT/scripts/build_release_metadata.py" build \
  --root "$PROJECT_ROOT" \
  --output-dir "$RELEASE_METADATA_DIR" \
  --version "$EFFECTIVE_APP_VERSION" \
  --git-commit "${EDB_RELEASE_GIT_COMMIT:-}" \
  --strict-environment

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required to build or deterministically verify ui_prototype/app.bundle.js." >&2
  exit 1
fi
if [[ "$SKIP_FRONTEND_BUILD" == "0" ]]; then
  node "$PROJECT_ROOT/scripts/build_frontend_bundle.mjs"
fi

"$PYTHON_EXE" "$PROJECT_ROOT/scripts/verify_frontend_package.py" --root "$PROJECT_ROOT"

if ! "$PYTHON_EXE" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller가 설치되어 있지 않습니다. --install-pyinstaller 옵션으로 다시 실행하세요." >&2
  exit 1
fi

WINDOW_ARG="--windowed"
if [[ "$CONSOLE" == "1" ]]; then
  WINDOW_ARG="--console"
fi

ICON_ARGS=()
if [[ -f "$PROJECT_ROOT/assets/app_icon.icns" ]]; then
  ICON_ARGS=(--icon "$PROJECT_ROOT/assets/app_icon.icns")
fi

DATA_ARGS=()
DATA_ARGS+=(--add-data "$APP_UPDATE_CONFIG:.")
DATA_ARGS+=(--add-data "${RELEASE_METADATA_DIR}:release_metadata")
HIDDEN_IMPORT_ARGS=(
  --hidden-import preprocess
  --hidden-import build_mvp_export
  --hidden-import build_problem_board_edb
  --hidden-import build_structured_page_json
  --hidden-import edb_builder
  --hidden-import page_repair
  --hidden-import image_reconstruction_backend
  --hidden-import upscayl_backend
)
add_data() {
  local src="$1"
  local dest="$2"
  if [[ -e "$PROJECT_ROOT/$src" ]]; then
    DATA_ARGS+=(--add-data "$PROJECT_ROOT/$src:$dest")
  fi
}

add_data "ui_prototype/index.html" "ui_prototype"
add_data "ui_prototype/board.html" "ui_prototype"
add_data "ui_prototype/board.css" "ui_prototype"
add_data "ui_prototype/favicon.png" "ui_prototype"
add_data "ui_prototype/reorder.js" "ui_prototype"
add_data "ui_prototype/review_filters.js" "ui_prototype"
add_data "ui_prototype/publish_summary.js" "ui_prototype"
add_data "ui_prototype/publish_guard.js" "ui_prototype"
add_data "ui_prototype/app.bundle.js" "ui_prototype"
add_data "ui_prototype/vendor/react.production.min.js" "ui_prototype/vendor"
add_data "ui_prototype/vendor/react-dom.production.min.js" "ui_prototype/vendor"
add_data "ui_prototype/vendor/fonts/fonts.css" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/PretendardVariable.woff2" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/JetBrainsMonoVariable.woff2" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/CaveatVariable.woff2" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/OFL-Pretendard.txt" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/OFL-JetBrainsMono.txt" "ui_prototype/vendor/fonts"
add_data "ui_prototype/vendor/fonts/OFL-Caveat.txt" "ui_prototype/vendor/fonts"
add_data "scripts/render_hwp_with_rhwp_core.mjs" "scripts"
add_data "assets/app_icon.png" "assets"
if [[ "$BUNDLE_UPSCAYL" == "1" ]]; then
  add_data "resources/upscayl/LICENSE" "resources/upscayl"
  add_data "resources/upscayl/THIRD_PARTY_NOTICES.md" "resources/upscayl"
  add_data "resources/upscayl/CORRESPONDING_SOURCE.txt" "resources/upscayl"
  add_data "resources/upscayl/models" "resources/upscayl/models"
  add_data "resources/upscayl/mac" "resources/upscayl/mac"
fi

"$PYTHON_EXE" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  "$WINDOW_ARG" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --distpath "$RESOLVED_OUTPUT_DIR" \
  --specpath "$SPEC_DIR" \
  --workpath "$WORK_DIR" \
  --name "$APP_NAME" \
  "${DATA_ARGS[@]}" \
  "${HIDDEN_IMPORT_ARGS[@]}" \
  "${ICON_ARGS[@]}" \
  app_server.py

PLIST_PATH="$APP_PATH/Contents/Info.plist"
if [[ -f "$PLIST_PATH" && -x "/usr/libexec/PlistBuddy" ]]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $EFFECTIVE_APP_VERSION" "$PLIST_PATH" >/dev/null 2>&1 || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $EFFECTIVE_APP_VERSION" "$PLIST_PATH" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $EFFECTIVE_APP_VERSION" "$PLIST_PATH" >/dev/null 2>&1 || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $EFFECTIVE_APP_VERSION" "$PLIST_PATH" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$PLIST_PATH" >/dev/null 2>&1 || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $BUNDLE_ID" "$PLIST_PATH" >/dev/null
fi

PACKAGED_APP_ROOT=""
if [[ -d "$APP_PATH" ]]; then
  PACKAGED_APP_ROOT="$APP_PATH"
elif [[ -d "$RESOLVED_OUTPUT_DIR/$APP_NAME" ]]; then
  PACKAGED_APP_ROOT="$RESOLVED_OUTPUT_DIR/$APP_NAME"
fi
if [[ -n "$PACKAGED_APP_ROOT" ]]; then
  verify_packaged_app_root "$PACKAGED_APP_ROOT"
fi
if [[ -d "$APP_PATH" && -d "$APP_DIR_PATH" ]]; then
  rm -rf "$APP_DIR_PATH"
fi
if [[ -d "$APP_PATH" ]]; then
  rm -rf "$WORK_DIR"
fi

notarytool_submit() {
  local artifact_path="$1"
  local args=(notarytool submit "$artifact_path" --wait)
  if [[ -n "$NOTARY_PROFILE" ]]; then
    args+=(--keychain-profile "$NOTARY_PROFILE")
  elif [[ -n "$NOTARY_KEY" && -n "$NOTARY_KEY_ID" && -n "$NOTARY_ISSUER" ]]; then
    args+=(--key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER")
  elif [[ -n "$NOTARY_APPLE_ID" && -n "$NOTARY_PASSWORD" && -n "$NOTARY_TEAM_ID" ]]; then
    args+=(--apple-id "$NOTARY_APPLE_ID" --password "$NOTARY_PASSWORD" --team-id "$NOTARY_TEAM_ID")
  else
    echo "Notarization credentials are missing. Provide --notary-profile, App Store Connect API key args, or Apple ID password args." >&2
    exit 2
  fi
  xcrun "${args[@]}"
}

if [[ -d "$APP_PATH" ]] && command -v codesign >/dev/null 2>&1; then
  if [[ -n "$SIGN_IDENTITY" && "$SIGN_IDENTITY" != "-" ]]; then
    SIGN_ARGS=(--force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY")
    if [[ -n "$ENTITLEMENTS_PATH" ]]; then
      SIGN_ARGS+=(--entitlements "$ENTITLEMENTS_PATH")
    fi
    codesign "${SIGN_ARGS[@]}" "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
    if [[ "$NOTARIZE" == "1" ]]; then
      SIGNING_DETAILS="$(codesign -dvvv "$APP_PATH" 2>&1)"
      if ! grep -Fq "Authority=Developer ID Application:" <<<"$SIGNING_DETAILS"; then
        echo "Notarization requires a Developer ID Application signature." >&2
        exit 2
      fi
      if ! grep -Eq 'flags=.*\(runtime\)' <<<"$SIGNING_DETAILS"; then
        echo "Notarization requires the hardened runtime flag." >&2
        exit 2
      fi
    fi
  else
    codesign --force --deep --sign - "$APP_PATH" >/dev/null 2>&1 || true
  fi
  verify_packaged_app_root "$APP_PATH"
fi

if [[ "$NOTARIZE" == "1" && -d "$APP_PATH" ]]; then
  rm -f "$APP_NOTARY_ZIP"
  (cd "$RESOLVED_OUTPUT_DIR" && /usr/bin/ditto -c -k --keepParent --zlibCompressionLevel 9 "$APP_NAME.app" "$APP_NOTARY_ZIP")
  require_nonempty_file "$APP_NOTARY_ZIP" "Notary upload archive"
  notarytool_submit "$APP_NOTARY_ZIP"
  xcrun stapler staple "$APP_PATH"
  xcrun stapler validate "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
  verify_packaged_app_root "$APP_PATH"
  rm -f "$APP_NOTARY_ZIP"
fi

if [[ "$ZIP" == "1" && -d "$APP_PATH" ]]; then
  rm -f "$ZIP_PATH"
  (cd "$RESOLVED_OUTPUT_DIR" && /usr/bin/ditto -c -k --keepParent --zlibCompressionLevel 9 "$APP_NAME.app" "$(basename "$ZIP_PATH")")
  require_nonempty_file "$ZIP_PATH" "Zip archive"
  require_zip_entry "$ZIP_PATH" "$APP_NAME.app/Contents/Info.plist"
  print_file_artifact_summary "$ZIP_PATH" "Zip archive"
fi

if [[ "$DMG" == "1" && -d "$APP_PATH" ]]; then
  STAGING_DIR="$(mktemp -d "$RESOLVED_OUTPUT_DIR/${APP_NAME}.dmg.XXXXXX")"
  rm -f "$DMG_PATH"
  /usr/bin/ditto "$APP_PATH" "$STAGING_DIR/$APP_NAME.app"
  ln -s /Applications "$STAGING_DIR/Applications"
  hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH" >/dev/null
  rm -rf "$STAGING_DIR"
  require_nonempty_file "$DMG_PATH" "DMG installer"
  if [[ -n "$SIGN_IDENTITY" && "$SIGN_IDENTITY" != "-" ]] && command -v codesign >/dev/null 2>&1; then
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG_PATH"
    codesign --verify --verbose=2 "$DMG_PATH"
  fi
  hdiutil verify "$DMG_PATH"
  verify_dmg_contains_app "$DMG_PATH" "$APP_NAME"
  if [[ "$NOTARIZE" == "1" ]]; then
    notarytool_submit "$DMG_PATH"
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
    hdiutil verify "$DMG_PATH"
    verify_dmg_contains_app "$DMG_PATH" "$APP_NAME"
  fi
  print_file_artifact_summary "$DMG_PATH" "DMG installer"
fi

echo "Packaging complete."
if [[ -d "$APP_PATH" ]]; then
  echo "App bundle: $APP_PATH"
  if [[ "$ZIP" == "1" ]]; then
    echo "Zip archive: $ZIP_PATH"
  fi
  if [[ "$DMG" == "1" ]]; then
    echo "DMG installer: $DMG_PATH"
  fi
else
  echo "Output folder: $RESOLVED_OUTPUT_DIR"
fi
