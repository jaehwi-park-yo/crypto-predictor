#!/usr/bin/env bash
set -euo pipefail

# ─── 설정 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
API_PORT=8000
# ─────────────────────────────────────────────────────────────────────────────

echo "===================================================="
echo " BTC Grid Prediction System  |  로컬 실행 스크립트"
echo "===================================================="

if ! command -v python3 > /dev/null 2>&1; then
    echo "❌ python3 가 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요."
    exit 1
fi

# 가상환경 없으면 생성
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[1/4] 가상환경 생성 중..."
    python3 -m venv "$VENV_DIR"
fi

# 가상환경 활성화
source "$VENV_DIR/bin/activate"

# 의존성 설치
echo "[2/4] 의존성 확인 중..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# 데이터 디렉토리 생성
mkdir -p "$SCRIPT_DIR/data"

# 기존 API 서버 프로세스 정리
if lsof -ti:$API_PORT > /dev/null 2>&1; then
    echo "  이미 $API_PORT 포트가 사용 중입니다. 기존 프로세스를 종료합니다..."
    lsof -ti:$API_PORT | xargs -r kill 2>/dev/null || true
    sleep 1
fi

# FastAPI 서버 백그라운드 실행
echo "[3/4] FastAPI 서버 시작 (http://localhost:$API_PORT) ..."
cd "$SCRIPT_DIR"
python api_server.py &
API_PID=$!
echo "  API 서버 PID: $API_PID"

# 서버 기동 대기 (최대 10초)
API_UP=0
for i in $(seq 1 10); do
    sleep 1
    if curl -s "http://localhost:$API_PORT/api/health" > /dev/null 2>&1; then
        echo "  API 서버 응답 확인 완료"
        API_UP=1
        break
    fi
    echo "  대기 중... ($i/10)"
done
if [ "$API_UP" -eq 0 ]; then
    echo "⚠️  API 서버 응답이 없습니다. 대시보드는 캐시/데모 데이터로 동작합니다."
fi

# index.html 브라우저로 열기
echo "[4/4] index.html 브라우저로 열기..."
if command -v open > /dev/null 2>&1; then
    open "$SCRIPT_DIR/index.html"          # macOS
elif command -v xdg-open > /dev/null 2>&1; then
    xdg-open "$SCRIPT_DIR/index.html"      # Linux
fi

echo ""
echo " ✔  API 서버: http://localhost:$API_PORT/api/health"
echo " ✔  대시보드: $SCRIPT_DIR/index.html"
echo ""
echo " Streamlit UI (선택): streamlit run gui/app.py"
echo ""
echo " 종료하려면 Ctrl+C 를 누르세요."

# API 서버 포그라운드 유지
trap "echo ''; echo '서버를 종료합니다...'; kill $API_PID 2>/dev/null; exit 0" INT TERM
wait $API_PID
