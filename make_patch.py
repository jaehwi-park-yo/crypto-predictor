"""
make_patch.py — 배포자용 패치 ZIP 생성 스크립트
=================================================
실행:
    python make_patch.py              # btc-grid-patch-vX.Y.Z.zip 생성
    python make_patch.py --out my.zip

생성되는 ZIP 구조 (설치 폴더에 그대로 덮어쓰면 업데이트 완료):
    patch.bat / patch.sh   — 사용자용 원클릭 패치 스크립트
    VERSION                — 버전 파일
    CHANGELOG.md           — 변경 이력
    *.py, *.html 등        — 소스 코드 (data/, .venv/ 제외)
    data/dataset_seed.zip  — 데이터셋 시드 (있을 때만)

제외 항목 (사용자 데이터 보호):
    data/*.json, data/*.db, data/*.db-wal, data/*.db-shm
    data/*.imported
    .venv/, __pycache__/, .git/, .claude/
    reports/*.txt, reports/*.csv, reports/*.html
    *.pdf
"""
from __future__ import annotations

import argparse
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent

# 패치에 포함할 최상위 Python 패키지/모듈 디렉토리
SRC_DIRS = ["agents", "models", "services", "utils", "gui"]
SRC_FILES = [
    "api_server.py", "config.py", "main.py", "backtest.py", "make_pdf.py",
    "requirements.txt", "index.html",
    "start.bat", "start.sh",
    "VERSION", "CHANGELOG.md", "README.md", "INSTALL.md", "INSTALL.txt",
    "make_patch.py",
    "patch.bat", "patch.sh",
]

# data/ 내에서 패치에 포함할 파일 (시드 ZIP만)
SEED_PATH = ROOT / "data" / "dataset_seed.zip"

# 제외 패턴
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", ".claude",
                ".idea", ".vscode", "node_modules"}


def _add_dir(zf: zipfile.ZipFile, src: Path, base: Path) -> int:
    count = 0
    for p in sorted(src.rglob("*")):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIXES:
            continue
        if p.is_file():
            arcname = p.relative_to(base)
            zf.write(p, arcname)
            count += 1
    return count


def build_patch(out_path: Path) -> None:
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "unknown"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        total = 0

        # 소스 파일
        for name in SRC_FILES:
            p = ROOT / name
            if p.exists():
                zf.write(p, name)
                total += 1

        # 소스 디렉토리
        for d in SRC_DIRS:
            src = ROOT / d
            if src.exists():
                total += _add_dir(zf, src, ROOT)

        # 데이터셋 시드 (있을 때만)
        if SEED_PATH.exists():
            zf.write(SEED_PATH, "data/dataset_seed.zip")
            total += 1
            print(f"  [포함] data/dataset_seed.zip ({SEED_PATH.stat().st_size / 1e6:.1f} MB)")

        # reports 폴더: 분석 스크립트(.py)만, 결과물(.txt/.csv/.html/.pdf) 제외
        reports = ROOT / "reports"
        if reports.exists():
            for p in sorted(reports.glob("*.py")):
                zf.write(p, p.relative_to(ROOT))
                total += 1
            keep = ROOT / "reports" / ".gitkeep"
            if keep.exists():
                zf.write(keep, keep.relative_to(ROOT))

    size_mb = out_path.stat().st_size / 1e6
    print(f"\n✅  패치 ZIP 생성 완료")
    print(f"   파일: {out_path}")
    print(f"   크기: {size_mb:.1f} MB | 항목: {total}개 | 버전: v{version}")
    print(f"\n배포 방법:")
    print(f"  1. {out_path.name} 를 사용자에게 전달")
    print(f"  2. 사용자: ZIP을 설치 폴더에 압축 해제 후 patch.bat (Win) / patch.sh (Mac/Linux) 실행")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="패치 ZIP 생성")
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "0.0.0"
    default_out = ROOT / f"btc-grid-patch-v{version}.zip"
    ap.add_argument("--out", default=str(default_out))
    args = ap.parse_args()
    build_patch(Path(args.out))
