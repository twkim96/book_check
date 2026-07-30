# file_check 1.4.7 — warm cache와 Folderling 반복 검증 최적화

## 목표

1. 1.4.5~1.4.6에서 닫은 중복 정확성·복구 계약을 유지한다.
2. warm 감사에서 pair cache hit를 다시 쓰거나 본문을 다시 읽지 않는다.
3. Folderling의 최초·변이 직전·최종 안전 검증은 보존하면서 같은 백업과 같은 Doctor를 한 실행에서
   불필요하게 반복하지 않는다.
4. 보고서 첫 페이지와 후보 0건 분권 단계의 눈에 띄는 대기 시간을 줄인다.
5. 참조 관계가 복잡한 `.dedup_state` archive는 근거 없는 자동 삭제 대신 별도 migration 과제로 남긴다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.7`
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.1` 유지
- `FINGERPRINT_VERSION`: `5` 유지
- fingerprint/pair policy compatibility: `1.4.2` 유지
- fingerprint/pair normalizer compatibility token: `1.3.0` 유지
- local Chrome extension manifest: `2.5` 유지

이번 패치는 파일명·본문 정규화·pair 분류 의미를 바꾸지 않는다. cache 실행 방식만 바꾸므로 대규모
fingerprint sweep이나 pair generation을 새로 만들지 않는다.

## 리뷰 수용 판단

### 수용: pair cache hit 무기록과 pure-plan 재사용

기존 `store_pair_results()`는 이미 읽은 hit까지 다시 stat한 뒤 `INSERT OR REPLACE`하고, 각 pair의 open
review를 다시 조회했다. 1.4.7은 다음처럼 분리한다.

- cache hit: pair row와 `created_at`을 보존하고 writer transaction을 열지 않음
- cache miss: 현재 identity를 재검증한 stable 결과만 새 row로 저장
- open review가 현재 classification/evidence와 같음: bulk snapshot 한 번으로 확인하고 생략
- open review가 없거나 뒤처짐: 기존 1.4.6의 선별 refresh/recreate 경로 유지

`cache_write=False`도 read-only pair reader를 끝까지 유지한다. current fingerprints를 가진 pair는 DB
bytes를 바꾸지 않고 pair cache hit로 끝나며 본문 read가 0이다.

### 수용: fingerprint anchor 지연 로드

bulk preload의 `SELECT fp.*`를 current fingerprint의 identity, SHA, normalized length, encoding, status,
lossy/error metadata로 제한했다. 큰 front/tail anchor는 pair cache miss이고 normalized SHA도 다른 TXT가
정밀 비교로 진행할 때만 읽는다. 통계에 `fingerprint_detail_loads/chars`를 남긴다.

### 수용: run-scoped validation receipt

원버튼 실행은 공용 house/temp mutation lock 안에서 preflight full Doctor를 수행한 뒤 actual run token을
발급한다. 같은 프로세스에서만 전달되는 opaque receipt는 state DB·house/temp root·승인 run ID에
묶이며, 이 셋이 현재 실행과 정확히 같을 때만 readiness의 중복 Doctor를 생략한다. 같은 receipt는 최초
snapshot projection의 중복 Doctor에도 한정해 사용하고 한 번 가져오면 registry에서 소비한다.

다음 검증은 생략하지 않는다.

- approval/active state, root, unfinished operation, schema 및 backup evidence
- snapshot의 전체 지원 파일 walk와 각 파일 identity/file_analysis 일치
- dedup mutation 직전 full Doctor와 current SHA/identity
- 최종 index projection 검증
- actual run 종료 직전 final full Doctor

승인 백업은 첫 SHA-256 + `PRAGMA integrity_check` 성공 시 `(경로, dev, ino, ctime, size, mtime, SHA)`
process-local receipt를 최대 32개 보관한다. identity가 같을 때만 재사용하고, 달라지면 hash/integrity를 다시 검사한다.
정적 백업 DB는 `mode=ro&immutable=1`로 열어 불필요한 sidecar 생성을 피한다. 결과에는 preflight,
activation, one-button total 시간을 포함한다.

### 수용: 기본 보고서 pagination과 분권 후보 0건 fast path

보고서 기본 목록은 모든 TXT/JSON summary를 읽은 뒤 자르지 않는다. 파일명에서 kind/생성시각을 얻어
정렬·페이지를 먼저 계산하고 요청 페이지의 summary만 읽는다. JSON-only dedup 보고서는 앞쪽 128KiB의
top-level `kind/summary`만 decode하고 수 MB 결과 배열은 읽지 않는다. summary 검색은 모든 후보를
확인하지만 각 TXT/JSON의 제한된 앞부분만 읽는다.

분권 자동 단계는 최초 분석에서 `auto_ready=0`이면 active actual run만 확인한 뒤 그 summary를 바로
반환한다. cache invalidation과 동일 분석 재실행은 하지 않는다.

### 부분 수용: inventory 공유

이번 패치는 fingerprint bulk row와 anchor memory를 줄였고, 원버튼의 중복 snapshot Doctor를 없앴다.
Scanner inventory 객체를 auditor·manifest·최종 projection 전체에 공유하는 변경은 실행 중 파일 변화의
무효화 규칙과 mutation target별 재검증 경계를 함께 설계해야 한다. 현재 전체 walk와 mutation 대상의
개별 SHA/identity 검증은 보존한다.

### 별도 migration으로 보류: `.dedup_state` archive와 자동 purge

과거 fingerprint, pair cache, backup, manifest, report는 decision/review/operation/actual-run evidence가
참조한다. 현재 schema의 immutable/RESTRICT 계약을 무시하고 크기만으로 삭제하면 복구·감사 근거가
끊어진다. 다음 항목을 갖춘 별도 migration 전에는 자동 삭제하지 않는다.

- hot DB와 압축 archive의 stable ID/조회 계약
- anchor blob content-addressed dedup 및 복원 검증
- pair policy generation archive와 decision/review reference 유지
- backup byte+time tier, active/unfinished run 보호
- 완료 보고서 summary index/sidecar와 active report 보호
- orphan WAL/SHM의 소유 DB·process 확인 후 정리

## 회귀 검증

- warm pair row `created_at` 보존 및 `pair_cache_writes=0`
- 손상된 open review는 같은 fingerprint pair cache hit로 선별 refresh
- read-only pure plan: pair hit, 본문 read 0, DB bytes 불변, anchor detail load 0
- backup receipt: unchanged는 hash 1회, identity 변경은 즉시 SHA mismatch 재검출
- opaque receipt의 DB/root/run exact match에서만 readiness Doctor 생략
- 보고서 25건 중 limit 5 요청 시 summary read 5건
- 분권 후보 0건에서 분석 1회, cache invalidation 0회

## 최종 검증

- 전체 Python 회귀: `790 passed in 20.22s`
- frontend: `file-check-library-ui@1.4.7`, `npm run build` 통과
- Python/Chrome normalizer parity: `version=1.3.1`, 35 cases 통과
- `compileall backend public_tests`: 통과
- `git diff --check`: 통과

운영 DB를 쓰지 않는 `cache_write=False`, `house-only` warm 감사 결과:

- house 17,612파일 / 후보 3,282쌍
- fingerprint hit 17,579 / pair hit 3,244 / pair miss 0
- 본문 read 0 / anchor detail load 0 / stop reason 0 / `completed=true`
- auditor 9.333초, process wall 9.73초
- 비교 기준 1.4.6 warm 11.391초 대비 약 18% 단축

운영 `dedup_logs` 126건에서 기본 50건 목록을 읽은 결과:

- listing 0.043초 / process wall 0.09초
- maximum resident set 16,760,832 bytes
- 리뷰 기준 0.227초·약 188MB RSS에서 큰 JSON 전체 parse가 제거됨

위 운영 검증은 report를 쓰지 않는 read-only 경로이며 도서 이동·이름 변경·격리·삭제는 0건이다.
