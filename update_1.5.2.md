# file_check 1.5.2 — migration/recovery fail-closed hardening

기준:

- 변경 전 코드: `556b3f3` (`1.5.1`, branch `codex/folderling-stale-identity-recovery`)
- 서버/UI: `1.5.1` → `1.5.2`
- SQLite schema: `v19` 유지
- payload codec, fingerprint ID, normalizer/fingerprint/pair/auditor/archive 정책 유지

## 추가 리뷰 판정

| finding | 판정 | 1.5.2 조치 |
| --- | --- | --- |
| H-1 routine initializer의 migration 우회 | 수용 | payload migration 인자·공개 facade 제거, v18 evidence 강제 |
| H-2 resume 시 stale backup과 blocker 미재검사 | 수용 | 매 pre-commit writer epoch마다 전체 preflight 및 fresh backup |
| H-3 active gate가 routine entrypoint를 막지 못함 | 수용 | schema/transaction/server/Folderling/platform 중앙 차단 |
| M-1 reported journal을 현재 DB보다 먼저 신뢰 | 수용 | DB migration ID/marker/report SHA 교차검증, rollback `--reapply` |
| M-2 source FK/Doctor를 commit 뒤 발견 | 수용 | backup 전 integrity/FK/full operational Doctor |
| M-3 v17 evidence retention 누락 | 수용 | settings와 legacy filename 기반 retention/archive 보호 |
| M-4 운영 migration source provenance 부족 | 수용 | clean build 기본, commit/dirty/source SHA/Python/SQLite 기록 |
| DB별 journal 충돌 | 수용 | resolved DB path hash를 journal 이름에 포함 |
| 구버전 안내가 v17/v18 CLI를 과대 안내 | 수용 | schema 1–16 staged upgrade와 17/18 payload CLI를 구분 |
| report 필드 교차검증 부족 | 수용 | migration ID, digest, backup/evidence SHA까지 journal과 비교 |
| WAL checkpoint busy 미검사 | 수용 | checkpoint 첫 반환값이 busy이면 fail-closed |

현재 운영 DB 손상이나 즉시 rollback 주장은 받아들이지 않았다. 2026-08-28 read-only 확인 결과는 schema v19,
migration gate 없음, integrity `ok`, FK 문제 0이며 서비스는 1.5.1 exact commit `556b3f3`, clean build로
정상 동작 중이다. 1.5.2는 정상 payload 표현을 재작성하지 않고 future migration/recovery 경계만 강화한다.

## 핵심 계약

### 유일한 migration entrypoint

`decision_store.initialize_state_db()`는 `migrate`, `check_integrity`만 받는다. payload 전용 migration capability를
호출자에게 노출하지 않으며 schema v17/v18이면 항상 전용 CLI를 안내하고 종료한다. 내부 v18 변환 함수도
verified schema-v17 fingerprint ID/hash mapping 없이는 transaction을 진행하지 않는다.

### resume writer epoch

pre-commit failure 뒤 재실행은 journal의 fingerprint count/digest만으로 rollback backup을 재사용하지 않는다.
새 `BEGIN IMMEDIATE`에서 다음을 모두 다시 확인한다.

1. source schema와 전체 integrity/FK
2. active/approved actual run
3. `planned/fs_done/db_done` operation 및 operation group
4. full operational Doctor와 실제 file identity
5. main/WAL/journal/logical page extent 및 free space
6. clean build 또는 명시된 dirty override provenance

이 epoch에서 현재 DB 전체를 fresh SQLite backup으로 만들고 fsync/검증한 뒤에만 logical migration을 수행한다.

### gate와 completion marker

`fingerprint_payload_migration_gate`가 존재하면 다음이 모두 실패한다.

- `validate_schema()` 기본 경로
- `decision_store.transaction()`
- 새 library server startup
- Folderling backup/actual-run 생성 전 preflight
- platform catalog write preflight

이미 실행 중인 server의 health는 maintenance/active/503을 반환한다. migration 내부 acceptance만
`allow_active_migration_gate=True`를 명시할 수 있다.

logical commit은 DB settings에 migration ID, source schema, source snapshot SHA, rollback과 legacy evidence,
build/source provenance를 같은 transaction으로 기록한다. atomic report 이후 report path/SHA와 `reported` marker를
DB에 commit한 뒤 gate를 지운다. cached report는 journal, report, DB marker와 현재 fingerprint digest가 모두
일치할 때만 반환한다.

### artifact retention

새 migration은 rollback과 legacy evidence path/SHA를 모두 settings에 기록한다. 이미 1.5.0/1.5.1에서 생성돼
legacy setting이 없는 설치도 `before_fingerprint_payload_v18_*.sqlite3`와
`before_fingerprint_payload_v19_*.sqlite3`를 retention 및 cold archive에서 제외한다.

## fault regression 범위

- 공개 initializer migration capability 부재와 v18 evidence 강제
- backup fsync crash 뒤 중간 settings write가 fresh rollback backup에 포함됨
- backup fsync crash 뒤 새 approved run이 resume을 차단함
- active gate가 schema validation, transaction, new server, Folderling, platform을 side effect 전에 차단함
- 이미 실행 중인 server health가 maintenance/active/503을 반환함
- reported journal 뒤 v17 rollback은 기본 재사용 거부, `--reapply`에서 stale journal 보존 후 새 migration ID 사용
- source FK violation은 backup/logical commit 전에 차단됨
- dirty source 기본 차단과 `--allow-dirty` provenance 기록
- report와 journal checksum을 함께 조작해도 migration ID 교차검증 실패
- DB별 journal path 분리와 checkpoint busy 차단
- migration artifact retention 및 cold archive 제외

## 검증 결과

- 신규 1.5.2 migration/gate/provenance fault regression: **11 passed**
- 1.5.1/1.5.2 migration fault suite 합계: **17 passed**
- 전체 Python 회귀: **1067 passed**, urllib3/LibreSSL 환경 warning 1건
- Python `compileall`, 변경 파일 `pyflakes`, `git diff --check`: 통과
- frontend `typecheck`와 production build: 통과 (`file-check-library-ui@1.5.2`)
- 운영 DB read-only 1.5.2 validator/Doctor:
  - schema `19`, fingerprints `84,417`, gate 없음, Doctor issue `0`
  - objects `14,908`, refs `65,901`
  - v17 evidence와 v18 rollback backup 모두 retention protected
  - v17 evidence SHA-256
    `da8b1fc429ff7794423169f766b0e2128e27d11b7d103a1c626b152af51c1229`
  - v18 rollback SHA-256
    `90f89279110264347ac29c4e776f2b8e85aa20c81d009b336861cfb8079919c0`

운영 DB에는 migration이나 data write를 다시 수행하지 않았다. 현재 실행 서비스도 검증 중인 uncommitted
1.5.2 source로 재시작하지 않고, 기존 1.5.1 exact commit `556b3f3`, `build_dirty=false`, schema v19 상태를
유지했다. 1.5.2 release commit 이후 재시작할 때 새 build SHA/clean 상태를 별도 acceptance한다.
