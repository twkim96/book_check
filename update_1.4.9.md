# file_check 1.4.9 — 검증된 상태 백업 cold archive

## 목표

1. `.dedup_state/backups`의 큰 SQLite 사본 중 현재 어떤 actual-run 증거도 참조하지 않는 과거 파일만
   압축 보관한다.
2. 계획 확인, source identity, SQLite 무결성, 압축 round-trip을 모두 증명한 뒤에만 hot 원본을
   소비한다.
3. 중단 후에도 intent와 객체 metadata로 처리 범위를 확인하고, cold 객체를 보존한 채 원경로로
   검증 복원할 수 있게 한다.
4. fingerprint/pair 판정, 도서 mutation, final Doctor의 의미는 바꾸지 않는다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.9`
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.1` 유지
- fingerprint/pair policy: `1.4.2` 유지
- fingerprint normalizer compatibility: `1.3.0` 유지

상태 백업 수명주기만 추가하므로 기존 본문 fingerprint와 pair cache generation은 그대로 사용한다.

## 대상 경계

보관 가능 대상은 `.dedup_state/backups/*.sqlite3` 중 다음을 모두 만족하는 파일이다.

- 현재 `actual_runs.backup_path` 전체와 `settings.approved_backup` 어디에도 참조되지 않음
- 최신 미참조 hot backup 2개보다 오래됨
- symlink가 아니며 hardlink 수가 1인 regular file
- 계획 시점 identity와 적용 시점 identity가 같음
- SQLite `PRAGMA integrity_check=ok`

approved/active actual run, 미완료 operation, 미완료 operation group이 하나라도 있으면 전체 apply를
차단한다. 참조 중인 backup, manifest, fingerprint/pair row, `-wal/-shm`은 1.4.9에서 변경하지 않는다.

## 실행 계약

### 읽기 전용 계획

```bash
PYTHONPATH=backend python backend/state_archive.py \
  --state-db .dedup_state/dedup.sqlite3 plan
```

출력의 `eligible_count`와 `plan_sha256`을 사람이 확인한다. apply는 두 값을 모두 요구하고 lock 안에서
현재 계획을 재구성한다. source identity, 참조 집합, 보존 개수 중 하나라도 바뀌면 stale plan으로
중단한다.

### 검증 보관

```bash
PYTHONPATH=backend python backend/state_archive.py \
  --state-db .dedup_state/dedup.sqlite3 apply \
  --confirm-count <COUNT> --confirm-plan-sha256 <SHA256>
```

각 항목은 source SHA·SQLite integrity를 확인하고 deterministic gzip을 만든다. 압축 객체의 SHA와
압축 해제한 raw SHA/size를 같은 no-follow descriptor에서 재검증한 뒤 객체 metadata를 fsync한다.
마지막으로 SQLite writer transaction을 잡고 현재 참조/미완료 상태와 source 증거를 재확인한 후에만
원본을 unlink한다.

actual-run 승인도 writer transaction 안에서 backup evidence를 다시 확인하도록 보강했다. archive가
먼저 writer 경계를 잡으면 승인이 사라진 source를 거부하고, 승인이 먼저 잡으면 archive가 새 참조를
보고 원본 소비를 거부한다.

### 검증 복원

```bash
PYTHONPATH=backend python backend/state_archive.py \
  --state-db .dedup_state/dedup.sqlite3 restore \
  --metadata <OBJECT.archive.json> --confirm-raw-sha256 <RAW_SHA256>
```

복원은 기존 hot 경로가 비어 있을 때만 no-clobber로 수행한다. cold archive SHA, raw SHA/size,
복원 SQLite integrity를 확인하고 cold 객체를 유지한다.

## 로그와 중단 복구

- 시작 전 `state_archive_1_4_9_intent_*.json`
- 성공 후 `state_archive_1_4_9_*.json`
- 객체별 `cold_archive/backups/*.archive.json`
- 복원 후 `state_archive_1_4_9_restore_*.json`

중간에 실패하면 이미 검증된 cold 객체와 metadata는 남고 source는 소비되지 않거나, 성공한 객체만
source가 사라진다. 다음 plan은 남은 source만 다시 계산한다. 기존 cold 객체와 source가 함께 있는
재시도는 양쪽 SHA와 round-trip이 모두 같을 때만 source 소비 단계로 진행한다.

## 실제 상태 읽기 전용 확인

2026-07-30의 `.dedup_state/dedup_decisions.sqlite3`를 변경하지 않고 plan만 계산했다.

- open actual run / unfinished operation blocker: 0
- symlink / hardlink unsafe 후보: 0
- 최신 미참조 hot 보존: 2개
- 보관 가능: 5개, 2,909,040,640바이트
- plan SHA-256: `4fd5950e923f8e038ab573ddb25df6836b1e70cf76ea339626d381a0ff414cab`

실제 압축·원본 소비는 수행하지 않았다. 위 SHA는 이후 파일·DB 상태가 바뀌면 자동으로 stale이 되며,
실행하려면 그 시점의 새 plan을 다시 확인하고 별도 승인해야 한다.

## 완료 조건

- [x] archive 전용 회귀와 actual-run 승인 경합 회귀 통과
- [x] 전체 Python 회귀: `800 passed in 19.95s`
- [x] frontend 1.4.9 production build
- [x] compileall / diff check / normalizer parity 35 cases
- [x] 실제 `.dedup_state` 읽기 전용 plan으로 참조·보존·대상 수량 검증
- [x] 실제 archive apply는 별도 사용자 승인 전까지 수행하지 않음
