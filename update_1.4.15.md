# file_check 1.4.15 — 플랫폼 경로·stale override·staging recovery 보강

## 목표

1. macOS의 안정적 `/tmp`·`/var` 별칭을 Linux 경로에 잘못 적용하지 않는다.
2. stale 파일 분석에서도 사람이 확정한 제목 override를 보존한다.
3. 분권 staging 복구가 검증한 manifest와 실제 cleanup 대상을 같은 evidence로 묶는다.
4. 검증 후 디렉터리 drift나 제거 실패가 있으면 recovery 성공으로 보고하지 않는다.
5. NFC DB 경로 키와 실제 Unicode 파일명 접근을 분리해 Linux에서도 NFD fixture를 안전하게 처리한다.
6. 컨트롤서버가 앱별 비밀 환경을 전달하지 않아도 owner-only Google Sheet 설정을 사용한다.

## 리뷰 수용 결과

### 수용: Darwin 전용 경로 별칭

`mutation_io._canonical_absolute()`의 `/tmp → /private/tmp`, `/var → /private/var` 변환을
`sys.platform == "darwin"` 안으로 제한했다. Linux에서는 lexical absolute path를 그대로 사용한다.
no-follow component walk와 macOS `/var`·`/private/var` 동일성 계약은 유지한다.

후속 Ubuntu CI에서 드러난 Unicode 경로 문제도 같은 플랫폼 경계에서 수정했다. DB의
`canonical_path`는 계속 NFC로 정규화하되, `reconcile_file_metadata()`와
`upsert_file_analysis()`의 파일 I/O는 호출자가 전달한 실제 경로 표기를 사용한다. 따라서 macOS의
NFD 파일을 나타내는 합성 fixture도 Linux에서 존재하지 않는 NFC pathname으로 바꿔 읽지 않는다.

### 후속: Google Sheet private 설정

컨트롤서버 1.5.x의 공개 배포 경계는 file_check 자격증명을 LaunchAgent plist에 복제하지 않는다.
이 보안 경계를 되돌리는 대신 file_check가 `~/.config/book_check/google-sheet.json`을 직접 읽는
대체 경로를 추가했다.

- 파일은 현재 사용자 소유의 일반 파일이며 권한 `0600`이어야 한다.
- `credentials_path`와 `spreadsheet_id`를 한 쌍으로 읽고 인증 파일 실재 여부를 확인한다.
- 기존 환경변수 두 개가 모두 있으면 계속 최우선으로 사용한다.
- 환경변수가 한쪽만 있으면 local config와 섞지 않고 fail-closed 한다.
- UI readiness와 실제 Sheet writer가 같은 resolver를 사용한다.

### 수용: stale 수동 제목 override

`resolve_current_file_analysis()`의 stale 경로도 `_effective_file_analysis()`를 사용한다.

- `title_override_json`이 있는 저장 행: core/readable/catalog 제목과 override 증거 보존
- author, max/effective number, unit, complete, disambig, 권·회차 좌표: 현재 파일명으로 재분석

`volume_review`와 Folderling 분권 라우팅 query에는 `catalog_query_title`, `title_override_json`을
추가했다. 기존 house 후보는 저장 core 일치 행과 identity-stale 행을 함께 불러온 뒤 공용 resolver의
최종 core가 일치하는 경우만 사용한다.

### 수용: staging evidence binding과 최종 제거 확인

- `read_json_with_evidence()`에 선택적 크기 상한을 추가했다.
- stage manifest는 같은 no-follow FD에서 identity·SHA와 JSON payload를 함께 얻는다.
- recovery cleanup은 검증한 manifest evidence와 예상 directory entry 집합을 전달받는다.
- manifest 교체·내용 변경, stage 파일 drift, 늦은 entry 유입을 known file 삭제 전에 차단한다.
- case root가 실제로 제거되지 않거나 run/root가 비지 않으면 recovered count에 반영하지 않고 issue를
  남긴다.

## 부분 수용·보류: import 호환성

`python -c "import backend.decision_store"`가 공식 실행 topology가 아니라는 지적 자체는 맞다. 그러나
단순 relative-import fallback은 top-level `decision_store`와 `backend.decision_store`를 동시에 만들 수
있고, process-local mutation lock·actual-run receipt·recovery registry identity를 갈라 더 위험하다.

현재 서버, Scanner, Folderling, CLI와 tests는 모두 같은 top-level backend module identity를 공유한다.
따라서 이번 버전에서는 package import를 추가하지 않고 이 계약을 README에 명시한다. 향후 필요하다면
모든 entry point와 compatibility loader가 하나의 `sys.modules` identity로 수렴하는 별도 변경으로 다룬다.

반면 1.4.14의 제한적 `decision_store.__all__` 때문에 과거 star import에서 보이던 actual-run,
operation journal, recovery, Doctor 함수가 빠진 문제는 별도 module identity를 만들지 않고 해결할 수
있다. `__all__`을 제거해 1.4.13 이전의 top-level star import 범위를 복원했다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.15`
- DB schema: `v15` 유지
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3` 유지
- bare-volume context policy: `1.4.12` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.12` 유지
- archive object/version: `1.4.10` 유지

schema migration, filename projection 재기준, fingerprint/pair cache 무효화, house 전체 스캔은
요구하지 않는다.

## 검증 결과

- 핵심 회귀 66건
  - Linux `/tmp`·`/var` 비변환과 Darwin alias 변환
  - JSON evidence byte limit
  - stale title literal/structure override와 stored-author 분리
  - stored core 양방향 drift의 최종 resolver 필터
  - manifest pathname 교체, 늦은 파일 유입, stage drift, rmdir 실패
- 공개 회귀: `468 passed`, backend coverage `72%` (`fail-under=70` 통과)
- 전체 회귀: `871 passed`, backend coverage `81%` (`fail-under=75` 통과)
- frontend TypeScript typecheck와 production build: 통과
- backend/tools compileall, pyflakes, Python/Chrome normalizer parity 35건,
  `git diff --check`: 통과

실제 Ubuntu GitHub Actions는 첫 실행에서 NFD 물리 경로를 NFC DB 키로 `stat`한 경계를 찾아냈고,
파일 I/O와 식별 키를 분리한 후 재실행하는 배포 gate로 확인한다. 로컬에서는 platform 값을
Linux/Darwin으로 각각 고정한 회귀로 경로 변환 자체도 검증했다.

Google 후속 적용에서는 private 설정을 `0600`으로 설치한 뒤 다음 운영 증거를 확인했다.

- Google API 읽기 인증과 대상 Spreadsheet 접근 성공, 기존 두 탭 확인
- PM2 관리 `book_check`만 재시작하고 `/health`의 `version=1.4.15` 확인
- `/api/services`의 Google Sheet가 `configured=true`, `ready=true`, blocker 없음
- 실제 Sheet 쓰기와 도서 DB·house/temp 변경은 수행하지 않음

검증은 합성 temp fixture와 빌드만 사용했다. 실제 상태 DB, index, house/temp 파일 이동·격리·삭제,
Folderling actual 실행은 수행하지 않았다.
