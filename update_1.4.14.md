# file_check 1.4.14 — 상태 저장소 모듈 ownership 정리

## 목표

1. `decision_store.py`의 서로 다른 책임을 실제 구현 모듈로 분리한다.
2. 기존 서버·CLI·테스트의 `decision_store.*` import와 monkeypatch 계약은 유지한다.
3. actual-run부터 Doctor까지 이어지는 mutation 상태 머신은 한곳에 남겨 안전 불변조건을 보존한다.
4. 현재 제품이 import하지 않는 과거 일회성 도구를 `tools/legacy/`로 격리한다.
5. 제목·중복·파일 이동 의미와 DB/cache 세대는 바꾸지 않는다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.14`
- DB schema: `v15` 유지
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3` 유지
- bare-volume context policy: `1.4.12` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.12` 유지
- fingerprint/pair normalizer compatibility: `1.3.0` 유지
- archive object/version: `1.4.10` 유지

구조와 배포 표기만 바뀐다. schema migration, filename projection 재기준, fingerprint/pair cache 무효화,
실제 house 전체 스캔은 요구하지 않는다.

## 리뷰 제안 수용 결과

### 수용: schema와 repository 분리

- `backend/state_schema.py`
  - schema version과 DDL, 필수 table/view 계약만 보관한다.
  - import 시 DB open, migration, write를 수행하지 않는다.
- `backend/state_repository.py`
  - writer/read-only connection, v1→v15 migration, schema validation, transaction,
    canonical/retired path를 소유한다.
  - migration은 기존처럼 명시적 `migrate=True`와 상위 backup-owning entry point를 요구한다.

### 수용: 좌표와 현재 파일 분석의 단일 구현

- `backend/volume_policy.py`
  - `canonical_rational`, symbolic coordinate, filename coordinate projection,
    `coordinates_compatible`를 한곳에 둔다.
- `backend/file_analysis_repository.py`
  - current/stale 분석 판단, stored author 보존, analysis upsert, contextual bare-volume 보정,
    catalog title rekey, Scanner projection을 한곳에 둔다.
- `decision_store.py`는 기존 이름을 re-export하며 validation과 analysis monkeypatch hook을 thin wrapper로
  유지한다. 추출 모듈은 facade를 역으로 import하지 않아 순환 import와 module identity 분기를 막는다.

### 수용: 구형 일회성 도구 격리

다음 파일을 제품 `backend/`에서 `tools/legacy/`로 이동했다.

- `migrate_marker_position.py`
- `build_quarantine_cleanup_plan_1_4_4.py`
- `cleanup_quarantine_1_4_4.py`

marker migration actual은 1.4.13의 사전 hard-fail을 그대로 유지한다. 하드코딩 builder는 당시 감사
재현용이며 신규 계획 생성기로 사용하지 않는다. 1.4.4 cleanup runner는 단순 비호환 스크립트가 아니라
SHA-bound plan, backup, manifest, journal, root lock, Doctor와 실제 purge 회귀를 갖고 있으므로 실행 코드를
죽이지 않고 legacy 감사 재현 도구로 보존한다.

## 이번 버전에서 분리하지 않은 제안

- `actual_run.py`, `operation_journal.py`, `recovery.py`, `doctor.py`: 실제 코드에서는 approval receipt,
  backup/manifest, operation 전이, crash recovery, terminal Doctor가 서로의 상태를 직접 검증한다. 이번에
  파일 경계부터 나누면 안전 계약을 여러 module에 흩뜨리므로 `decision_store.py` 안에 함께 유지한다.
- `file_identity.py`: no-follow descriptor, SHA, inode/ctime evidence, owned unlink는 이미
  `mutation_io.py`의 책임이다. 새 파일은 단일 구현이 아니라 중복을 만든다.
- 범용 `dedup_policy.py`: episode 관계는 `dedup_episode_relation.py`, bare volume은
  `bare_volume_context.py`, 좌표는 새 `volume_policy.py`가 소유한다. 이름만 큰 policy module을 추가하지
  않는다.
- `duplicate_auditor.py`, `deduplicator.py`, `folderling.py` 동시 분할: 이번 버전의 저장소 경계와 함께
  바꾸면 회귀 원인을 격리할 수 없다. 후속 변경은 실제 중복 책임과 독립 테스트 경계가 확인될 때만 한다.

## 구조 결과

- `decision_store.py`: 5,965줄 → 3,533줄
- `state_schema.py`: 492줄
- `state_repository.py`: 768줄
- `volume_policy.py`: 191줄
- `file_analysis_repository.py`: 1,178줄

중요한 기준은 줄 수 자체가 아니라, 파일 분석 수정이 actual-run/purge recovery 구현을 건드리지 않고,
좌표 판단이 여러 mutation caller에서 다시 구현되지 않는다는 점이다.

## 검증 결과

- 기계적 AST 비교
  - 기존 `decision_store.py`에서 옮긴 함수 108개의 AST가 동일함을 확인했다.
  - `initialize_state_db`, `sync_contextual_bare_volume_metadata` 두 함수만 기존 monkeypatch/module identity
    계약을 유지하는 thin wrapper로 의도적으로 바뀌었다.
- facade/module ownership 및 schema round-trip: `4 passed`
- schema/current analysis, actual-run receipt, Folderling projection, volume grouping, legacy cleanup 집중 회귀:
  `144 passed`
- 공개 회귀: `453 passed`, backend coverage `72%` (`fail-under=70` 통과)
- 운영 체크아웃 전체 회귀: `856 passed`, backend coverage `81%` (`fail-under=75` 통과)
- ignored 운영 테스트와 extension이 없는 clean public checkout 재현: `453 passed`
- frontend TypeScript typecheck와 production build: 통과
- backend/tools compileall, pyflakes, normalizer parity, `git diff --check`: 통과

검증은 합성 temp fixture와 빌드만 사용했다. 실제 상태 DB, index, house/temp 도서 이동·격리·삭제,
Folderling actual 실행은 수행하지 않았다.
