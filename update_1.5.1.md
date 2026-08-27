# file_check 1.5.1 — fingerprint payload production-safety hardening

기준:

- 변경 전 코드: `db78cc3` (`1.5.0`, branch `codex/folderling-stale-identity-recovery`)
- 서버/UI: `1.5.0` → `1.5.1`
- SQLite schema: `v18` → `v19`
- content-addressed `zlib-6-v1` object와 immutable fingerprint ID는 유지

## 코드 리뷰 수용 판정

| finding | 판정 | 1.5.1 조치 |
| --- | --- | --- |
| H-1 ref completeness 증거 부재 | 수용 | fingerprint 기대 state/hash 영속화, 양방향 validator/loader |
| H-2 migration 중 old writer 경쟁 | 수용 | 연속 `BEGIN IMMEDIATE`, v19 writer UDF trigger, post-commit writer gate |
| H-3 routine entrypoint가 전용 acceptance 우회 | 수용 | server/Folderling/platform 자동 migration 제거 |
| M-1 commit 뒤 VACUUM/report 재개 불가 | 수용 | fsync journal과 phase별 idempotent resume |
| M-2 WAL 미포함 공간 계산·반복 backup | 수용 | physical/logical extent preflight와 deterministic partial/final backup |
| M-3 main/tag canonical release 부재 | 부분 수용 | health build SHA/dirty 공개; main merge/tag는 별도 release 작업으로 남김 |

현재 v18 운영 DB의 즉시 rollback 권고는 받아들이지 않았다. 적용 전 read-only 교차검사에서 보존된 v17
backup의 non-empty fingerprint ID 65,901개와 live ref/hash가 전부 일치했고, legacy row, active run,
unfinished operation/group, integrity/FK/Doctor issue가 모두 0이었기 때문이다.

## schema v19 불변식

`fingerprints`에 다음 immutable 기대 증거를 추가한다.

- `anchor_payload_state`: `none` 또는 `present`
- `anchor_payload_hash`: `present`일 때 canonical framed bytes의 lowercase SHA-256, `none`일 때 `NULL`

정상 상태는 정확히 두 가지다.

```text
none    + expected hash NULL + ref 없음
present + expected hash H    + 같은 fingerprint_id/ref hash H + object H 존재
```

다음은 모두 corruption이다.

- `present`인데 ref 없음
- `none`인데 ref 존재
- ref hash와 fingerprint expected hash 불일치
- ref가 가리키는 object 누락
- 참조되지 않는 payload object
- v19 fingerprint의 legacy `front_anchor`/`tail_anchor`가 `NULL`이 아님
- object codec, bounded decompression, raw length, checksum, frame boundary 또는 UTF-8 불일치

expected hash는 ref와 별개인 immutable fingerprint row에 있으므로, 여러 fingerprint가 object 하나를
공유하더라도 한 fingerprint의 ref만 유실되거나 다른 정상 object로 바뀐 상태를 증명할 수 있다.

## writer 계약과 구형 process 차단

신규 분석 writer는 같은 transaction에서 다음 순서를 지킨다.

1. canonical front/tail bytes와 hash 계산
2. object `INSERT ... ON CONFLICT DO NOTHING` 후 decode/hash 재검증
3. fingerprint INSERT에 `anchor_payload_state/hash` 고정
4. 정확히 같은 hash의 ref INSERT
5. `files.current_fingerprint_id` 전환

recovery/review clone은 source fingerprint의 expected state/hash를 SELECT/INSERT로 복제한 뒤 ref를 공유한다.
raw-only/empty 결과는 `none/NULL`을 명시한다.

`fingerprints_insert_storage_guard`는 INSERT connection에 `file_check_writer_schema_version()` 함수가 있고 그
값이 19인지 검사한다. 1.5.0 이하에서 이미 열린 connection에는 함수가 없으므로 schema 변경을 뒤늦게
발견하더라도 INSERT가 실패한다. 같은 trigger는 legacy TEXT, 잘못된 state/hash 조합, migration gate가
active인 동안의 INSERT도 막는다. ref INSERT trigger는 fingerprint의 expected hash와 exact match만 허용한다.

## authoritative migration과 resume journal

routine entrypoint는 기존 DB의 schema가 v19가 아니면 다음과 같이 종료한다.

```text
library server / Folderling / platform catalog
→ automatic migration 거부
→ migrate_fingerprint_payloads.py --run 안내
```

전용 CLI만 다음 acceptance를 수행한다.

1. house/temp mutation lock
2. `BEGIN IMMEDIATE`를 plan 전부터 logical commit까지 계속 유지
3. active/approved run, unfinished operation/group 차단
4. main + WAL + rollback journal과 logical page extent 계산
5. backup + migration WAL + VACUUM + 256 MiB free-space preflight
6. partial backup write, integrity/count/metadata digest, file fsync, atomic rename, directory fsync
7. schema v17 legacy anchors 또는 지정한 v17 backup의 ID별 expected hash 계산
8. v18 current ref와 v17 expected mapping을 fingerprint ID별 비교
9. state/hash backfill, legacy clear, strict triggers, schema v19를 한 transaction으로 commit
10. fingerprint writer gate를 유지한 채 checkpoint/VACUUM/full validation
11. count, anchor 제외 metadata digest, payload/FK/completeness 재검증
12. report atomic write 후 gate 해제와 journal `reported`

journal 경로는 DB마다 하나다.

```text
.dedup_state/reports/fingerprint_payload_migration_1_5_1_journal.json
```

phase는 `preparing`, `prepared`, `logical_committed`, `compacted`, `verified`, `reported` 순서다. backup 중
ENOSPC이면 source transaction을 rollback하고 `preparing`에 머문다. logical commit 직후 process가 종료되면
DB는 v19와 active writer gate를 유지하고, 재실행이 같은 backup SHA와 pre-count/digest로 compaction,
validation, report를 계속한 뒤 gate를 해제한다. report 완료 후 같은 명령은 기존 결과를 반환한다.

## v18 → v19 명령

보존된 schema-v17 backup이 필수다. filename의 `before_fingerprint_payload_v18`은 “v18 적용 전”이라는
뜻이며 실제 backup schema는 v17이다.

```bash
EVIDENCE=.dedup_state/backups/before_fingerprint_payload_v18_<timestamp>_<id>.sqlite3

PYTHONPATH=backend python3 backend/migrate_fingerprint_payloads.py \
  --state-db .dedup_state/dedup_decisions.sqlite3 \
  --legacy-anchor-backup "$EVIDENCE"

PYTHONPATH=backend python3 backend/migrate_fingerprint_payloads.py \
  --state-db .dedup_state/dedup_decisions.sqlite3 \
  --legacy-anchor-backup "$EVIDENCE" \
  --run
```

schema v17에서 바로 시작하는 경우 전용 CLI가 새로 만든 rollback backup 자체를 expected-anchor evidence로
사용하므로 `--legacy-anchor-backup`이 필요 없다.

## rollback 조합

| DB | writer |
| --- | --- |
| schema v19 current DB | 1.5.1 exact build commit |
| `before_fingerprint_payload_v19_*.sqlite3` 복원본 | 1.5.0 `db78cc3` |
| 최초 schema-v17 rollback backup | 1.4.24 `e018e73` |

v19 migration은 rollback backup path와 SHA-256을 settings에 남긴다. global backup retention은 이 path를
보호한다. rollback은 현재 writer를 중지하고 선택한 backup의 SHA/integrity를 재검증한 뒤 원래 DB 경로에
복원하고, 위 표의 코드와 함께 재가동하는 별도 운영 작업이다.

## 검증

집중 fault/edge 회귀:

- shared object의 한 fingerprint ref 강제 누락 → validator/load 실패
- 다른 정상 object hash로 ref 교체 → expected hash mismatch 실패
- v19 legacy anchor INSERT → trigger 실패
- v19 UDF가 없는 old raw SQLite writer → INSERT 실패
- routine server/Folderling/platform old-schema entrypoint → backup 없이 실패
- backup callback 중 outsider write → SQLite writer lock 확인
- logical commit 직후 injected crash → 같은 backup 하나로 resume/report/gate 해제
- backup ENOSPC → source schema/data 유지, 같은 journal ID로 재개
- v18 plan에서 v17 evidence 누락 차단과 ID/hash mismatch rollback

실제 운영 v18 DB의 read-only 복제본 검증:

| 항목 | 결과 |
| --- | ---: |
| source schema | v18 |
| fingerprints | 84,417 |
| v17 expected mapping mismatch | 0 |
| schema after | v19 |
| payload objects | 14,908 |
| payload references | 65,901 |
| missing expected ref | 0 |
| legacy row | 0 |
| integrity | `ok` |
| FK issue | 0 |

복제본 변환은 원본 운영 DB와 PM2 service를 변경하지 않았다.

## 2026-08-27 운영 적용 결과

복제본 acceptance 후 도서 관리 PM2 service 하나만 중지하고 같은 전용 CLI로 적용한 뒤 재가동했다.

| 항목 | 적용 전 | 적용 후 |
| --- | ---: | ---: |
| SQLite schema | v18 | v19 |
| DB 파일 크기 | 300,085,248 bytes | 306,688,000 bytes |
| fingerprints | 84,417 | 84,417 |
| expected `present` / refs | 65,901 | 65,901 |
| expected `none` | - | 18,516 |
| payload objects | 14,908 | 14,908 |
| missing / unexpected / wrong ref | - | 0 / 0 / 0 |
| legacy row | 0 | 0 |
| integrity / FK / Doctor issue | `ok` / 0 / 0 | `ok` / 0 / 0 |

expected state/hash 컬럼 때문에 current DB는 6,602,752 bytes 증가했다. 압축 object/ref 표현과 원래의
70.90% 절감은 유지되며 fingerprint count와 anchor 제외 metadata SHA-256
`2a6d38f42f6bdcdb71e2016c1fa7df1f9639ae7573f5e8b48c9591015871f69e`도 동일하다.

- v17 evidence mapping SHA-256:
  `c232d3c6ef4eb39fa7367b5954462d897b295cc5c25f8f82340ecd52ca72f497`
- v18 rollback backup:
  `.dedup_state/backups/before_fingerprint_payload_v19_23a372785a154a35ac81d7499cf0242b.sqlite3`
- v18 rollback backup SHA-256:
  `90f89279110264347ac29c4e776f2b8e85aa20c81d009b336861cfb8079919c0`
- 최초 v17 rollback backup SHA-256:
  `da8b1fc429ff7794423169f766b0e2128e27d11b7d103a1c626b152af51c1229`
- migration report:
  `.dedup_state/reports/fingerprint_payload_migration_1_5_1_23a372785a154a35ac81d7499cf0242b.json`
- journal phase/report SHA-256:
  `reported` / `6cdd61ff230580a210ec2ba3611224072bbece484d354ea3a6968b892b1ad240`

검증 결과:

- 전체 Python: **1053 passed**, urllib3/LibreSSL 환경 warning 1건
- Python `compileall`, 변경 범위 `pyflakes`, `git diff --check`: 통과
- frontend `typecheck`·production build: 통과 (`file-check-library-ui@1.5.1`)
- `dedup_recover doctor`: issue 0
- 재가동 `/health`: `version=1.5.1`, `schema=19`, `database=ok`
- 적용 시점 build provenance:
  `build_commit=db78cc33f994cde0c7906c0e90a82f26192c374b`, `build_dirty=true`

마지막 build 값은 1.5.1 변경을 아직 commit하지 않은 working tree를 정확히 나타낸다. 정식 release commit 후
서비스를 다시 시작하면 새 commit SHA와 `build_dirty=false`를 acceptance 값으로 사용한다. main merge/tag는
코드 수정과 구분되는 별도 release 결정이며 이 변경에서 임의로 수행하지 않았다.
