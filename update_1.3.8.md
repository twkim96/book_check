# file_check 1.3.8 — 전체 house exact 중복 수렴

- 상태: 구현 및 운영 house 적용 완료
- 기준 커밋: `febdc73` (`feat: infer safe ebook volume groups`)
- 완료 커밋: `533f22c`, `b7fb5c8`
- 적용일: 2026-07-27
- 범위: 기존 Folderling 중복처리 파이프라인의 exact 격리 계약 보강

## 1. 배경

대규모 입고 뒤 기존 house 파일과 바이트가 같은 신규 파일 일부가 `_dup_N` 이름으로 다시 입고됐다.
문제는 `_dup` 접미사 자체가 아니라, exact 중복을 찾고도 keep과 source가 모두 관리 작품에 연결된 경우만
자동 격리하던 조건이었다. 오래된 `unassigned`, `legacy_unresolved`, `decision_required` 행은
`managed_report_only`로 남았고 Folderling은 해당 temp 파일을 신규 파일처럼 house에 입고할 수 있었다.

1.3.8은 접미사나 특정 실행 시점을 기준으로 청소하지 않는다. TXT·EPUB·PDF 전체 house를 기존
Folderling 파이프라인에 다시 넣어 exact, 제목·좌표, TXT 본문, EPUB 내용, 보호·대표·작품 관계 규칙을
동일하게 적용한다.

## 2. 수정한 계약

### 2.1 레거시 exact 중복도 같은 파이프라인에서 격리

- 활성 house의 `unassigned` 파일도 exact source와 keep 후보로 사용한다.
- `legacy_unresolved`와 `decision_required` 파일도 바이트가 완전히 같고 보호·대표가 아닐 때만
  되돌릴 수 있는 exact quarantine 대상으로 허용한다.
- keep이 `decision_required`여도 keep 자체는 이동하거나 관계를 바꾸지 않고 그대로 보존한다.
- managed source는 이전과 같이 같은 managed variant의 대표 keep과 일치할 때만 처리한다.
- protected 파일과 representative 파일은 exact source로 사용하지 않는다.
- 서로 다른 managed variant, 좌표 충돌, manifest·identity 불일치는 계속 fail closed 한다.

### 2.2 fingerprint가 불완전한 파일의 actual 검증

`decode_lossy`와 일부 분석 오류 fingerprint는 정상적으로 존재하지만 `raw_sha256`이 비어 있을 수 있다.
기존 코드는 캐시 부재를 캐시 불일치로 해석해 전체 actual run을 중단했다.

수정 후 계약은 다음과 같다.

1. source와 keep을 mutation 직전에 정규 파일로 다시 확인한다.
2. 두 파일을 끝까지 읽어 현재 raw SHA-256이 같은지 확인한다.
3. 캐시 SHA가 존재하면 현재 SHA와 반드시 일치해야 한다.
4. 캐시 SHA가 없는 것은 불일치로 간주하지 않지만, 실제 양쪽 SHA와 manifest·identity 검증을 생략하지 않는다.
5. current fingerprint 자체가 없는 레거시 행은 현재 identity와 raw SHA에 묶인 immutable `raw_only`
   fingerprint를 먼저 연결한다.
6. 이미 동일한 immutable raw fingerprint가 있으면 새 행을 중복 생성하지 않고 재사용한다.

따라서 SHA 검사를 완화한 것이 아니다. 오래된 캐시 유무와 관계없이 실제 현재 파일 두 개를 다시 읽는
검증을 최종 권한 근거로 사용한다.

## 3. 명시적으로 유지한 안전 경계

- 파일명에 `_dup`이 있다는 이유만으로 격리하지 않는다.
- raw SHA가 다른 EPUB를 같은 권수나 비슷한 이름만으로 삭제하지 않는다.
- EPUB는 기존 내부 content fingerprint와 review 관계를 그대로 사용한다.
- exact가 아닌 본문 유사·포함·metadata-only 관계는 기존 review 정책을 따른다.
- source와 keep의 실제 SHA, actual manifest, dev/ino/ctime/size/mtime, current fingerprint를 mutation 직전
  재검증한다.
- 파일 이동은 기존 backup, actual token, journal, copy-record-consume, Doctor 계약을 그대로 사용한다.
- 격리 파일은 즉시 영구 삭제하지 않는다.

## 4. 운영 적용 결과

전체 house에 동일한 Folderling 파이프라인을 반복 적용해 중간 실패 원인을 수정하고 수렴 여부를 확인했다.

- 첫 실행에서 검증을 통과한 exact 중복 63개 격리 후, 비어 있는 cached SHA 계약 충돌로 안전 중단
- 계약 수정 후 전체 재실행에서 exact 중복 496개 추가 격리
- 보존 keep의 판정 상태 처리 보강 후 남은 exact 중복 8개 추가 격리
- 총 exact quarantine: **567개**
- 최종 활성 house 및 index 지원 파일: **16,848개**
- 최종 실제 raw SHA 완전 중복 그룹: **0개**
- 최종 unfinished operation: **0개**
- 최종 Doctor issue: **0개**

중간에 성공한 63개 operation은 실패 실행과 함께 롤백하지 않았다. 각 operation의 source 부재,
격리 destination 존재, 비활성 DB 행, 활성 house keep 존재를 확인했고 Doctor도 0건이어서 이미 완료된
정상 격리로 유지했다.

## 5. `_dup` 잔여 판정

최종 house에는 `_dup` 이름의 EPUB 3개가 남았다. 이 파일들은 각각 대응 파일과 다음 근거가 달랐다.

- raw SHA-256 불일치
- EPUB 내부 content SHA-256 불일치
- archive member 수 불일치
- 압축 해제 content 크기 불일치
- 서로 다른 protected managed variant

따라서 기존 중복 로직에서 자동 격리할 근거가 없으며, 이름만 보고 제거하지 않고 보존했다. 필요하면
도서 관리 UI의 기존 review·사용자 격리 흐름에서 사람이 판정한다.

## 6. 테스트

추가 회귀 fixture는 다음을 검증한다.

- unassigned temp exact와 unassigned house keep
- house 내부 unassigned `_dup` exact
- 실제 raw SHA가 다른 파일의 격리 차단
- cached raw SHA가 없는 `decode_lossy` exact
- current fingerprint가 없는 레거시 exact의 raw-only fingerprint 준비
- `decision_required` keep을 보존한 채 unassigned 복제본만 격리
- 보호되지 않은 `legacy_unresolved` exact 복제본 격리

최종 검증 결과:

- 공개 Python 회귀: `249 passed`
- Python `compileall`: 통과
- TypeScript/Vite production build: 통과
- `git diff --check`: 통과
- 운영 actual run: `finished`, error 없음
- 프로젝트 index, DB active house projection, 실제 지원 파일 경로: 16,848개 일치
- 전체 repeated-size 후보 재해시: 남은 raw exact 그룹 0개

## 7. 운영상 의미

앞으로 Folderling은 신규 파일만이 아니라 기존 house의 레거시 exact 중복도 같은 실행에서 정리한다.
사용자는 `_dup` 파일만 따로 청소하거나 특정 입고 실행을 기억할 필요가 없다. 반대로 이름이 `_dup`이어도
실제 내용이나 관리 관계가 다르면 자동 삭제하지 않는다.

1.3.8의 완료 기준은 특정 접미사 제거가 아니라 다음 수렴 상태다.

```text
전체 house + temp
→ 기존 exact/본문/EPUB/좌표/관계 판정
→ 검증된 exact만 journal quarantine
→ index 재게시
→ Doctor 0
→ raw exact 그룹 0
```

## 8. 후속 검토 UI 보강

파일 탐색기의 기존 상세 화면은 review 분류와 시각만 표시해 사용자가 어떤 파일과 비교해야 하는지
core title로 추측해야 했다. 같은 review 쌍의 이전 `superseded` 이력까지 단순 합산해 `검토 2`처럼
보이기도 했다.

1.3.8 후속 UI는 다음처럼 바꿨다.

- 검토 필요 목록 행에 대표 상대 파일명과 검토 분류를 직접 표시
- 상대 정보를 기존 관계 셀에 섞지 않고 표 헤더의 독립 `검토 상대` 컬럼으로 분리
- 파일 표의 강제 최소 너비를 제거하고 검토 제목을 한 줄 말줄임 처리해 페이지 폭 안에서 가로 스크롤 없이 배치
- `열린 검토 N건`을 버튼으로 바꾸고 파일 상세로 바로 연결
- 상세 모달에서 pending/deferred 관계만 `검토할 상대 파일`로 우선 표시
- 상대 파일명, 경로, core title, 좌표, 작가, 크기, 활성 상태, fingerprint 상태 표시
- 각 관계에서 `이 파일과 비교`를 누르면 해당 두 파일이 지정된 비교 모달을 즉시 표시
- superseded·종료 이력은 `지난 검토 이력`으로 접어 현재 처리 건수와 분리
- 분류 코드를 `본문 동일 추정`, `다른 범위를 포함한 판본`처럼 사람이 읽을 수 있는 문구로 표시
- 한쪽 파일이 이미 비활성화된 review는 검토 필요 파일 필터와 열린 건수에서 제외
- 앞으로 exact quarantine이 완료될 때 해당 파일의 pending/deferred review를 provenance와 함께 자동 supersede

운영 DB의 pending `contained_version` 사례로 목록의 양방향 상대 표시, 상세 모달의 현재 1건과 과거
이력 분리, 지정된 두 파일 비교 화면 연결을 별도 임시 서버에서 읽기 전용으로 검증했다.

exact quarantine 이후에도 pending으로 남아 있던 비활성 파일 연결 review 535건은 실행 전 DB를 백업한
뒤 `inactive_file_review_cleanup_1_3_8` 근거를 남기고 supersede했다. 활성 파일끼리의 실제 열린 검토
112건과 queue 검토는 유지했으며 정리 후 Doctor issue는 0건이다.
