# file_check 1.4.2 — 강한 중복 입고 차단과 시리즈 폴더 정상화

- 적용일: 2026-07-29
- 시작 기준: `b71253a` (`docs: finalize 1.4.1 library cleanup`)
- DB schema: v15 유지
- app/server: 1.4.2
- auditor: 1.4.2, fingerprint: v5 유지

## 1. 목표

1. 1.4.1 대규모 입고에서 house에 남은 확정 중복과 잘못 보류된 파일을 증거에 따라 정상화한다.
2. 원시 SHA·TXT 정규화 SHA·EPUB 내부 동일성이 확정된 파일은 일반적인 제목 차이 때문에
   warning이나 사람 판정으로 보내지 않는다.
3. `1부 1권`/`2부 1권`, 번호 외전, 병행 포맷, 웹연재 합본 공존, 작가 누락을 시리즈 폴더 로직에
   정확히 반영한다.
4. 실제 파일 이동은 backup, manifest, actual run, operation journal, 복구 가능한 격리와 JSON 로그를
   남기고 최종 Doctor/index 검증까지 닫는다.

## 2. 발견된 원인과 수정

### macOS 경로 정규화 때문에 exact 중복이 다시 입고됨

auditor가 temp 파일을 DB에 reconcile한 뒤 deduplicator가 경로를 `abspath`만으로 다시 찾았다.
macOS 파일시스템이 NFD로 노출한 경로와 DB의 NFC canonical path가 달라 `file_id=None`으로 남았고,
확정 SHA 중복이 `managed_report_only`가 된 다음 Folderling 2단계에서 house로 입고됐다.

- DB와 입력 경로를 모두 `decision_store.canonicalize_path()`로 비교한다.
- 여러 managed 대표와 충돌해 정말 report-only가 된 temp 파일은 Folderling 입고 단계에서도 명시적으로
  차단한다.
- 원시 SHA 전체 재검증이 성공하면 파일명 좌표 충돌은 격리를 막지 않는다.

### EPUB의 실제 읽기 내용은 같은데 편집기 metadata만 달랐음

기존 EPUB hash는 ZIP 시각·압축 방식은 무시했지만 OPF 제목/수정일과
`META-INF/calibre_bookmarks.txt`까지 내부 내용으로 포함했다. 실자료의 동일 3권 두 파일은 40개
읽기 member가 모두 같고, 한쪽에 Calibre bookmark 1개가 추가되며 OPF 설명 metadata만 달랐다.

- strict EPUB content hash는 그대로 보존한다.
- strict hash가 다를 때만 2차 reading-payload hash를 계산한다.
- Calibre bookmark와 OPF `<metadata>`만 제외하고, manifest·spine·guide·본문·이미지·CSS·navigation은
  모두 정확히 같아야 `epub_equivalent`가 된다.
- mutation 직전 같은 reading-payload SHA를 다시 계산하며 spine 순서가 달라지면 자동 격리를 거부한다.
- 2차 payload 검사 직전에 읽기 예산이 끝나면 `metadata_only`로 cache하지 않고
  `body_budget_exhausted`로 남긴다. 64 GiB 재기준은 같은 pair를 다시 계산하며, reading-payload
  pair 계약 hash만 바꾸므로 이미 만든 TXT/EPUB 기본 fingerprint는 재사용한다.

### 새 강한 판정이 과거 review 행에 반영되지 않았음

같은 fingerprint pair가 과거 `metadata_only` open review를 가지고 있으면, 새 auditor가
`epub_equivalent`를 증명해도 review 저장기가 단순히 "이미 존재"로 종료했다. 감사 보고서에는 강한
그룹이 생겼지만 mutation 단계는 `epub_equivalent` review ID를 찾지 못해 이동이 0개가 됐다.

- 같은 current fingerprint의 pending/deferred review는 review ID와 상태를 유지하면서 최신
  classification/evidence로 갱신한다.
- pair cache hit에서도 이 갱신을 수행하므로 규칙 버전만 강해진 관계가 다음 실행에 다시 사람 검토로
  남지 않는다.
- `metadata_only -> epub_equivalent` 실회귀를 공개 테스트로 고정했다.

### 부+권 좌표와 시리즈 cohort가 평탄화됨

- 기존 `_coordinate_key`는 권 좌표에서 부 번호를 버려 `1부 1권`과 `2부 1권`을 충돌시켰다.
- 권별 세트와 같은 core의 웹연재 합본을 한 cohort로 합쳐 이미 잘 만들어진 권별 폴더도
  `mixed_coordinate_kind`로 보였다.
- 동일 권의 EPUB/PDF 병행 보관, `외전 1`/`외전 2`도 중복 좌표로 접혔다.
- 괄호 토큰을 종류별로 수집해 `(1부 完) ... [연역]`에서 실제 뒤쪽 작가 대신 상태 문구를 작가로
  읽었다.

복합 부+권 좌표, 번호 외전, 확장자별 병행 포맷, 권별 cohort 우선, 괄호의 실제 텍스트 순서,
명시 작가끼리만 충돌하는 규칙으로 수정했다.

### queue 복원 직후 분석 snapshot이 과거 경로에 남았음

`user_queue_accept_to_house`와 `user_queue_restore`가 files 경로·identity는 바꾸면서 `file_analysis`의
새 ctime을 즉시 갱신하지 않았다. 따라서 같은 actual 흐름에서 바로 시리즈를 계산하면 복원된 권이
일시적으로 cohort에서 빠질 수 있었다. 두 mutation 모두 이동 직후 새 house 경로의 분석을 upsert해
후속 시리즈 판정과 인덱스 projection이 전체 재스캔 없이 현재 상태를 사용하도록 수정했다.

### Finder metadata가 legacy pass 검토 수에 포함됨

`txt_temp/pass/.DS_Store` 한 개가 실제 도서 검토 항목처럼 집계됐다. legacy pass도 일반 intake와 같은
`should_exclude_file` 규칙을 사용해 Finder metadata를 제외한다. 실제 legacy pass 도서는 0개다.

## 3. 1.4.1 결과에 대한 적용 전 증거

- 적용 전 Doctor issue 0, SQLite `integrity_check=ok`, active actual run 0
- 활성 house DB row 17,645
- active house 원시 SHA 중복: 44개 discard
- `volume_coordinate_conflicts` 보류: 40개
  - 현재 house 파일과 원시 SHA가 같은 확정 중복: 38개
  - 잘못 충돌한 고유 파일: `천마군림 2부 1권`, `천마군림 2부 2권` 2개
- 직전 두 대규모 입고 run과 연결된 시리즈 중 현재 자동 정상화 가능: 24개 core
- 별도 검토였던 `마도구사 달리아는 고개 숙이지 않아 3권` 두 파일은 39개 member가 완전히 같고,
  한쪽의 Calibre bookmark와 OPF 설명 metadata만 달라 reading-payload SHA가
  `3b8d70bc90a707456a2409442c4f31893d2849a3cea8d56104f4648cec27b972`로 일치했다.

읽기 전용 전체 감사는 17,641개 지원 파일과 3,369개 후보 관계를 확인했고, 구 cache를 쓰지 않는
pure-plan에서 1,265개 정밀 비교를 9.66 GiB까지 수행한 뒤 `deep_check_deferred`로 fail-closed했다.
이는 파일 mutation 실패가 아니며 1.4.2 actual run에서는 auditor 1.4.2 cache를 기록하고 기존
20/64 GiB 자동 재기준 계약으로 완료한다.

## 4. 자동/수동 경계

자동 격리:

- 현재 원시 SHA 동일
- TXT 정규화 SHA 동일
- EPUB strict content SHA 동일
- EPUB reading-payload SHA 동일
- 1.4.0 포함판 또는 1.4.1 95% 순서형 본문 계약 충족

자동 격리하지 않는 예외:

- 보호 파일 또는 서로 다른 managed work/variant 결정과 충돌
- 하나의 입력이 여러 managed 대표 identity와 동시에 일치
- EPUB spine/manifest/본문/이미지 등 읽기 payload 차이
- 같은 확장자·같은 권이지만 위 strong 증거를 만들지 못함
- 명시 작가 충돌 또는 모호한 좌표가 있는 확률적 포함판/95% 관계

## 5. 회귀 검증

- macOS NFD/NFC file ID 재연결
- filename 좌표가 충돌하는 raw exact 자동 격리
- warning queue의 raw exact 최종 격리
- strict/reading-payload EPUB temp 및 house-house 자동 정리
- OPF spine 차이 fail-closed
- 익명 작가 신규 권별 폴더와 명시 작가 충돌
- `1부 1권`/`2부 1권`, `외전 1`/`외전 2`
- 동일 권 병행 포맷과 웹연재 합본 공존
- 같은 fingerprint의 open review가 더 강한 최신 판정으로 갱신됨
- queue accept/restore 직후 file analysis identity 갱신
- legacy pass의 `.DS_Store` 제외
- 전체 공개 회귀, Python compile, frontend production build, `git diff --check`

최종 결과는 공개 테스트 `355 passed`, Python compileall, frontend 1.4.2 production build,
`git diff --check`를 통과했다.

## 6. 실제 적용 결과

### 첫 전체 run과 예산 경계 보정

- actual run: `actual-be0e4f72-ed99-452a-bd5f-2bd1fb809da9` (`finished`)
- backup: `before_folderling_20260729_003725_885953_634ceb13.sqlite3`
- manifest: `actual-be0e4f72-ed99-452a-bd5f-2bd1fb809da9-03d8ff1e-0754-468d-9cf8-219484604f79.json`
- dedup report: `dedup_20260729_004724_351874.json`
- 원시 SHA exact 44개 격리, strict EPUB equivalent 4개 격리, warning 0, 신규 입고 0
- 4개 EPUB은 `잃고 나서야 깨달았다`, `대역무사`, `Re:제로 37권`, `Re:제로 41권`이다.
- auditor 3,369쌍 완료, 20 GiB 첫 구간 뒤 64 GiB 자동 재기준 성공
- 최종 index 17,593개 지원 파일, revision `e8db89cc5137...`
- run 직후 Doctor 0, SQLite `integrity_check=ok`, 미완료 operation 0

첫 구간의 마지막 200개 EPUB 관계는 strict 분석 뒤 reading-payload 예산이 모자랐고, 그중
`마도구사 달리아 3권`도 payload가 같은데 `metadata_only` cache로 남은 것을 확인했다. 파일 mutation
오류나 오탐 격리는 없었지만 자동 재기준이 이 pair를 다시 읽지 못하는 결함이므로 위 2절의
예산-deferred 계약으로 수정하고 회귀를 추가했다.

### 후속 전체 재감사와 마지막 EPUB 격리

- `actual-a61f1f56-abd7-4f74-a712-e0f3e3007679`: 3,259쌍을 20 GiB 안에서 완료하고
  `마도구사 달리아 3권`을 `epub_equivalent`로 다시 증명했다. 이 run에서 위 stale review 갱신 결함이
  드러났으며 파일 이동은 0개였다.
- 갱신 결함 수정 뒤 `actual-deb1c76c-b110-4f62-8788-1d8e529a85d1`에서 덜 적합한 `03권` 1개를
  `suspected_duplicates`로 격리했다.
- 보고서: `dedup_20260729_085207_548019.json`, `dedup_20260729_085754_716683.json`

### 좌표 충돌 큐 40개 정상화

- run: `actual-58059666-4056-40de-a4cf-0173862258d6` (`finished`)
- raw SHA가 현재 house keep과 같은 38개는 모두 `exact_quarantine`으로 이동했다.
- 38개 모두 원래 warning 경로가 사라졌고, quarantine 파일이 존재하며, current house keep SHA와
  일치한다.
- 고유본 `천마군림 2부 1권`, `2부 2권`은 `user_queue_accept`로 house에 복원했다.
- 40개 operation 모두 `committed`다.

### 시리즈 폴더 정상화

- `actual-039a618a-204e-4987-b6d2-ccf7a5365b60`은 `강철의 용병` 24개 move를 전부 커밋한 뒤
  일회성 결과 코드가 실제 반환 키 `unchanged` 대신 `noops`를 읽어 run 자체는 `failed`로 닫혔다.
  파일/DB operation 24개는 모두 committed이고 Doctor·미완료 operation은 0이었다.
- 키 수정 뒤 `actual-53186257-6936-4334-9bf6-80ccb201de2e`에서 나머지 24개 core의 180개 파일을
  이동하고 이미 목적 폴더에 있던 4개도 같은 work 관계로 연결했다.
- 이때 전체 index fallback이 queue accept 분석 stale을 드러냈다. 원인 수정 뒤
  `actual-74b5d3c3-a990-4a4c-9d79-f875404c645a`에서 `천마군림 2부 1·2권`을 기존 폴더에 합쳤다.
- 최종 25개 core, 210개 파일이 모두 `already_grouped`, parent 1개, blocked reason 0이다.
- 실제 `volume_group_merge` move는 206개이며 모든 operation이 committed다.
- 복구 보고서: `v1_4_2_library_repair_20260729_090507_284477.json`,
  `v1_4_2_library_repair_20260729_090821_090139.json`

### 최종 클린 감사

- run: `actual-4f754f0f-128b-40c9-aa41-8b7ca8dbb713` (`finished`)
- dedup report: `dedup_20260729_091105_129289.json`
- 지원 도서 17,594개, 후보 관계 3,257쌍 완료, stop reason 없음
- exact 0, strong/auditor group 0, suspect move 0, warning 0, blocked strong 0
- active house 17,598개 중 지원 도서 17,594개는 index와 정확히 일치한다. 제외 4개는 cover JPG 3개와
  HWP 1개다.
- Doctor issue 0, SQLite integrity `ok`, foreign key issue 0, 미완료 operation/group 0,
  active actual run 0, 활성 `volume_coordinate_conflicts` queue 0
- legacy pass 실제 도서 0 (`.DS_Store`만 존재하며 집계 제외)
- 최종 통합 감사 로그:
  `file_check_1_4_2_final_audit_20260729_091605_234968.json`

1.4.2 전체 적용에서 house에서 격리된 도서는 raw exact 44개, strict EPUB 4개,
reading-payload EPUB 1개로 총 49개다. 과거 warning queue에서는 raw exact 38개를 추가 격리했고,
잘못 보류된 고유본 2개를 복원했다. 모든 격리는 삭제가 아니라 복구 가능한 quarantine이다.
