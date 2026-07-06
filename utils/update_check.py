"""
utils/update_check.py — 자동 업데이트 확인 · 적용
=================================================
GitHub 저장소의 VERSION 파일을 조회해 로컬 버전과 비교하고,
새 버전이 있으면 최신 소스 ZIP을 받아 설치 폴더에 덮어쓴다.

- 확인: raw.githubusercontent.com/{repo}/{branch}/VERSION
- 적용: codeload.github.com/{repo}/zip/refs/heads/{branch}
- data/ 폴더(사용자 데이터)는 절대 건드리지 않음
- 네트워크 차단/저장소 비공개 시 조용히 비활성 (배지 미표시)
"""
from __future__ import annotations

import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import requests

from config import GITHUB_REPO, UPDATE_BRANCH

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
_TIMEOUT = 10

# 업데이트로 덮어쓸 대상 — make_patch.py 의 패치 구성과 동일
_SRC_DIRS = {"agents", "models", "services", "utils", "gui", "docs"}  # make_patch.py와 동일 유지
_SRC_FILES = {
    "api_server.py", "config.py", "main.py", "backtest.py", "make_pdf.py",
    "requirements.txt", "index.html",
    "start.bat", "patch.bat",
    "VERSION", "CHANGELOG.md", "README.md", "INSTALL.md", "INSTALL.txt",
    "make_patch.py",
}

# 확인 결과 캐시 (서버 재시작 전까지 30분 간격으로만 원격 조회)
_CHECK_INTERVAL = 1800
_last_check: dict | None = None
_last_check_ts: float = 0.0


def get_local_version() -> str:
    p = ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "0.0.0"


def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except ValueError:
        return (0,)


def fetch_remote_version() -> str | None:
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{UPDATE_BRANCH}/VERSION"
    try:
        r = requests.get(url, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.text.strip()
        logger.debug("[업데이트] 원격 VERSION 조회 실패: HTTP %d", r.status_code)
    except Exception as e:
        logger.debug("[업데이트] 원격 VERSION 조회 실패: %s", e)
    return None


def check_update(force: bool = False) -> dict:
    """{current, latest, available} 반환. 원격 조회 실패 시 available=False."""
    global _last_check, _last_check_ts
    now = time.time()
    if not force and _last_check is not None and now - _last_check_ts < _CHECK_INTERVAL:
        return _last_check

    current = get_local_version()
    latest = fetch_remote_version()
    result = {
        "current": current,
        "latest": latest,
        "available": bool(latest) and _ver_tuple(latest) > _ver_tuple(current),
    }
    _last_check, _last_check_ts = result, now
    if result["available"]:
        logger.info("[업데이트] 새 버전 발견: v%s → v%s", current, latest)
    return result


def _wanted(rel: Path) -> bool:
    """ZIP 내 경로(저장소 루트 기준)가 덮어쓰기 대상인지 판정."""
    parts = rel.parts
    if not parts:
        return False
    if parts[0] == "data":          # 사용자 데이터 보호 (시드 포함 — 업데이트에 불필요)
        return False
    if len(parts) == 1:
        return parts[0] in _SRC_FILES
    if parts[0] in _SRC_DIRS:
        return rel.suffix not in {".pyc", ".pyo"}
    if parts[0] == "reports":
        return rel.suffix == ".py"
    return False


def apply_update() -> dict:
    """최신 소스를 받아 덮어쓴다. 반환: {updated: n, version: str}"""
    url = f"https://codeload.github.com/{GITHUB_REPO}/zip/refs/heads/{UPDATE_BRANCH}"
    logger.info("[업데이트] 최신 소스 다운로드: %s", url)
    r = requests.get(url, timeout=120)
    r.raise_for_status()

    updated = 0
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # 최상위 'repo-branch/' 프리픽스 제거
            rel = Path(*Path(info.filename).parts[1:])
            if not _wanted(rel):
                continue
            dest = ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
            updated += 1

    new_version = get_local_version()
    logger.info("[업데이트] 파일 %d개 덮어쓰기 완료 → v%s", updated, new_version)
    return {"updated": updated, "version": new_version}


def restart_server(delay: float = 1.0) -> None:
    """start.bat 으로 새 프로세스를 띄운 뒤 현재 서버를 종료 (백그라운드)."""
    def _do():
        time.sleep(delay)
        bat = ROOT / "start.bat"
        if os.name == "nt" and bat.exists():
            logger.info("[업데이트] start.bat 재기동 후 현재 서버 종료")
            subprocess.Popen(
                ["cmd", "/c", str(bat)], cwd=str(ROOT),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            # 비 Windows (개발 환경): 동일 인터프리터로 재실행
            logger.info("[업데이트] 서버 프로세스 재실행")
            subprocess.Popen([sys.executable, str(ROOT / "api_server.py")], cwd=str(ROOT),
                             start_new_session=True)
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_do, daemon=True).start()
