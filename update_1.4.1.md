# file_check 1.4.1 — 순서형 본문 중복 자동 격리

- 상태: 구현·운영 검증·cold-cache 마감 패치 완료
- 시작 기준: `3334edb` (`docs: record strong review queue cleanup`)
- 적용일: 2026-07-28
- 범위: 동일 좌표·완전 중첩·외전 합계·회차/권 판본의 자동 중복 정리

## 1. 목표와 결정

1. 같은 `core_title`의 흔한 장편 중복을 계속 사람 판단에 넘기지 않는다.
2. `1-100` 대 `1-150`뿐 아니라 `1-150` 대 `1-130 외전 1-20`,
   `1-150화` 대 `1-9권`도 본문으로 입증할 수 있게 한다.
3. 회차와 권 사이의 고정 환산식은 만들지 않는다. 좌표는 후보를 좁히고, 최종 동일성은
   현재 전체 본문의 순서형 증거로만 판단한다.
4. 작가는 양쪽에 모두 명시되어 서로 다를 때만 차단한다. 작가 누락은 무시한다.
5. 자동 격리 기준은 90%가 아니라 95%로 둔다. 90% 이상 95% 미만은 warning 검토로 남긴다.

## 2. 본문 증거 계약

- TXT를 NFC 정규화하고 각 물리 줄의 공백을 제거한 뒤, 정확히 같은 줄이 나타나는 순서를
  글자 수 가중 LCS로 계산한다.
- 버릴 판본의 정규화 본문이 100,000자 이상이고, 95% 이상이 남길 판본에 같은 순서로
  일치해야 한다.
- 64회를 넘게 반복되는 줄은 일치 증거로 세지 않지만 전체 분모에는 남긴다.
- 순서 비교 그래프는 500,000개 노드로 제한하고 초과 시 fail-closed한다.
- 최대 연속 불일치가 본문의 약 2%를 넘으면 전체 점수가 95% 이상이어도 자동 격리하지 않는다.
- 감사 결과를 그대로 믿지 않고 mutation 직전에 no-follow FD로 두 파일을 다시 열어 identity,
  normalized SHA-256, 순서형 본문 증거를 모두 재검증한다.
- 변경 없는 pair는 auditor 1.4.1 결과 cache를 재사용하므로 이후 실행에서는 본문을 다시 읽지 않는다.

## 3. 좌표와 keep 규칙

- `same_coordinates`: 동일 회차/권 좌표
- `contained_coordinates`: 시작이 같고 한쪽의 본편·외전 총 범위가 더 넓은 완전 중첩
- `side_aggregate_equivalent`: `1-150`과 `1-130 외전 1-20`처럼 총 범위가 같음
- `cross_unit_edition`: 양쪽이 1에서 시작하는 회차판·권 단행본판

완전 중첩은 선언 범위가 넓은 판본을 남긴다. 나머지는 기존 `choose_keep` 순서인 비교 가능한
편수, 5%를 넘는 본문 길이 차이, 완결 표기, 짧은 파일명, 안정적인 파일명 순서를 사용한다.
이미 서로 다른 managed variant로 확정했거나 보호 관계가 충돌하면 자동 격리를 금지한다.

## 4. 실자료 읽기 전용 보정

- 1.4.0에서 수동 확정한 동일 core·동일 좌표 중복 4쌍은 방향별 약 99.21~99.59%로 측정됐다.
- 반면 이미 다른 판본으로 보존한 적격 관계도 약 98.29~100%가 나왔다. 따라서 본문 백분율만으로
  판본 결정을 덮어쓰면 안 되며, managed variant 차단은 필수다.
- 줄 재배치가 큰 과거 중복 1쌍은 약 71~72%였다. 해당 쌍은 좌표도 불명확해 자동 대상이 아니며,
  이런 재편집본은 오탐 방지를 위해 기존 검토 경로에 남긴다.
- 이 보정 결과에 따라 90% 자동선은 채택하지 않고 `95% 자동 / 90~95% 검토`로 고정했다.

실자료 보정은 파일과 DB를 변경하지 않는 읽기 전용 검사로만 수행했다.

## 5. 안전한 실행과 기록

- 자동 확정 결과는 `status=ordered_duplicate`와 `ordered_body_quarantine_count`에 집계한다.
- 격리 위치는 `txt_temp/trash_bin/ordered_body_duplicates`이며 원본 bytes는 삭제하지 않는다.
- 보고서에는 keep/discard, 좌표 모드, 양쪽 normalized SHA-256, 전체·일치 글자 수, 일치율,
  최대 연속 불일치, ingest/quarantine operation ID와 최종 경로를 남긴다.
- temp 판본을 남길 때는 먼저 house에 journal 입고하고 managed 대표를 넘긴 뒤 기존 house 파일을
  격리한다. 어느 단계든 현재 상태가 달라지면 fail-closed한다.
- 관리 화면의 `본문 95% 자동 격리` 필터에서 실제 격리 파일을 읽기 전용으로 확인할 수 있다.

## 6. 회귀 범위

- [x] 동일 좌표 96% 자동 격리와 `choose_keep`
- [x] 95% 경계값 포함
- [x] temp 없이 기존 house 두 판본만 있는 전수 재검사
- [x] 외전 합계 동등 자동 격리
- [x] 회차판·권 단행본판 후보와 자동 격리
- [x] 90~95% warning, 연속 4% 개정 warning
- [x] 명시 작가 충돌 차단
- [x] 한쪽 작가 누락 허용과 작가 괄호 뒤 좌표 복구
- [x] managed distinct variant 자동화 차단
- [x] 반복 비교 그래프 500,000 노드 상한
- [x] 변경 없는 pair cache hit와 실제 read 0
- [x] 기존 1.4.0 엄격 포함판 회귀
- [x] 전체 Python 공개 회귀: `327 passed`
- [x] frontend production build: TypeScript + Vite 성공
- [x] Python compile 및 `git diff --check` 성공

코드 커밋 단계에서는 실제 `txt_house` 파일을 이동하지 않았다. 이후 별도의 actual run 승인으로
수행한 운영 적용과 검증 결과는 다음 절에 기록한다.

## 7. 실제 전체 Folderling 운영 검증

- DB schema는 1.4.1에서 변경하지 않았다. 코드와 실제 DB 모두 schema v14이며 migration은
  `불필요(schema current)`로 확인됐다.
- 실행 전 Doctor 0, 미완료 operation/group 0, active actual run 0, 활성 house 16,759개,
  신규 temp 입력 0개를 확인했다.
- 첫 actual run `actual-f35f11d5-1689-494e-99cb-7319116cb3e2`는 최초 1.4.1 재분석이 기본
  20 GiB 읽기 예산을 소진해 `body_budget_exhausted/deep_check_deferred`로 fail-closed했다.
  파일 mutation 전 중단됐고 run은 `failed`, Doctor 0, 미완료 operation 0으로 종결됐다.
- 파일 이동 없는 유지보수 감사로 같은 cache 규약을 채웠다. 마지막 감사 보고서
  `strong_candidates_20260728_174205_700428.json`은 house 16,759개, 후보 2,622쌍,
  `completed=true`, stop reason 0이다. 모든 house 파일을 현재 후보 생성 규칙에 넣었지만,
  감사기는 설계대로 all-pairs가 아닌 bounded heuristic이므로 `coverage_limited=true`는 유지된다.
- 성공 actual run은 `actual-25e76368-7fca-434d-aecb-671505090679`이며 backup은
  `before_folderling_20260728_174228_808741_65ab4105.sqlite3`, dedup 보고서는
  `dedup_20260728_174325_116085.json`이다.
- 성공 run은 fingerprint cache hit 2,705 / miss 0, pair cache hit 2,622 / miss 0,
  actual auditor read 0으로 실행됐다.
- `ordered_body_match` 28쌍 중 현재 mutation 안전선에 적격인 19권을 복구 가능한
  `trash_bin/ordered_body_duplicates`로 격리했다. 좌표별로 동일 10권, 완전 중첩 8권,
  회차/권 교차 1권이며 mutation 직전 점수는 95.4730~99.8104%였다.
- 나머지 9쌍은 기존 managed variant, legacy marker, `decision_required` 또는 사람 판정 안전선으로
  자동 mutation하지 않았다. 90~95%인 `ordered_body_review` 4쌍도 자동 격리하지 않았다.
- 성공 run의 `user_quarantine` operation 19건은 모두 committed이고 actual run은 `finished`,
  error 없음, 최종 Doctor 0, 미완료 operation/group 0이다.
- 최종 활성 house/DB 지원 파일은 16,740개다. index revision은
  `5e6e35bfea95a2f7ba4cb5dcd404d82e240436e835c37ff3567d9ec90f9c7a2e`이며 project,
  `txt_house`, extension의 `file_index.json` SHA-256은 모두
  `f41e2c25b711c536b20660c23453b629e8d5f6add3d1359ab2f00215c23044b3`로 일치한다.
- `pass/`의 legacy 항목 1개는 기존 정책대로 자동 입고하지 않고 사람 pair 판정 대상으로 남겼다.

## 8. cold-cache 자동 재기준 마감

- 일상 Folderling 감사는 기존 20 GiB 누적 읽기 예산과 파일당 정밀 후보 24쌍 제한을 유지한다.
- actual managed run의 첫 감사가 오직 `body_budget_exhausted` 또는 `deep_check_deferred` 때문에
  불완전하면, 아직 파일 mutation을 시작하기 전에 같은 fingerprint/pair cache를 이어 받아
  64 GiB·파일당 128쌍으로 한 번 자동 재시도한다.
- stale input, decode/구조 오류 등 다른 stop reason이 함께 있으면 재시도하지 않는다. 재기준
  재시도도 완료되지 않으면 기존처럼 Folderling 전체를 fail-closed한다.
- dry-run, pure-plan, 외부에서 주입한 auditor report에는 자동 재기준을 적용하지 않는다.
- 구조화 event에 `auditor_rebaseline`과 `auditor_rebaseline_result`를 남기고, dedup summary에는
  재시도 여부, 최초 stop reason, 최초·합계 read bytes를 기록한다.
- 실제 house를 다시 움직이지 않고 20/64 GiB와 24/128쌍 설정 경계, 자원 stop reason만 허용하는
  회귀를 추가했다. 최종 검증은 공개 회귀 327개, Python compileall, TypeScript/Vite production
  build, `git diff --check`를 통과했다.
- DB schema와 사용자 중복 판정 계약은 바뀌지 않았으며 file_check 버전은 1.4.1로 마감한다.
