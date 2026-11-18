#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

HOST="${EDB_HOST:-127.0.0.1}"
PORT="${EDB_PORT:-8765}"
URL="http://${HOST}:${PORT}/"
HEALTH_URL="${URL}api/health"
NO_BROWSER="${EDB_NO_BROWSER:-0}"
FOREGROUND="${EDB_FOREGROUND:-0}"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements-local.txt"
RUNTIME_DIR="$PROJECT_ROOT/.app_runtime"
REQUIREMENTS_STAMP="$RUNTIME_DIR/requirements-local.sha256"
APP_LOG="$RUNTIME_DIR/app.log"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11

pause_before_exit() {
  read -r "?Enter를 누르면 창을 닫습니다." || true
}

python_meets_min_version() {
  local candidate="$1"
  [[ -x "$candidate" ]] || return 1
  "$candidate" - "$MIN_PYTHON_MAJOR" "$MIN_PYTHON_MINOR" <<'PY' >/dev/null 2>&1
import sys

required = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= required else 1)
PY
}

python_version_label() {
  local candidate="$1"
  local version
  if version="$("$candidate" - <<'PY' 2>/dev/null
import sys

print(".".join(str(part) for part in sys.version_info[:3]))
PY
  )"; then
    echo "$version"
  else
    echo "알 수 없음"
  fi
}

find_suitable_python() {
  local candidate candidate_path
  for candidate in "${EDB_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    candidate_path="$(command -v "$candidate" 2>/dev/null || true)"
    [[ -n "$candidate_path" ]] || continue
    if python_meets_min_version "$candidate_path"; then
      echo "$candidate_path"
      return 0
    fi
  done

  return 1
}

create_or_recreate_venv() {
  local action="$1"
  local suitable_python

  suitable_python="$(find_suitable_python || true)"
  if [[ -z "$suitable_python" ]]; then
    echo "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} 이상 인터프리터를 찾지 못했습니다."
    echo "현재 코드는 enum.StrEnum을 사용하므로 Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+가 필요합니다."
    echo "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} 이상을 설치한 뒤 다시 실행해주세요."
    pause_before_exit
    exit 1
  fi

  echo "$action"
  echo "사용할 Python: $suitable_python ($(python_version_label "$suitable_python"))"
  rm -rf "$VENV_DIR"
  "$suitable_python" -m venv "$VENV_DIR"

  if ! python_meets_min_version "$PYTHON_BIN"; then
    echo ".venv 생성 후에도 Python 버전 확인에 실패했습니다."
    echo "생성된 .venv Python: $(python_version_label "$PYTHON_BIN")"
    pause_before_exit
    exit 1
  fi

  REQUIREMENTS_INSTALL_NEEDED=1
}

echo "ClassIn EDB 로컬 앱을 시작합니다."
echo "프로젝트: $PROJECT_ROOT"
echo "주소: $URL"
echo ""

if /usr/bin/curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo "이미 로컬 앱 서버가 실행 중입니다. 브라우저만 엽니다."
  if [[ "$NO_BROWSER" != "1" && "$NO_BROWSER" != "true" && "$NO_BROWSER" != "yes" ]]; then
    /usr/bin/open "$URL"
  fi
  exit 0
fi

REQUIREMENTS_INSTALL_NEEDED=0

if [[ -x "$PYTHON_BIN" ]]; then
  if python_meets_min_version "$PYTHON_BIN"; then
    echo ".venv Python $(python_version_label "$PYTHON_BIN") 확인 완료."
  else
    OLD_VENV_VERSION="$(python_version_label "$PYTHON_BIN")"
    create_or_recreate_venv "기존 .venv Python ${OLD_VENV_VERSION}은 지원되지 않습니다. Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+로 다시 만듭니다."
  fi
else
  create_or_recreate_venv ".venv가 없어 새로 만듭니다. 첫 실행에서만 시간이 조금 걸릴 수 있습니다."
fi

mkdir -p "$RUNTIME_DIR"

if [[ -f "$REQUIREMENTS_FILE" ]]; then
  CURRENT_REQUIREMENTS_HASH="$("$PYTHON_BIN" - "$REQUIREMENTS_FILE" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
  )"
  SAVED_REQUIREMENTS_HASH="$(cat "$REQUIREMENTS_STAMP" 2>/dev/null || true)"
  if [[ "$REQUIREMENTS_INSTALL_NEEDED" == "1" || "$CURRENT_REQUIREMENTS_HASH" != "$SAVED_REQUIREMENTS_HASH" ]]; then
    echo "필수 패키지를 확인/설치합니다. 새 .venv이거나 requirements-local.txt가 바뀐 경우에만 다시 실행됩니다."
    "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE"
    echo "$CURRENT_REQUIREMENTS_HASH" > "$REQUIREMENTS_STAMP"
  fi
fi

echo ""
echo "로컬 앱 서버를 실행합니다."
echo ""

APP_ARGS=(
  "$PROJECT_ROOT/app_server.py"
  "--host" "$HOST"
  "--port" "$PORT"
  "--no-open-browser"
  "--log-file" "$APP_LOG"
)

if [[ "$FOREGROUND" == "1" || "$FOREGROUND" == "true" || "$FOREGROUND" == "yes" ]]; then
  echo "포그라운드 개발 모드입니다. 이 터미널을 닫으면 서버도 종료됩니다."
  echo "로그: $APP_LOG"
  exec "$PYTHON_BIN" "${APP_ARGS[@]}"
fi

echo "서버를 터미널과 분리해 실행합니다. 이 창은 닫아도 됩니다."
echo "로그: $APP_LOG"
nohup "$PYTHON_BIN" "${APP_ARGS[@]}" </dev/null >>"$APP_LOG" 2>&1 &
SERVER_PID=$!

for attempt_no in {1..150}; do
  if /usr/bin/curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "서버 시작 완료 (PID $SERVER_PID)"
    if [[ "$NO_BROWSER" != "1" && "$NO_BROWSER" != "true" && "$NO_BROWSER" != "yes" ]]; then
      /usr/bin/open "$URL"
    fi
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "서버가 시작 중 종료되었습니다. 최근 로그:"
    /usr/bin/tail -n 30 "$APP_LOG" 2>/dev/null || true
    pause_before_exit
    exit 1
  fi
  sleep 0.1
done

echo "서버가 제한 시간 안에 준비되지 않았습니다. 최근 로그:"
/usr/bin/tail -n 30 "$APP_LOG" 2>/dev/null || true
pause_before_exit
exit 1
