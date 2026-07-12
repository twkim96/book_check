#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

export PYTHONDONTWRITEBYTECODE=1
# 새 파이프라인 dry-run:
# --rescan        : 인덱스를 항상 새로 만들어 stale 데이터 영향 차단
# --include-temp  : temp 하위 폴더까지 검토 큐 후보로 포함
# --audit-suspects: 본문 auditor와 managed 대표 완전 비교까지 포함
# --move-suspects : 검토 큐 이동 동작도 미리보기 (실제 이동은 dry-run이라 발생 안 함)
python3 deduplicator.py --dry-run --rescan --include-temp --audit-suspects --move-suspects

echo
echo "Done. Press any key to close."
read -k 1
