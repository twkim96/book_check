# file_check 1.4.3 — 전체 시리즈 자동 폴더링

상태: **완료** (2026-07-29)

## 목표

1.4.2까지의 분권 분석은 `auto_ready`를 표시했지만, 실제 이동은 사람이 관리 화면에서 실행하거나
새 파일이 이미 만들어진 작품 폴더에 들어오는 일부 경로에 한정됐다. 이 때문에 과거부터 loose 상태인
분권은 안전 판정이 끝났어도 계속 자모 폴더에 남았다.

1.4.3은 Folderling의 일상 실행을 다음 계약으로 바꾼다.

- 이번 입고에서 영향받은 core만이 아니라 현재 DB의 `auto_ready` **전체**를 적용한다.
- 기존 loose 본권과 이번에 입고된 새 본권을 같은 실행에서 함께 작품 폴더로 만든다.
- 권·부뿐 아니라 같은 core의 회차 분할본도 시리즈 폴더 대상으로 삼는다.
- 흔한 시리즈는 자동화하고, 오탐 위험이 실제로 큰 외전 관계만 사람에게 남긴다.

## 자동/수동 경계

자동 묶음:

- 같은 core, 양쪽에 명시된 작가 충돌 없음
- 서로 다른 권 또는 부 좌표 2개 이상
- 같은 core에서 **시작 좌표가 서로 다른** 회차 분할본 2개 이상
  (`1-100 + 105-200`, `1-100 + 100-200`처럼 범위 중첩 허용)
- 본편 좌표가 2개 이상인 묶음에 외전이 추가된 경우
- 좌표가 서로 다르면 TXT·EPUB·PDF 혼합 허용
- 빠진 중간 권이 있어도 현재 존재하는 좌표끼리 묶음

자동 묶지 않음:

- 단일 본편 좌표 + 외전
- 외전 + 외전
- 양쪽에 작가가 명시되어 서로 다름
- 동일 좌표의 같은 형식 파일, 모호한 좌표, 서로 다른 managed work
- `1-200.txt + 1-200.epub` 또는 `1-180 + 1-200`처럼 시작점이 같은 합본/완결 판본

시작 `0`과 `1`은 모두 작품 처음부터라는 동일 좌표로 취급한다. 동일 시작점 판본끼리는 분권 검토
큐에도 만들지 않고 앞 단계 dedup에 맡긴다. 다만 실제 서로 다른 시작점/권 좌표가 2개 이상 존재하는
시리즈 안에서는 동일 좌표의 TXT·EPUB 병행 포맷을 함께 보존할 수 있다.

외전 예외는 관리 화면의 `allow_side_story_without_two_main_coordinates`를 사람이 직접 선택한 경우에만
해제된다. 자동 실행은 이 override를 절대 사용하지 않는다.

## 실행 순서

```text
1. house + temp 중복 정리
2. 살아남은 temp 파일을 house로 입고
3. DB 전체 시리즈 후보 재계산
4. auto_ready 전부 staging → 작품 폴더 이동 → strong_match 연결
5. file_list/file_index projection 및 최종 Doctor
```

입고 뒤에 전체 시리즈 단계를 두므로, 기존 `1권`이 loose 상태이고 이번 실행에서 `2권`이 들어와도
같은 실행 안에서 둘 다 새 작품 폴더로 이동한다. 실행 시작 manifest에 없던 신규 house 파일은 같은
run의 committed house-producing operation과 현재 destination의 전체 파일 증거가 정확히 일치할 때만
후속 staging source로 허용한다.

## 회차 분할본과 중복 처리의 관계

시리즈 폴더링은 파일을 삭제하거나 동일 내용이라고 선언하지 않는다. `100-200`이 기존 `105-200`을
완전히 포함하고 본문 증거가 충분하면 앞 단계의 dedup이 한쪽을 복구 가능한 격리로 보낸다. 반대로
`1-100`, `105-200`이 있는 실제 분할 cohort에 `100-150`처럼 현재 중복 규칙으로 어느 한쪽을
완전히 버릴 수 없는 파일이 추가되면 세 파일을 보존한 채 같은 작품 폴더로 묶는다. 반면
`100-150 + 100-200` 두 파일만 있으면 시작점이 같으므로 시리즈 폴더를 만들지 않는다.

## 안전·복구 계약

- actual run, 실행 전 backup, immutable manifest, operation journal을 그대로 사용한다.
- 모든 그룹은 전체 staging 복사와 SHA 검증이 끝난 뒤 원본을 이동한다.
- 자동으로 새로 연결한 관계는 `strong_match`, 수동 예외 승인은 `human_decision`으로 기록하고 기존
  사람 결정 origin은 보존한다.
- `_최근` 링크는 이동 전 source를 정확히 가리키는 symlink만 원자적으로 새 destination으로 바꾼다.
- 실패 시 actual run을 failed로 끝내고 다음 실행에서 journal recovery를 먼저 수행한다.
- DB schema는 v15를 유지한다. 이번 패치는 저장 형식을 추가하지 않고 판정·실행 경로만 확장한다.

## 검증 기록 (2026-07-29)

코드 검증:

- 공개 회귀 테스트: `369 passed`
- Python 전체 backend `py_compile`: 통과
- 관리 UI `npm run build`: 통과 (`file-check-library-ui@1.4.3`)
- 분권 목록 성능:
  - 현재 normalizer와 파일 identity가 같은 16,036개 행은 저장된 분석 결과를 재사용
  - 동일 DB revision의 동시 요청은 한 번의 분석으로 합치고, 실제 DB/WAL 변경 전까지 결과 유지
  - 서버 시작 시 목록을 백그라운드에서 미리 준비
  - 실서비스 측정: 첫 화면 0.021초, 7초 뒤 다른 필터 요청 0.033초
- 실자료 적용 전 목적지 preflight:
  - 서로 다른 core가 같은 목적 폴더를 공유: 0건
  - symlink/일반 파일인 잘못된 목적지: 0건
  - 목적 파일명 충돌: 0건
  - 전체 staging 원본 크기 약 9.96GB, 한 그룹 최대 약 353MB

실자료 원버튼 실행(초기 적용 기록):

- actual run: `actual-35573a7a-2144-4f74-963d-d691ab4fff62`
- backup:
  `.dedup_state/backups/before_folderling_20260729_101146_123526_927f9611.sqlite3`
- manifest:
  `.dedup_state/manifests/actual-35573a7a-2144-4f74-963d-d691ab4fff62-4c3fa734-6bd5-486d-9cbd-d20a67162489.json`
- 적용 전: `auto_ready 1,015`, `already_grouped 101`, `review_required 76`, `excluded 5`
- 적용: 1,015작품, 2,309개 구성 파일 중 2,261개 실제 이동
- committed `volume_group_merge`: 2,261건, 미완료 operation 0건
- 적용 후: `auto_ready 0`, `already_grouped 1,116`, `review_required 76`, `excluded 5`
- `_최근`: 이동 전 source를 실제 가리키던 889개 링크를 새 destination으로 재지정했고,
  링크 자체가 없던 1,372개는 만들지 않았다. 다른 target을 가진 항목을 덮어쓴 사례는 0건이다.
- index generation: `9444d00667464385bed46d7774fac76b`
- inventory revision:
  `da44c92a2dc8e4115306ecc6b775d4ed905e83c335a8e4566ff4c2ed92ebc7b7`
- project/house/extension 세 index SHA-256:
  `ac0e73e10567540ce22056a67d84fa1db5febcd809411cb830e59d002bce5833`
- 실행 시간: 전체 134.29초, 시리즈 단계 89.88초
- actual run 상태 `finished`, error `null`, 최종 Doctor 0건

초기 실행 당시 남은 76개는 자동 실패가 아니다. 동일 좌표 충돌, 혼합 좌표, 양쪽 명시 작가 충돌,
disambiguation 충돌 또는 이 버전에서 의도적으로 보류한 단일 본편+외전/외전끼리 관계다.

## 동일 시작점 오분류 교정 및 선택 복원 (2026-07-29)

초기 1.4.3은 `1-200.txt + 1-200.epub`처럼 같은 시작점의 병행 판본도 파일이 2개라는 이유로
시리즈로 오인했다. source-run journal 전체를 새 규칙으로 다시 분류한 결과는 다음과 같다.

- source 그룹 1,015개
- 실제 서로 다른 시작 좌표가 있는 정상 시리즈 96개: 그대로 보존
- 동일 시작점 오분류 919개: 선택 복원
- 시작 `0`과 `1`: 동일 시작점으로 정규화
- 복원 전 충돌, 누락, identity 변경, 후속 수동 operation: 모두 0건
- 확정 plan SHA-256:
  `ca1fa0dc68d71fe090748aee965ec4b53aba7e04a68c4d02c8e1f905a021453d`
- 확정 계획:
  `.dedup_state/reports/1.4.3_false_series_restore_plan_final_20260729.json`

선택 복원 실행:

- actual run: `actual-364300d1-fea2-456c-868d-3751d41e9f05`
- operation group: `33`, action `volume_false_series_restore`, state `committed`
- 실행 전 backup:
  `.dedup_state/backups/before_false_series_restore_20260729_113201_256625.sqlite3`
- manifest:
  `.dedup_state/manifests/actual-364300d1-fea2-456c-868d-3751d41e9f05-7321114a-907c-4980-91cf-3f258db30303.json`
- 원래 경로 복원: 1,840개, path/파일 누락 0건
- source-run 전 관계로 복원: 1,845개, 관계 mismatch 0건
- 잘못 생성된 orphan variant 1,839개, orphan work 915개 제거
- 빈 오분류 폴더 916개 제거; 실행 전부터 내용이 있던 3개 폴더는 보존
- `_최근` 링크: 정확히 이전 destination을 가리키던 788개만 원래 경로로 재지정,
  링크가 없던 1,052개는 만들지 않음
- 현재 분권 요약: `auto_ready 0`, `already_grouped 197`, `review_required 31`, `excluded 1`
  - 동일 시작점 병행 판본은 큐에서 제외
  - 같은 시작점 본편 판본에 외전이 함께 있는 2개 관계는 기존 계약대로 사람 검토
- index generation: `6deb66c5da7e4965b9eec8bcb7fd23b8`
- inventory revision:
  `7d54b12d74be347f477be1fb491fc22a2453ed043600dce7ebb7ba0c3b0972aa`
- project/house/extension index 동기화 완료, 세 파일 SHA-256:
  `4c1a453daada7591bc8a196c2429314538d52ff18009f0694d2731da4a54eed3`
- active house 파일 수: 복원 전 backup과 현재 모두 17,598개
- unfinished operation 0건, actual run `finished`, 최종 Doctor 0건
- 실제 관리 API: 전체 229건, `auto_ready 0`, 응답 0.028초
- 대표 동일 시작점 사례 `100년 묵은 탑 셰프` 검색: 분권 후보 0건
- 정상 `1권 + 2권` 사례 `그때 그후`: 기존 시리즈 폴더와 두 파일 보존 확인
- 최종 결과 로그:
  `.dedup_state/reports/1.4.3_false_series_restore_result_20260729.json`
- 최종 검증 로그:
  `.dedup_state/reports/1.4.3_false_series_restore_verification_20260729.json`
