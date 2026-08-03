# Legacy maintenance tools

이 디렉터리는 현재 서버·Scanner·Folderling이 import하지 않는 과거 감사 도구만 보관합니다.

- `migrate_marker_position.py`: 1.1.1 앞마커 후보를 찾는 dry-run 전용 도구입니다. `--run`은
  1.4.13부터 파일 탐색 전에 hard-fail합니다.
- `build_quarantine_cleanup_plan_1_4_4.py`: 2026-07-29 전수 감사의 고정 operation/file ID로
  당시 계획을 재현하는 read-only builder입니다. 신규 정리 계획 생성기로 사용하지 않습니다.
- `cleanup_quarantine_1_4_4.py`: 위 감사 plan의 SHA-bound 재현·검증 runner입니다. 현재 관리형
  backup, manifest, journal, root lock, Doctor를 계속 사용하고 실제 회귀 테스트도 유지하지만,
  일상 Folderling이나 신규 cleanup 진입점은 아닙니다.

현재 버전과 호환되지 않는 mutation 경로를 되살리거나 이 코드를 복사해 새 제품 경로를 만들지
마십시오. 신규 정리는 현재 관리 API에서 별도 계획과 테스트를 작성해야 합니다.
