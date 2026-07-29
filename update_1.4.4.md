# file_check 1.4.4 — quarantine 전수 해소와 강한 동일성 최종 격리

## 목표

1. 과거 버전부터 누적된 `txt_temp/trash_bin`의 도서를 전부 다시 읽어 영구 폐기, 복원,
   더 나은 대표판 교체로 판정한다.
2. 현재 로직이라면 자동 확정할 TXT/EPUB 강한 동일성이 사람 검토 큐에 계속 쌓이지 않게 한다.
3. Finder 열기·이름 편집창 진입·폴더 이동으로 fingerprint ID와 파일 metadata가 달라져도,
   현재 내용이 다시 증명되면 오래된 quarantine을 안전하게 정리할 수 있게 한다.
4. 오탐으로 격리됐던 별개 작품·고유 외전·판본은 house로 복원하고 판정 이력을 남긴다.

## 버전 범위

- 관리 서버: `1.4.4`
- 관리 UI package: `1.4.4`
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.0` 유지

이번 변경은 제목 정규화 규칙이나 DB 구조를 바꾸지 않는다. mutation 정책, purge 재검증,
운영 감사 도구와 문서를 변경하므로 schema/normalizer cache를 불필요하게 무효화하지 않았다.

## 감사 결과와 판정선

### 복원 20권

다음은 자동 폐기하면 안 되는 관계로 분류했다.

- 별개 작품 3권
  - `내 눈에 스카우트` / `스카우트`: 본문 일치 0.04%
  - `스카우트@사류라` / 다른 `스카우트`: 0.07%
  - `시간의 지배자` / `도시의 지배자`: 0%
- 고유 후기·용어집·외전
  - `비트타는 수양대군 M사 후기`
  - `크루세이더 용어 사전&외전`, `크루세이더외전`
- 월야환담 부별·상하권·권별 판본 4권
- 같은 작품이지만 word-shingle containment가 90% 미만인 판본 10권
  - 도시의 지배자 OV 74.6%
  - 케스핀의 대군주 65.1%
  - SSS급 랭커 회귀하다 56.3%
  - MLB 메이저리그 57.4%
  - 대항해시대VIII 81.6%
  - 던전앤시티 61.2%
  - 캔슬러 59.5%
  - 머더러시티 55.9%
  - 영웅의 주인 61.3%
  - 1217 고려3군단은 복원 후 더 긴 대표판으로 승격

복원은 원래 경로가 temp/action inbox였던 경우에도 `destination_rel`을 명시해 house 안의 안전한
새 위치로만 갈 수 있게 했다. 각 복원은 `user_quarantine_restore` operation과
`distinct_work` 또는 `same_work_distinct_variant` 결정을 남긴다.

### 대표 교체 3건

- `1217 고려3군단`: 1-875+외전 판본을 채택하고 기존 1-846 판본을 폐기
- `[티그리드] 스페셜 메이지`: 검토 큐의 더 긴 판본을 채택
- `백작가 도련님이 미쳐날뜀`: 기존 대표가 99.86% 포함되는 더 긴 검토 큐 판본을 채택

새 판본을 먼저 house에 journal 수용/복원한 뒤 그 operation의 destination evidence를 keep 근거로
사용해 기존 대표를 격리했다. 따라서 중간 실패가 나도 두 판본을 동시에 잃지 않는다.

### 영구 폐기

- 기존 exact quarantine: 1,973건
- 기존 사람 승인 quarantine: 224건
- 복원·검토 큐 해소 과정에서 새로 생긴 quarantine: 51건
- 합계: **2,248건, 8,445,820,651바이트**
- 실행 전부터 파일이 없었던 과거 quarantine 2건: 별도 승인 group으로 DB 정합화

최종 물리 `txt_temp/trash_bin`은 일반 파일 0개, 0B다. DB의 미삭제 `user_quarantine` 20건은
파일이 남은 격리가 아니라 이번에 house로 복원된 원래 operation 이력이다.

## 1.4.4 로직 변경

### 1. 강한 TXT/EPUB 동일성은 사람 큐가 아니라 최종 quarantine

`text_equivalent`와 `epub_equivalent`는 mutation 직전에 다음을 다시 검사한다.

- 현재 review pair와 fingerprint ID
- TXT 정규화 SHA와 실제 정규화 본문
- EPUB strict member payload 또는 reading payload SHA
- 양쪽 실제 파일 identity와 actual manifest

통과하면 `suspected_duplicates`/`house_cleanup_review`가 아니라
`strong_equivalent_duplicates`로 `user_quarantine`한다. 요약에는
`strong_equivalent_quarantine_count`를 추가하고 사람 검토 수인 `review_queue_move_count`에서는 뺀다.
house 전체 cleanup 도구도 같은 함수를 사용한다.

### 2. 형제 권·분할 회차 오탐 차단

강한 동일성 graph와 raw-exact component도 같은 core의 다음 관계는 합치지 않는다.

- `1권` 대 `2권`
- `1-100` 대 `105-200`
- 좌표 없는 합본이 서로 충돌하는 `1권`, `2권` 양쪽을 연결하는 component

반면 완전 중첩, 외전 총량 동등, 회차판/단행본판은 기존 허용 관계를 유지한다. house 대표가 없는
temp-temp strong component도 최종 mutation 대상이 아니라 report-only다.

서로 다른 core인데 raw bytes 전체가 같은 legacy exact 파일은 이름 오기 가능성이 높으므로 기존
정리를 유지한다. 같은 core의 명시적 형제 권 충돌만 fail-closed한다.

### 3. metadata drift를 고려한 purge

exact quarantine 영구 삭제는 과거 keep의 managed/representative 상태나 fingerprint ID 고정을
요구하지 않는다. 현재 활성 house 파일 후보를 raw SHA로 찾고 실제 파일 전체를 다시 해시한다.

오래된 사람 승인 quarantine은 다음 `user_approved_purge_revalidation`을 추가했다.

- 승인 plan SHA를 가진 committed operation group
- 현재 quarantine과 keep이 모두 포함된 actual manifest
- 현재 양쪽 전체 SHA-256과 identity
- 원래 quarantine journal의 경로·크기·SHA-256

이 증거가 있으면 Finder/폴더 이동으로 inode·mtime·ctime이 달라져도 내용이 같은 현재 snapshot을
기준으로 purge할 수 있다. `decode_lossy` fingerprint처럼 cache의 `raw_sha256`이 비어 있어도
`user_quarantine` source/destination journal의 전체 SHA가 현재 bytes와 같으면 허용한다.

### 4. 운영 도구

- `backend/build_quarantine_cleanup_plan_1_4_4.py`
  - 이번 감사의 복원·대표 교체·폐기 판정과 현재 endpoint SHA를 immutable JSON plan으로 만든다.
- `backend/cleanup_quarantine_1_4_4.py`
  - 기본 dry-run
  - 승인 plan SHA 확인
  - disposition과 purge를 서로 다른 백업/actual run으로 실행
  - 중단된 disposition 뒤 purge-only 재개 지원
  - terminal JSON report와 integrity/Doctor/잔여 검증 기록

승인 계획:

- `quarantine_cleanup_plan_20260729_v1_4_4.json`
- canonical plan SHA-256:
  `a23b9e42ddd3b273d1ae0224ac0b1b4d384d0417316b17de0e08af95d78fadd5`

실행 결과:

- disposition run: `actual-3088dc9e-14b2-47de-b8cd-80e632acedc4`
- purge run: `actual-dc70b9ef-0bcf-4eb3-b710-700be295b0d7`
- revalidation group: 35
- missing acknowledgement group: 36
- report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/quarantine_cleanup_1_4_4_20260729_143535_894414.json`

첫 시도는 과거 quarantine의 inode/ctime drift를 원본 journal identity와 완전히 같아야 한다고
요구해 파일 이동 전 중단됐다. 실패 actual run과 group 34를 terminal failed로 닫고 Doctor 0을
복구했다. 두 번째 disposition은 완료됐지만 새 `decode_lossy` 2건의 fingerprint cache에 raw SHA가
없어 purge 전 중단됐다. 두 파일의 현재 bytes, 이번 `user_quarantine` source/destination SHA,
현재 keep을 다시 확인한 뒤 journal SHA를 정식 근거로 허용하고 purge-only로 재개했다.

## 운영 검증

- `txt_temp/trash_bin`: 0개, 0B
- 복원 20권: 전부 `active=1`, `source=house`
- 대표 교체 3건: 새 대표 존재, 구 대표 house 경로 없음
- exact quarantine: 1,973/1,973 `purged_at` 기록
- 선택한 purge 잔여: 0
- unfinished operation: 0
- active queue: 0
- `PRAGMA integrity_check`: `ok`
- Doctor: 0
- index generation 검증 통과
  - physical supported files: 17,612
  - file index entries: 17,877 (`_최근` alias 포함)
  - project/house/extension index SHA-256 일치:
    `de0ee162fc51c8126984c0b34fb0e8ee8fb0078c40c46eda68788f831963d874`
- 후속 전체 Scanner 작업 `42608cfc-3af6-4b5a-b775-6d7602181bcc` 성공
  - 2026-07-29 15:18:42 KST 생성, house 파일 17,612개·폴더 265개
  - 세 인덱스 동기화, integrity `ok`, Doctor 0, 미완료 operation 0
- DB backup retention: 최근 10개 유지
- 전체 테스트의 Scanner API fixture는 `project_root`도 임시 디렉터리로 격리한다. 과거 fixture가
  실제 `extension/file_index.json`을 1개 항목 테스트 인덱스로 덮어쓰던 문제를 수정했고, 전체
  테스트 전후 확장 인덱스 SHA가 위 값으로 유지되는 것을 확인했다.
- 격리 카탈로그는 현재 파일을 소유한 최신 operation과 `purged_at` 삭제 journal만 투영한다. 복원·house
  수용·후속 격리로 이미 해소된 옛 목적지 442건은 `파일 없음`에서 제외하고, 기본 화면을 `실제 보관`으로
  변경했다. 전수 삭제 직후 운영 projection은 실제 보관 0, 파일 없음 0, 이력 없음 0, 삭제 이력 2,252다.

## 회귀 검증

- 강한 TXT/EPUB 자동 최종 quarantine
- strong 결과가 사람 검토 수에 포함되지 않음
- 명시적 형제 권과 disjoint 회차 graph veto
- temp-only strong report-only
- exact keep의 대표/fingerprint 갱신 후 현재 byte copy 기반 purge
- plan-bound 사람 승인 재검증과 metadata drift
- 명시적 house-relative restore destination
- 중단 후 purge-only 재개 증거 검증
- quarantine cleanup 소형 end-to-end fixture

- `PYTHONPYCACHEPREFIX=/tmp/file-check-pycache /opt/anaconda3/bin/pytest -q tests public_tests`
  - **759 passed in 19.48s**
- Python 변경 모듈 `py_compile`: 통과
- `library_frontend` `npm run build`: 통과
  - `file-check-library-ui@1.4.4`
  - Vite production bundle 생성 완료
