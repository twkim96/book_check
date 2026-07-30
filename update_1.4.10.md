# file_check 1.4.10 — archive/review 경합·경로 안전 보강

## 목표

1. archive가 symlink를 따라 `.dedup_state` 밖의 SQLite 원본을 소비할 수 없게 한다.
2. warm review repair와 동시 pair cache miss가 stale/UNIQUE 오류를 남기지 않게 한다.
3. 현재 representative/protected/managed 상태가 바뀌면 open review 방향을 다시 맞춘다.
4. gzip publish 직후 중단된 archive를 동일 원문 증거로 자동 재개한다.
5. preflight·mutation 직전·최종 full Doctor를 보존하면서 같은 실행의 중복 integrity scan만 줄인다.
6. macOS `/var`·`/private/var` 표기 차이로 참조 중인 backup이 archive 대상이 되지 않게 한다.
7. metadata 기록 뒤 cold 객체가 바뀌면 hot 원본을 소비하지 않는다.
8. restore와 cold archive의 모든 쓰기·publish를 고정된 directory descriptor 아래에서 수행한다.
9. 최종 integrity receipt로 Doctor 전후의 동일 storage identity를 증명한다.
10. 동시 fingerprint cache miss도 canonical immutable row 하나로 수렴시킨다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup/archive report: `1.4.10`
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.1` 유지
- fingerprint/pair policy: `1.4.2` 유지
- fingerprint normalizer compatibility: `1.3.0` 유지
- archive metadata schema: `v1` 유지, 1.4.9 객체와 복원 호환

이번 변경은 본문 fingerprint나 pair classification 의미를 바꾸지 않으므로 기존 cache generation을
폐기하지 않는다.

## 리뷰 수용 판단

### 수용: backup/cold root symlink 탈출 P1

기존 plan은 `backups.is_dir()/iterdir()`와 `resolve()`를 함께 사용해 `backups -> ../outside`를 정상
관리 루트처럼 기록할 수 있었다. 1.4.10은 다음을 모두 강제한다.

- backup root 자체를 `lstat`으로 실제 directory인지 확인
- directory descriptor를 component-wise `O_NOFOLLOW`로 열고 `dir_fd` 기준으로 목록/stat 수행
- plan의 source/archive/metadata는 realpath가 아닌 관리 루트 아래 lexical absolute path로 고정
- apply/restore에서 backup root와 cold root를 다시 no-follow open
- source, gzip, metadata와 최종 unlink도 no-follow descriptor/evidence 사용

합성 `.state/backups -> ../outside`와 `.state/cold_archive -> ../outside`에서 plan/apply가 중단되고 외부
SQLite 및 원본 backup이 모두 남는 회귀를 추가했다.

### 수용: warm review repair stale P1

cache hit 뒤 open review가 누락된 repair에만 filesystem identity를 다시 stat한다. DB row의
canonical path, dev/inode/ctime/size/mtime, current fingerprint도 같은 audit entry와 일치해야 한다.
불일치하면 `review_item_stale_skips`를 기록하고 pending/deferred row를 쓰지 않는다. 마지막 snapshot의
stale report 제거와 mutation 직전 SHA/identity 검사는 그대로 유지한다.

### 수용: 동시 pair cache miss 수렴 P2

pair cache insert는 primary key conflict에서 실패하지 않는다. `ON CONFLICT DO NOTHING` 뒤 현재 completed
row를 다시 읽고, 먼저 커밋된 classification/evidence를 canonical review 동기화 입력으로 사용한다.
통계에는 `pair_cache_concurrent_reuses`를 남긴다. 두 auditor barrier 회귀에서 pair row와 open review가
각각 하나만 남아야 한다.

### 수용: review candidate/reference 방향 재계산 P2

warm open-review snapshot은 file/fingerprint/classification/evidence뿐 아니라 현재 파일 상태로 계산한
방향을 포함한다. reference 우선순위는 기존 `_store_review_item` 계약과 동일하다.

1. representative
2. protected
3. managed
4. house
5. 안정적인 rel path

방향이 바뀌면 과거 open row를 supersede하고 현재 방향으로 새 row를 만든다. 하류 mutation의 기존
representative/protected 재검증도 유지한다.

### 수용: gzip-only crash recovery P2

source와 `.gz`만 있고 metadata가 없으면 기존 gzip을 같은 no-follow descriptor에서 압축 SHA와 raw
SHA/size로 검증한다. 현재 source와 정확히 같을 때만 schema-v1 metadata를 no-clobber로 재구성한다.
불일치·손상 gzip은 source를 소비하지 않는다.

### 범위를 제한해 수용: 반복 integrity scan P2

full SQLite integrity 검사는 다음 세 경계에 남긴다.

- one-button preflight full Doctor
- auditor/reconcile 뒤 첫 mutation 직전 full Doctor
- 모든 도서 mutation 뒤 최종 DB projection full Doctor

다음 세 중복만 줄인다.

- one-button의 schema open: 바로 이어지는 verified backup integrity + preflight Doctor가 있으므로 structural
- verified inventory에 묶인 auditor cache open: preflight 완료 receipt가 있고 mutation 전 full Doctor 유지
- terminal Doctor: 최종 projection receipt의 run ID와 main/WAL/journal identity가 같을 때만 integrity 재사용

terminal 단계도 file identity, schema objects, active/unfinished run·operation과 representative 관계는 다시
검사한다. receipt가 위조됐거나 storage identity가 달라지면 full integrity로 복귀한다. standalone auditor와
일반 `initialize_state_db()`의 기본 full integrity 동작은 바꾸지 않는다.

## 추가 리뷰 수용 판단

### 수용: macOS 안정적 경로 별칭 P1

임의 symlink를 따라가는 `resolve()`는 계속 금지한다. 대신 공용 mutation path 계약과 동일하게
`/var`↔`/private/var`, `/tmp`↔`/private/tmp`만 lexical absolute path 단계에서 정규화한다. DB가
`/var/.../backup.sqlite3`을 참조하고 plan이 `/private/var/.../backup.sqlite3`을 보더라도 같은 참조로
제외된다. 일반 `backups -> outside` symlink는 여전히 no-follow 단계에서 거부한다.

### 수용: source 소비 직전 cold 증거 재검증 P1

metadata를 durable publish한 뒤 SQLite writer transaction에 들어가 최종 참조를 확인하고, source unlink
바로 전에 metadata를 no-follow descriptor로 다시 읽는다. metadata의 state/source/archive 계약과 source
SHA·size를 다시 대조하고 gzip 압축 bytes의 SHA-256·size를 다시 계산한다. metadata 또는 gzip을 이
구간에서 바꾼 합성 경합은 모두 오류로 끝나며 hot source는 남는다.

### 수용: restore parent FD pinning P2

`backups` directory FD를 임시파일 생성부터 decompression, descriptor-backed SQLite integrity,
`linkat` no-clobber publish, directory fsync와 최종 SHA 검증까지 닫지 않는다. publish 전후 lexical root가
같은 dev/inode인지 확인하고, 교체됐으면 고정 FD 아래 임시파일·부분 destination을 제거한다. 합성
`backups` rename 뒤 외부 symlink 교체에서도 외부 directory에는 파일이 생성되지 않는다.

### 수용: Doctor 전후 integrity receipt P2

read-only SQLite가 첫 schema read에서 만들 수 있는 0-byte WAL을 먼저 안정화한다. 그 뒤 receipt를
발급하고 full Doctor를 실행한 다음 같은 receipt가 current인지 다시 확인한다. Doctor 도중 또는 반환
직후 commit은 이 사후 확인에서 중단되고, 이후 DB row projection·house walk·index publication 중 변경도
terminal Doctor에서 receipt를 무효화한다.

## 최종 리뷰 수용 판단

### 수용: cold root FD pinning P2

gzip 임시파일 생성·압축·`linkat` publish, metadata JSON의 no-clobber publish와 directory fsync, source
소비 직전 gzip/metadata 재검증까지 같은 `cold_archive/backups` directory FD를 유지한다. 일반 intent·
report JSON도 parent FD 기준으로 publish한다. gzip 또는 metadata 단계에서 cold root를 외부 symlink로
교체한 합성 경합은 hot source를 보존하고 외부 directory에 파일을 만들지 않는다.

### 수용: 동시 fingerprint miss 수렴 P2

fingerprint insert도 immutable unique key에 `ON CONFLICT DO NOTHING`을 사용한다. 충돌한 auditor는 현재
identity·policy의 canonical row를 다시 읽고, 새로 계산한 raw/normalized SHA·anchor·status 증거와 같을
때만 그 fingerprint ID로 `files.current_fingerprint_id`를 맞춘다. 다른 증거면 캐시를 섞지 않고 오류로
중단한다. pair miss와 별도로 두 auditor·두 파일 cold miss 회귀를 둔다.

### 보류 유지: hot retention과 전체 장기 수명주기 P2

지적은 타당하지만 1.4.10 archive의 회귀는 아니며 기존 문서의 명시적 제외 범위다. 2026-07-30 read-only
확인에서 `actual_runs.backup_path` 94개 중 실파일은 4개, 이미 없는 경로는 90개였다. 완료 run까지 모두
즉시 보호하는 부분 수정은 현재 약 720MB 상태 DB backup을 실행마다 무기한 쌓고, 반대로 기존 참조를
archive 경로로 바꾸는 수정은 cold object ID·복원 조회·manifest 보존 schema 없이 증거를 훼손한다.
따라서 후속 버전에서 cold object와 actual-run 참조를 먼저 연결하고 retention이 archive 전환을 호출하게
한다. 1.4.10은 “미참조 backup tier 1”까지만 완료한 것으로 최종 고정한다.

## 명시적 보류

완료 actual-run backup을 기존 hot retention이 직접 지우는 정책, manifest/report 압축, 과거
fingerprint/pair generation archive는 1.4.10에 합치지 않는다. 1.4.9가 문서화한 “미참조 backup tier 1”
범위를 유지하며, 영속 참조 schema와 복원 조회 계약 없이 용량만 보고 삭제하지 않는다.

## 실제 상태 읽기 전용 확인

2026-07-30의 `.dedup_state/dedup_decisions.sqlite3`에는 plan만 실행했다.

- archive version: `1.4.10`
- blocker / unsafe path: 0 / 0
- 최신 미참조 hot 보존: 2개
- 보관 가능: 5개, 2,909,040,640바이트
- 새 plan SHA-256: `d82d244284af0bdc16c91fbf87ee3542d7ba2bbbd886b5d4c6d9e35e455046db`
- 실제 압축·원본 소비: 0건

1.4.9 plan SHA는 archive version과 경로 계약이 바뀌어 더 이상 적용되지 않는다. 실제 apply가 필요하면
위 결과도 그 시점에 다시 생성·확인하고 별도 승인해야 한다.

## 완료 조건

- [x] archive symlink root·gzip-only crash recovery 회귀 통과
- [x] stale review repair·방향 전환·동시 pair miss 회귀 통과
- [x] receipt-bound structural validation과 terminal fallback 회귀 통과
- [x] 추가 경로 별칭·cold 변조·restore swap·Doctor receipt 회귀 통과
- [x] 최종 Doctor-window·cold publish swap·동시 fingerprint miss 회귀 통과
- [x] 전체 Python 회귀: `819 passed in 21.11s`
- [x] frontend 1.4.10 production build
- [x] compileall / diff check / normalizer parity 35 cases
- [x] 실제 `.dedup_state`에는 read-only plan만 실행하고 archive apply는 별도 승인 전까지 미실행
- [x] 1.4.10 커밋 후 작업 트리 clean
