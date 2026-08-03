# file_check 1.4.13 — legacy 실행 차단과 분권 복구 경계

## 목표

1. 현재 actual-run/journal 계약을 우회하는 구형 실실행 경로를 닫는다.
2. 분권 검토와 Folderling 자동 합류가 current 저장 분석을 같은 기준으로 사용하게 한다.
3. 강제 종료 뒤 남은 분권 staging 복사본을 증거가 완전할 때만 회수한다.
4. 이미 존재하는 회귀 테스트를 macOS/Linux CI와 정적 검사·커버리지 게이트에 연결한다.
5. unpack 부속파일 폐기와 작가 미상 허용은 사용자가 확인한 운영 정책으로 유지한다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.13`
- DB schema: `v15` 유지
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3` 유지
- bare-volume context policy: `1.4.12` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.12` 유지
- fingerprint/pair normalizer compatibility: `1.3.0` 유지
- archive object/version: `1.4.10` 유지

제목·본문·EPUB pair 의미는 바뀌지 않는다. 배포/auditor 표기만 올리며 기존 filename projection,
fingerprint와 pair cache를 재기준하지 않는다. schema migration이나 실제 house 전체 스캔도 요구하지 않는다.

## 리뷰 수용 사항

### 1. 구형 marker migration actual 실행 차단

`backend/migrate_marker_position.py`는 1.1.1 시절의 앞마커를 찾는 dry-run 감사기로만 남긴다.

- CLI `--run`과 직접 `migrate(..., dry_run=False)` 호출은 파일 walk 전에 같은 RuntimeError로 끝난다.
- regular file 삭제, symlink 교체, rename, DB/index 불일치가 생길 수 있는 과거 actual 구현은 도달할 수 없다.
- dry-run은 기존 파일과 `_최근` 링크를 바꾸지 않고 후보만 출력한다.
- 실제 제목/파일명 변경은 backup·manifest·root lock·operation journal·최종 Doctor가 있는 관리형 경로만
  사용한다.

### 2. current 파일 분석 resolver 공용화

`decision_store.resolve_current_file_analysis()`가 분권 검토와 Folderling 자동 라우팅의 공통 기준이다.

- `normalizer_version`, `analyzed_name`, size, mtime, ctime이 현재 file row와 모두 맞으면 저장 분석을
  그대로 사용한다. 따라서 현재 저장된 명시 작가가 파일명 재파싱으로 사라지지 않는다.
- 하나라도 stale이면 현재 파일명(필요한 경우 bare-volume 문맥 이름)을 메모리에서 다시 분석해 author와
  좌표를 보정한다. 이 함수는 판단만 통일하며 DB를 쓰지 않는다.
- 현재 저장 작가와 신규 파일의 명시 작가가 다르면 `author_conflict`로 멈춘다.
- 저장 분석이 stale이고 현재 파일명에는 작가가 없으면 과거 작가를 강제로 유지하지 않는다.

### 3. abandoned volume staging 회수

다음 Folderling 시작은 새 actual run을 활성화하기 전에 `txt_temp/.volume_group_staging`을 검사한다.
아래 조건을 전부 만족하는 case만 stage 복사본을 자동 제거한다.

1. staging 경로의 run ID가 상태 DB에 존재하고 `finished`/`failed`/`cancelled`임
2. 같은 run의 `planned`/`fs_done`/`db_done` operation과 operation group이 0건임
3. 모든 디렉터리 component와 파일이 no-follow 검사에서 일반 directory/file임
4. manifest의 action·run ID·stage 경로가 실제 case 경로와 정확히 일치함
5. manifest 파일 수·각 size·SHA-256이 실제 stage 복사본과 모두 일치함
6. manifest 밖의 예상하지 못한 파일이 0개임

조건이 하나라도 부족하면 어떤 stage 파일도 추측해서 지우지 않고 `needs_review` 이벤트를 남긴 뒤 새
actual run을 차단한다. 이 staging은 원본을 소비하기 전에 만든 복사본이므로 회수 과정은 house 도서를
삭제하지 않는다.

### 4. 자동 품질 게이트

`.github/workflows/ci.yml`을 추가했다.

- Ubuntu와 macOS 14에서 공개 저장소에 포함되는 `public_tests` 전체 회귀
- Ubuntu에서 production Python `pyflakes`
- 공개 회귀 기준 전체 backend coverage 측정과 `70%` 하한
- Node 22에서 frontend typecheck와 production build

기존 production Python 경고 8건은 동작을 바꾸지 않는 unused import/assignment 정리로 0건이 됐다.
실제 GitHub Actions 실행은 이 변경이 push된 뒤 확인할 수 있으며, 로컬에서는 같은 명령을 직접
검증했다. 공개 저장소에서 제외되는 `tests/`와 `extension/` 회귀는 운영 체크아웃의 로컬 전체 검증에서
추가로 실행한다.

## 사용자 확인으로 유지한 정책

### unpack 비지원 부속파일은 영구 폐기

이 항목은 결함으로 수용하지 않는다. `unpack`/`___*`는 배포 포장 wrapper이고 JPG·ZIP 등은 보존 대상이
아니다. 모든 지원 도서(TXT·EPUB·PDF)가 먼저 입고·격리되어 남지 않고 symlink·tree identity 검사를
통과한 경우에만 비지원 파일을 삭제한다. 실패·동시 변경·지원 파일 잔존 시 wrapper 전체를 보존한다.

삭제는 복구 가능한 quarantine을 만들지 않는 의도된 영구 폐기다. 대신 기존처럼 실행별 결과 이벤트와
`success.log`에 삭제 파일 수와 byte를 남긴다. 이 계약은 `public_tests/test_folderling_unpack.py`가
고정한다.

### 작가 미상은 자동 분권 묶기를 막지 않음

작가가 적히지 않은 파일이 많아 누락 자체를 review 사유로 만들지 않는다. 같은 core, 호환되는 서로 다른
본편 좌표, 좌표 중복/형태 충돌 없음, managed work 충돌 없음 등 기존 분권 안전선을 모두 통과하고,
양쪽에 실제로 명시된 작가끼리 충돌하지 않을 때 자동 묶는다.

따라서 `1권 [작가] + 2권(작가 없음)`은 계속 자동 합류한다. `1권 [A] + 2권 [B]`처럼 두 작가가
명시적으로 다를 때만 차단한다. 이 정책은 검토 큐 폭증을 피하기 위한 의도된 자동화 계약이다.

## 이번 버전에서 수용하지 않은 제안

- **실행별 plain-text log 디렉터리 추가**: 웹 서버 작업은 기존 `success.log`/`fail.log` 원문을 영속 job
  log와 구조화 event에 이미 복사하고, 파일 변경 근거는 DB journal에도 남긴다. 별도 무제한 로그
  디렉터리는 보존 기간 정책 없이 중복 누적되므로 이번 버전에는 추가하지 않는다.
- **대형 모듈 분할**: 구체적인 정확성 결함을 닫는 데 필요하지 않고 mutation 경계를 넓게 건드린다.
  current-analysis resolver만 공용화하고 전면 구조 개편은 하지 않는다.
- **모든 legacy/1.4.4 도구 이동**: 당시 SHA 고정 계획용 스크립트까지 이동하면 과거 감사 재현성이
  떨어진다. 범용으로 실행 가능하면서 안전장치를 우회한 marker actual 경로만 hard-fail했다.
- **고아 SQLite sidecar 일괄 삭제**: 복구 증거 여부를 개별 검증하지 않은 삭제는 하지 않는다.
- **별도 장시간 kill/fault-injection CI job**: 현재 recovery 합성 회귀는 전체 suite에서 계속 실행한다.
  실제 프로세스 kill 전용 job은 runner 시간과 deterministic fixture를 별도로 설계할 때 추가한다.

## 검증 결과

- 집중 회귀: `133 passed`, 후속 Folderling/index/staging 회귀 `69 passed`
- 공개 Python 회귀: `449 passed`; 공개 backend coverage `71%`, CI 하한 `70%` 통과
- 운영 체크아웃 전체 Python 회귀: `852 passed in 20.90s`
- 공개+비공개 전체 회귀 기준 backend coverage: `80%`
- frontend typecheck: 통과
- frontend production build: 통과
- backend compileall: 통과
- production Python `pyflakes backend *.py`: 0건
- `git diff --check`: 통과

검증은 합성 temp fixture와 빌드에만 파일을 썼다. 실제 상태 DB, index, house/temp 도서 이동·격리·삭제,
Folderling actual 실행은 수행하지 않았다.

## 운영 인수

- 기존 방식대로 도서 관리 서버 또는 원버튼 Folderling을 실행하면 된다.
- 이전 강제 종료의 검증 가능한 staging 잔여물은 시작 단계에서 자동 회수되고 결과 이벤트에 수량이 남는다.
- 알 수 없는 staging이 있으면 원본을 보존한 채 실행이 중단되므로 해당 경로와 manifest를 먼저 확인한다.
- `migrate_marker_position.py` dry-run 결과를 실제로 적용해야 한다면 구형 `--run`을 되살리지 말고 현재
  관리형 제목 변경 작업으로 새 계획을 만들어야 한다.
