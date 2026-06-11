#!/usr/bin/env bash
# BTC Grid Prediction — 패치 적용 (Mac / Linux)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "===================================================="
echo " BTC Grid Prediction — 패치 적용 도구"
echo "===================================================="
echo ""

NEW_VER="unknown"
[ -f "$SCRIPT_DIR/VERSION" ] && NEW_VER=$(cat "$SCRIPT_DIR/VERSION" | tr -d '[:space:]')
echo " 새 버전: v$NEW_VER"
echo ""
echo " [주의] 현재 폴더의 소스 파일을 덮어씁니다."
echo "        data/ 폴더(수집된 데이터)는 보호됩니다."
echo ""
read -r -p " 계속 하시겠습니까? (y/N): " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "취소되었습니다."
    exit 0
fi
echo ""

# ── 실행 중인 서버 종료 ──────────────────────────────────
echo "[1/4] 실행 중인 서버 확인 및 종료..."
if lsof -ti:8000 > /dev/null 2>&1; then
    lsof -ti:8000 | xargs -r kill 2>/dev/null || true
    sleep 1
    echo "       포트 8000 서버 종료 완료"
else
    echo "       실행 중인 서버 없음"
fi

# ── data 폴더 보호 확인 ──────────────────────────────────
echo "[2/4] 데이터 폴더 보호 확인..."
mkdir -p "$SCRIPT_DIR/data"
echo "       data/ 폴더 보호됨 (사용자 데이터 유지)"

# ── 의존성 재설치 ────────────────────────────────────────
echo "[3/4] 패키지 의존성 업데이트..."
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
    python3 -m pip install -q -r "$SCRIPT_DIR/requirements.txt"
    echo "       의존성 업데이트 완료"
else
    echo "       가상환경 없음 — 첫 실행 시 start.sh 이 자동 생성합니다."
fi

# ── 완료 ─────────────────────────────────────────────────
echo "[4/4] 패치 완료!"
echo ""
echo "===================================================="
echo " 버전 v$NEW_VER 업데이트 완료"
echo " start.sh 을 실행해 서버를 다시 시작하세요."
echo "===================================================="
echo ""
if [ -f "$SCRIPT_DIR/CHANGELOG.md" ]; then
    echo " 변경 이력:"
    echo " ──────────────────────────"
    head -20 "$SCRIPT_DIR/CHANGELOG.md"
fi
