# file_check 1.4.17 — Folderling 운영 안정화와 실제 정리 완료

## 목표

1. 재마운트·서버 재시작 뒤에도 안전하게 Folderling을 복구한다.
2. 대용량 감사와 EPUB/TXT 예외가 정상 파일의 처리를 막지 않게 한다.
3. 강하게 증명된 중복은 복구 가능 격리까지 수렴시키고, 사람의 판본 판정은 보존한다.
4. 실제 폴더링 뒤 오탐·미탐과 Doctor 상태를 확인한다.

## 핵심 변경

### 재마운트·재시작 복구

- macOS/APFS 재마운트로 활성 파일 17,681개와 관리 폴더 1개의 `st_dev`만 함께 바뀐 것이
  17,682건 `stale_identity`의 원인이었다. 경로·inode·ctime·size·mtime가 모두 같은 완전한
  device 재번호만 백업 후 자동 재결합하며, 부분 변경이나 다른 Doctor 문제는 계속 차단한다.
- job에 actual run ID를 직접 보존하고 active job은 표시 개수와 무관하게 전부 검색한다. 이벤트 기록
  직전 중단도 유일한 실행만 보수적으로 연결한다.
- 사용자가 서버를 재시작해 작업이 끊긴 경우, durable mutation 표식과 operation/group이 모두 없는
  run만 자동 종료한다. 파일·관계 변경이 시작된 run은 기존 수동 복구 절차를 유지한다.

### Auditor와 입력 파일

- cold cache가 20 GiB 읽기 예산을 넘은 경우 64 GiB로 한 번 재기준한다.
- 같은 이름의 EPUB 멤버는 내용이 모두 같을 때만 하나로 접는다. 상충하는 신규 temp EPUB은
  삭제하지 않고 journal warning hold로 보존하며, house 오류는 계속 fail-closed한다.
- 손상 의심 TXT 69개는 실제로 모두 reversible byte escape로 읽을 수 있었다. 이 파일들은
  exact identity에만 사용하며 fuzzy/containment 판정에는 쓰지 않았다. 실제 읽기 불능 TXT는 0개라
  삭제한 파일도 없다.

### 중복 정리 수렴과 안전선

- 디스크뿐 아니라 비활성 DB 경로 점유까지 확인해 격리 목적지의 UNIQUE 충돌을 막는다.
- 같은 run에서 입고 뒤 생긴 exact/ordered 중복과 queue exact 중복도 현재 identity·SHA·review
  관계를 다시 확인한 뒤 journal 격리한다.
- normalized exact, contained, ordered-body, 제한된 양방향 near-identical 증거를 mutation 직전에
  다시 계산한다. 명시적 개정판·외전·회차 범위 차이와 기존 사용자 판정은 자동 격리하지 않는다.
- 모든 자동 처리는 backup, manifest, no-clobber 이동, operation journal, recovery와 최종
  Doctor 0건을 요구한다. 원본 bytes를 영구 삭제하지 않는다.

## 실제 운영 결과

2026-08-13 실제 Folderling run `actual-9ac66f8b-8bcc-4387-a76d-a7a26590ef66`까지 완료했다.
작업 화면의 `needs_review`는 실패가 아니라 기존 분권 정리 후보 31건이 남았다는 표시다.

- 오늘 자동 격리 2,253건을 현재 양쪽 파일 bytes로 재검증: **2,253 통과 / 0 실패**
  - raw exact 2,027, ordered-body 220, normalized exact 3, contained 3
- 최종 house full-fingerprint sweep: 17,886개 eligible fingerprint 사용, global exact pair 0,
  `decode_lossy` 0, EPUB 분석 오류 0, stop reason 0
- fuzzy/containment는 bounded 후보 검사이며 선택된 2,905쌍을 완료했다. contained 후보 40쌍은
  분권·합본·외전 또는 최종 증거 부족으로 자동 처리 기준을 충족하지 않았다.
- 잔여 강한 본문 후보 5쌍: 모두 활성 `same_work_distinct_variant` 판정에 연결된 개정·재편·
  분산 편집·회차 범위 차이 판본
- 자동 처리 가능한 pending strong review 0, 활성 판정과 충돌하는 review 0
- 최종 Doctor 0, 활성 actual run 0, 미완료 operation/group 0, raw/normalized 활성 중복 그룹 0
- 최종 서비스 상태: 입고 대상 0, `blocked_code=no_targets`, `doctor_ok=true`

근거 보고서는 `folderling_outcome_audit_20260813_170207_132337`와
`strong_candidates_20260813_170332_206677`이다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.17`
- Chrome extension manifest: `2.10` 유지
- DB schema: `v15` 유지
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.16-lossless-legacy-v3`
- strong proof policy: `1.4.16-pinned-v1`
- archive object/version: `1.4.10` 유지

내부 fingerprint/pair compatibility ID는 실제 판정 의미가 바뀐 범위만 갱신했다. 배포 번호만
맞추기 위해 다시 올려 검증된 house cache를 불필요하게 전면 재분석하지 않는다.

## 검증

- 공개+운영 Python 전체 회귀: `920 passed`
- backend/tools compileall, pyflakes, frontend typecheck·production build, normalizer parity,
  `git diff --check`: 통과
- 로컬 Anaconda에는 `coverage` 모듈이 없어 coverage 수치는 재측정하지 않았으며, push CI의
  Python 3.12 `fail-under=70` 게이트에서 확인한다.
- PM2 관리 서버 재시작 뒤 `/health version=1.4.17`과 Folderling Doctor/readiness 재확인
