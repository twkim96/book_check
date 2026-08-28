# file_check 1.5.3 — review lifecycle and alternate-format hardening

기준:

- 변경 전 코드: `65e2c54` (`1.5.2`, branch `codex/folderling-stale-identity-recovery`)
- 서버/UI/auditor: `1.5.2` / `1.4.17` → `1.5.3`
- SQLite schema: `v19` 유지, DB migration 없음
- fingerprint version/policy와 payload codec: `5` / `1.4.2` 그대로 유지
- pair policy: `1.4.16-lossless-legacy-v3` → `1.5.3-review-lifecycle-v1`

## 운영 감사에서 확인한 원인

2026-08-28 Folderling 실행의 큰 수치는 대규모 오격리가 아니었다. 초기 결과의 raw-SHA exact 243건,
정규화 본문 동일 6건, 95% ordered-body 46건은 현재 본문 증거가 있었고, 격리 폴더 전수 감사에서도
house 복원 후보는 없었다. 반면 사람이 보는 open review에는 과거 `metadata_only`, cross-core
`decode_lossy`, 동일 파일쌍의 약한 과거 classification이 누적되어 실제 이동보다 검토 대상이 크게
보이는 문제가 있었다.

Scanner fallback은 이 누적 상태를 한 번에 노출한 계기였고 판정 의미를 바꾼 원인은 아니다. 따라서
1.5.3은 자동 격리 범위를 넓히거나 본문 threshold를 낮추지 않고 review lifecycle만 정리한다.

## 1.5.3 계약

### coverage-independent review reconciliation

완료된 writable auditor는 다음 open row만 `superseded`로 전환한다.

1. 활성 endpoint의 현재 fingerprint와 더 이상 일치하지 않는 unqueued row
2. endpoint가 비활성화된 unqueued row
3. 서로 다른 명시 권차·분할 구간, side story/numbered volume, 다른 core의 `metadata_only`
4. 서로 다른 core의 `decode_lossy`
5. 같은 unordered file pair에 여러 row가 있을 때 current fingerprint, physical queue, 분류 강도,
   최신순으로 선택한 한 건보다 약한 unqueued row

감사에서 후보로 보이지 않았다는 사실만으로 row를 닫지 않는다. 따라서 `coverage_limited=true`에서도
이번 bounded 감사가 방문하지 않은 현재 actionable review는 그대로 남는다. 실제 queue path가 있는 row도
파일 disposition 없이 자동 종료하지 않는다. 종료된 row의 기존 evidence는 삭제하지 않고 suppression
reason, policy version, 유지한 review ID/classification을 덧붙인다.

### review tier 축소

- cross-core `decode_lossy`는 제목 유사도와 읽기 실패만 있는 진단 정보이므로 새 pending review를 만들지
  않는다. 같은 core의 decode 실패는 계속 사람 검토로 남긴다.
- 현재 파서가 서로 다른 명시 EPUB 좌표를 증명한 `metadata_only`는 actionable review를 만들지 않는다.
  강한 EPUB 본문/reading payload 증거는 이 억제보다 먼저 평가되므로 영향을 받지 않는다.

### PDF/EPUB alternate format

같은 작품·같은 권 좌표라도 기존 파일과 신규 파일이 PDF/EPUB로 서로 다른 경우에는 동일 파일 충돌이
아니라 alternate format으로 기존 작품 폴더에 입고한다. 다음은 계속 warning conflict다.

- 같은 확장자의 동일 좌표
- 동일 좌표 기존 항목이 둘 이상인 경우
- PDF/EPUB 이외 형식
- 작가, work, parent folder 또는 관리 관계가 모호한 경우

입고된 두 형식은 같은 work의 서로 다른 variant/representative로 보존하며 어느 쪽도 삭제하지 않는다.

### EPUB package variant evidence

기존 framed spine digest와 함께 spine item 경계를 제외한 연속 visible-text digest를 계산한다. 같은 선언
좌표, 최소 50,000자, 같은 연속 본문 길이/hash, 겹치는 안정 package identifier를 모두 만족하지만 framed
digest가 다르면 `epub_package_variant`로 기록한다. 빈 cover/nav spine이나 chapter split 차이를 동일판
패키지 변형으로 설명할 수 있지만 이미지·navigation 선호는 자동 결정하지 않는다.

`epub_package_variant`는 pending manual review일 뿐 strong class가 아니며 자동 격리나 “큰 파일 우선”을
허용하지 않는다. 신규 temp package는 기존 house 사본을 유지한 채 warning에 보류하고, 이미 양쪽이
house인 경우에는 파일을 이동하지 않고 DB review만 남긴다.

## 유지하는 안전선

- ordered-body 95%, 최소 100,000자, 최대 누락 구간 제한 유지
- protected/representative/서로 다른 managed variant 자동 격리 차단 유지
- `contained_version`, `metadata_only`, package variant를 자동 중복으로 승격하지 않음
- fingerprint payload와 schema v19를 재작성하지 않음

## 검증 결과

- review diagnostic/stale/cross-class reconciliation, PDF/EPUB 동일 권 alternate-format 실제 journal 입고,
  EPUB spine boundary-only package-variant 집중 회귀: **146 passed**
- mutation/queue/index/house-cleanup 확대 회귀: **126 passed**
- 전체 Python 회귀: **1,074 passed**, urllib3/LibreSSL 환경 warning 1건
- 변경 Python `py_compile`/`pyflakes`, `git diff --check`: 통과
- frontend `typecheck`와 production build: 통과 (`file-check-library-ui@1.5.3`)
- 운영 DB read-only 확인:
  - schema `19`, integrity `ok`, FK 문제 0, Doctor issue 0
  - open actionable review 50건 유지
  - 1.5.3 reconciliation dry-run의 noise/stale/cross-class 종료 계획 0건

운영 DB에는 review cleanup이나 파일 이동을 적용하지 않았고 실행 중인 1.5.2 서버도 재시작하지 않았다.
1.5.3 release commit 뒤 재시작할 때 `/health version`, `build_commit`, `build_dirty=false`, schema v19를
별도로 acceptance한다.
