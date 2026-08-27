# file_check 1.5.0 — content-addressed fingerprint anchor payload

기준:

- 변경 전 코드: `e018e73` (`1.4.24`)
- 서버/UI: `1.4.24` → `1.5.0`
- SQLite schema: `v17` → `v18`
- 저장 표현만 변경하며 normalizer/fingerprint/pair 판정 의미는 유지

## 목표

`fingerprints`의 immutable 이력과 이를 참조하는 decision/review/pair/operation 증거를 삭제하지 않고,
각 fingerprint row에 반복된 `front_anchor`/`tail_anchor`만 한 번 압축 저장한다. PostgreSQL이나 별도
archive service는 도입하지 않으며 SQLite 한 파일의 transaction, backup, Doctor 계약을 유지한다.

변경 전 운영 DB read-only plan 기준은 다음과 같다.

| 항목 | 값 |
| --- | ---: |
| schema | v17 |
| DB 크기 | 1,031,340,032 bytes |
| fingerprints | 84,417 |
| non-empty legacy anchor rows | 65,901 |
| legacy anchor UTF-8 bytes | 762,531,403 |
| migration blocker | 0 |

## schema v18

### `anchor_payload_objects`

- `payload_hash`: canonical framed raw bytes의 lowercase SHA-256
- `codec`: `zlib-6-v1`
- `front_byte_length`, `tail_byte_length`, `raw_length`
- `compressed_payload`: canonical frame 전체의 zlib level 6 BLOB
- `raw_checksum`: content key와 동일한 raw SHA-256 검증값
- update/delete 금지 trigger

### `fingerprint_anchor_refs`

- `fingerprint_id` 1:0..1 primary/FK
- `payload_hash` FK와 조회 index
- update/delete 금지 trigger
- empty/deferred/raw-only fingerprint는 ref가 없으며 `("", "")`로 해석

기존 `fingerprints.front_anchor`/`tail_anchor` 컬럼은 schema 호환과 비상 legacy fixture read를 위해 nullable로
남긴다. v18 migration 완료 행과 모든 신규 production writer는 두 컬럼을 `NULL`로 유지한다. 따라서 실제
용량은 legacy overflow page를 비운 뒤 `VACUUM`으로 회수하면서, 이전 test/inspection SQL의 구조적 호환은
보존한다.

## canonical serialization과 손상 경계

payload identity는 `magic + uint64(front UTF-8 byte length) + uint64(tail UTF-8 byte length) + front + tail`
순서의 canonical bytes로 만든다. front/tail 단순 연결은 사용하지 않는다.

read는 다음을 모두 검증한다.

1. 지원 codec과 64 MiB raw 상한
2. bounded zlib decompression, 정확한 EOF와 trailing data 부재
3. declared raw length와 실제 decompressed length
4. `payload_hash == raw_checksum == SHA-256(raw frame)`
5. magic, 두 boundary 길이와 실제 byte slice
6. strict UTF-8 decode
7. legacy TEXT와 ref가 동시에 존재하는 과도기에는 byte-for-byte 동일성

하나라도 어긋나면 `AnchorPayloadCorruptionError`로 fail-closed한다. 손상 시 파일 본문을 읽어 anchor를
추측 복구하거나 공유 object를 제자리 갱신하지 않는다. codec 변경은 후속 schema/object generation으로
수행한다.

## writer/read 경로

- `PersistentAuditCache.store_analysis`: fingerprint metadata insert와 같은 writer transaction에서 object를
  `ON CONFLICT DO NOTHING`으로 공유하고, 기존 object를 다시 압축 해제해 hash collision/손상을 확인한 뒤
  ref를 기록한다. legacy TEXT는 쓰지 않는다.
- warm bulk preload: 기존처럼 SHA/identity/status만 읽고 payload를 읽지 않는다.
- pair-cache miss 상세 비교: fingerprint ID로 object/ref를 조회해 검증 압축 해제한다.
- review-action 및 recovery clone: 새 fingerprint ID를 만든 뒤 source payload evidence를 공유한다.
- library detail API: 동일 decoder를 사용하므로 UI contract의 front/tail 문자열은 유지한다.

## v17 → v18 migration

기본 명령은 read-only plan이다.

```bash
PYTHONPATH=backend python3 backend/migrate_fingerprint_payloads.py
```

실제 변환은 `--run`을 명시해야 한다.

```bash
PYTHONPATH=backend python3 backend/migrate_fingerprint_payloads.py --run
```

적용 조건과 순서:

1. house/temp root mutation lock
2. schema가 정확히 v17이며 newer schema가 아님
3. approved/active actual run, unfinished operation/group 0
4. DB 크기의 3배 + 256 MiB free disk
5. `before_fingerprint_payload_v18_*.sqlite3` SQLite backup과 integrity 확인
6. 한 `BEGIN IMMEDIATE` transaction에서 object/ref 생성
7. 모든 ref를 기존 TEXT와 decompressed byte-for-byte 검증
8. fingerprint immutable trigger를 migration transaction 안에서만 교체하고 legacy TEXT를 `NULL`로 전환
9. schema v18, payload validator, SQLite integrity/FK 확인
10. WAL checkpoint와 `VACUUM`, 다시 full validation
11. fingerprint 수와 anchor 제외 metadata SHA-256이 변환 전후 동일한지 확인
12. `.dedup_state/reports/fingerprint_payload_migration_1_5_0_*.json` 기록

중간 오류는 logical migration transaction 전체를 rollback한다. logical commit 뒤 compaction 오류가 나더라도
schema v18/object/ref가 source of truth이며 v17 code는 version mismatch로 write를 거부한다. rollback이
필요하면 1.5.0 writer를 중지하고 보고서의 검증 backup을 원래 DB 경로로 복원한다.

## 호환성 버전

| 계약 | 값 |
| --- | --- |
| server/UI | `1.5.0` |
| SQLite schema | `v18` |
| normalizer | `1.3.3` |
| fingerprint version/policy | `5` / `1.4.2` |
| pair policy | `1.4.16-lossless-legacy-v3` |
| duplicate auditor | `1.4.17` |
| archive | `1.4.10` |
| bare-volume context | `1.4.24` |

저장 표현만 바뀌므로 기존 fingerprint/pair cache generation을 무효화하거나 도서 본문을 다시 읽지 않는다.

## 회귀 검증

- canonical front/tail boundary 구분
- 신규 writer의 legacy TEXT 미사용
- 동일 payload 2 ref → 1 object dedup
- v17→v18 fingerprint ID·metadata 보존
- empty anchor의 ref 없는 표현
- object/ref update/delete 차단
- compressed BLOB 손상 시 fail-closed
- persistent warm audit 결과와 body-read 0 계약
- 전체 Python: **1046 passed**, urllib3/LibreSSL 환경 warning 1건
- Python `compileall`·`pyflakes`·`git diff --check`: 통과
- JS normalizer parity: **47 passed** (`NORMALIZER_VERSION=1.3.3`)
- frontend `typecheck`·production build: 통과 (`file-check-library-ui@1.5.0`)

## 2026-08-27 운영 적용 결과

검증 backup을 만든 뒤 도서 관리 서비스만 중지하고 migration을 적용했으며, 완료 후 같은 PM2 service를
재가동했다.

| 항목 | 적용 전 | 적용 후 |
| --- | ---: | ---: |
| SQLite schema | v17 | v18 |
| DB 파일 크기 | 1,031,340,032 bytes | 300,085,248 bytes |
| fingerprint 수 | 84,417 | 84,417 |
| non-empty legacy anchor rows | 65,901 | 0 |
| payload objects | - | 14,908 |
| payload references | - | 65,901 |
| unique framed raw bytes | - | 173,109,959 |
| compressed payload bytes | - | 79,070,970 |
| FK issue | 0 | 0 |

DB 파일은 **70.90%** 줄었다. payload는 fingerprint당 평균 4.42개 ref가 object 하나를 공유하며, 공유 후
고유 canonical payload 자체도 zlib으로 54.32% 줄었다. fingerprint anchor 제외 metadata SHA-256은
변환 전후 모두 `2a6d38f42f6bdcdb71e2016c1fa7df1f9639ae7573f5e8b48c9591015871f69e`로
동일하다.

- full SQLite integrity: `ok`
- full payload/FK validator와 `dedup_recover doctor`: issue 0
- 재가동 health: `version=1.5.0`, `database=ok`
- dashboard operational Doctor: issue 0
- migration report: `.dedup_state/reports/fingerprint_payload_migration_1_5_0_20260827_204810_741783.json`
- v17 rollback backup SHA-256: `da8b1fc429ff7794423169f766b0e2128e27d11b7d103a1c626b152af51c1229`

안전한 rollback을 위해 1,031,340,032-byte v17 backup은 삭제하지 않았다. 따라서 migration 직후
`.dedup_state` 전체 점유량은 DB 파일 감소분만큼 바로 줄지 않으며, 실제 총공간 절감은 기존 backup
retention 정책이 이 rollback copy를 만료시킨 뒤 완전히 반영된다.
