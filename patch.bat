@echo off
title BTC Grid Prediction — 패치 적용
cd /d "%~dp0"
chcp 65001 > nul 2>&1
setlocal enabledelayedexpansion

echo ====================================================
echo  BTC Grid Prediction — 패치 적용 도구
echo ====================================================
echo.

:: ── 버전 표시 ──────────────────────────────────────────
if exist VERSION (
    set /p NEW_VER=<VERSION
    echo  새 버전: v%NEW_VER%
) else (
    set NEW_VER=unknown
)
echo.
echo  [주의] 이 스크립트는 현재 폴더의 소스 파일을 덮어씁니다.
echo         data 폴더 (수집된 데이터)는 보호됩니다.
echo.
set /p CONFIRM= 계속 하시겠습니까? (Y/N):
if /i not "%CONFIRM%"=="Y" (
    echo 취소되었습니다.
    pause
    exit /b 0
)
echo.

:: ── 실행 중인 서버 종료 ──────────────────────────────────
echo [1/4] 실행 중인 서버 확인 및 종료...
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do (
    echo        PID %%p 종료 중...
    taskkill /PID %%p /F > nul 2>&1
)
timeout /t 1 /nobreak > nul

:: ── data 폴더 보호 확인 ──────────────────────────────────
echo [2/4] 데이터 폴더 보호 확인...
if not exist "data" mkdir data
echo        data\ 폴더 보호됨 (사용자 데이터 유지)

:: ── 의존성 재설치 ────────────────────────────────────────
echo [3/4] 패키지 의존성 업데이트...
set VENV_DIR=.venv
if exist "%VENV_DIR%\Scripts\activate.bat" (
    call %VENV_DIR%\Scripts\activate.bat
    python -m pip install -q -r requirements.txt
    echo        의존성 업데이트 완료
) else (
    echo        가상환경 없음 — 첫 실행 시 start.bat 이 자동 생성합니다.
)

:: ── 완료 ─────────────────────────────────────────────────
echo [4/4] 패치 완료!
echo.
echo ====================================================
if defined NEW_VER (
    echo  버전 v%NEW_VER% 업데이트 완료
) else (
    echo  업데이트 완료
)
echo  start.bat 을 실행해 서버를 다시 시작하세요.
echo ====================================================
echo.
if exist CHANGELOG.md (
    echo  변경 이력 (CHANGELOG.md 최신 항목):
    echo  ──────────────────────────
    rem 최신 버전 헤더부터 15줄 출력 (첫 줄 제목만 나오던 문제 수정)
    set /a _cl=0
    for /f "usebackq delims=" %%l in (CHANGELOG.md) do (
        set /a _cl+=1
        if !_cl! leq 16 echo  %%l
    )
)
pause
