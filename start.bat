@echo off
chcp 65001 > nul
title BTC Grid Prediction — 로컬 실행

:: ─── 설정 ───────────────────────────────────────────────────────────────────
set VENV_DIR=.venv
set API_PORT=8000
set GUI_PORT=8501
:: ────────────────────────────────────────────────────────────────────────────

echo ====================================================
echo  BTC Grid Prediction System  ^|  로컬 실행 스크립트
echo ====================================================

:: 가상환경 없으면 생성
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [1/4] 가상환경 생성 중...
    python -m venv %VENV_DIR%
)

:: 가상환경 활성화
call %VENV_DIR%\Scripts\activate.bat

:: 의존성 설치
echo [2/4] 의존성 확인 중...
pip install -q -r requirements.txt

:: 데이터 디렉토리 생성
if not exist "data" mkdir data

:: FastAPI 서버 실행 (백그라운드)
echo [3/4] FastAPI 서버 시작 (http://localhost:%API_PORT%) ...
start "API Server" cmd /k "call %VENV_DIR%\Scripts\activate.bat && python api_server.py"

:: 잠시 대기 (서버 기동 시간)
timeout /t 3 /nobreak > nul

:: index.html 브라우저로 열기
echo [4/4] index.html 브라우저로 열기...
start "" "%~dp0index.html"

echo.
echo  ✔  API 서버: http://localhost:%API_PORT%/api/health
echo  ✔  대시보드: index.html (브라우저에서 열림)
echo.
echo  Streamlit UI (선택): streamlit run gui/app.py --server.port %GUI_PORT%
echo.
echo  종료하려면 API Server 창을 닫으세요.
pause
