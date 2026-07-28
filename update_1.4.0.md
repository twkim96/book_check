# file_check 1.4.0 — 버전형 전수 중복 감사와 house 재기준

- 상태: 구현·운영 재기준 완료
- 시작 기준: `43ce099` (`fix: finalize v1.3.9 relationship choices`)
- 적용일: 2026-07-28
- 범위: 격리 불변식, 제목 무관 콘텐츠 중복 회수, 증분 fingerprint 전수 감사,
  실제 `txt_house` 정리와 수동 예외 기록

## 1. 목표

1. 최신 로직을 거치지 않은 기존 house 도서를 한 번 전수 재기준한다.
2. 이후에는 파일 identity와 감사 버전이 바뀐 항목만 다시 읽는다.
3. 제목이 달라도 전체 TXT 정규화 hash 또는 EPUB 내부 콘텐츠 hash가 같으면 후보로 회수한다.
4. 강한 의심 도서는 삭제하지 않고 journal quarantine에 보관하며 keep/discard 근거와 복구 경로를 남긴다.
5. 같은 작품의 짧은 구간판이 더 긴 최신·완전판에 포함됨을 현재 본문으로 엄격히 증명하면 자동 교체하고,
   cross-title near나 좌표·본문 증거가 불충분한 contained 예외만 수동 판단으로 남긴다.

## 2. 구현 단계

### A. mutation 안전선

- [x] 다중 managed 대표 충돌 파일은 exact mutation에서도 report-only
- [x] mutation 계층에서 다중 대표 exact 격리 재차 차단
- [x] 폴더 격리 전 house/temp/queue 전체 variant 구성원 확인
- [x] 적격 house 대체 대표가 없으면 preview 차단
- [x] Doctor가 active managed variant의 대표 누락 탐지

### B. 감사 회수율과 증분 실행

- [x] 명시적 `--full-fingerprint-sweep`로 house TXT/EPUB 전체를 versioned cache에 backfill
- [x] 기본 실행은 신규·변경 temp TXT/EPUB을 먼저 fingerprint
- [x] raw/TXT normalized/EPUB content digest를 제목 무관 전역 join
- [x] 앞부분이 달라도 bounded 내부·후반 anchor 검사 계속
- [x] 3~4글자 core의 adaptive gram 회수 또는 명시적 coverage 기록
- [x] full sweep 실패·예산 초과·identity 변화는 `completed=false`로 fail-closed

### C. 실제 house 재기준

- [x] 새 auditor를 full sweep으로 실행하고 구조화 report 저장
- [x] 자동 회수 가능한 exact EPUB review 12권을 journal quarantine
- [x] cross-title/near strong-anchor 예외 50권을 수동 JSON plan으로 quarantine
- [x] 확인된 false-positive 3건을 house로 복원하고 `distinct_work` 기록
- [x] 같은 시작 회차·더 큰 종료 회차와 5개 분산 고유 본문 anchor로 증명된 짧은 판본 4권 자동 격리
- [x] 모든 격리에 원본·keep·근거·operation/run ID·복구 경로 기록

### D. 완료 검증

- [x] 전체 Python 회귀와 frontend production build
- [x] final Doctor 0
- [x] unfinished operation/group 0, active run 0
- [x] 최초 재기준 뒤 실제 house·DB·`file_index.json` 지원 파일 16,788개 일치
- [x] 최신판 자동 교체 뒤 실제 house·DB·`file_index.json` 지원 파일 16,784개 일치
- [x] 최종 warm auditor에서 fingerprint cache hit 16,767 / miss 0 / 실제 read 0 확인
- [x] 최종 exact 0, near는 판본 결정 3쌍·수동 보류 2쌍만 남는지 재감사
- [x] 동일 로직 재실행에서 최신판 자동 교체·warning·기타 이동 모두 0건 확인

## 3. 자동화와 수동 예외의 경계

- 자동 strong: 전체 TXT normalized hash 동일, EPUB 내부 콘텐츠 hash 동일.
- 자동 최신판 교체: TXT의 exact core가 같고, 같은 시작 회차에서 종료 회차가 더 크며, 현재 normalized
  전체 접두 SHA 또는 본문 전역의 고유 anchor 5개가 같은 순서·간격으로 일치하는 관계.
- 작가 차단: 양쪽에 작가가 모두 있고 서로 다를 때만 적용한다. 한쪽 또는 양쪽의 작가 누락은 흔한
  정상 입력이므로 무시하며 warning 사유로도 삼지 않는다.
- review-only: near-identical, 시작·단위·managed variant가 충돌하는 관계, 고유 anchor가 부족하거나
  순서·간격이 흔들려 중간 삽입·누락 가능성을 배제할 수 없는 contained 관계.
- 수동 예외: 서로 다른 `core_title`이지만 분산된 여러 고유 4 KiB 본문 anchor가 순서대로 일치하는 관계.
- 보존: decode 실패, EPUB 구조 오류, 부분본 여부가 불명확하거나 좌표가 충돌하는 관계.
- 실제 bytes 제거는 하지 않는다. 모든 정리 대상은 `txt_temp/trash_bin` 아래 복구 가능한 격리로 이동한다.

## 4. 전수 사전 판정 요약

- 기준 inventory: 지원 도서 16,847개(TXT 14,423 / EPUB 2,408 / PDF 16), 인덱스와 실제 파일 일치.
- raw SHA 동일 house 그룹: 0.
- TXT normalized 전체본문 동일: 8그룹. 자동 선택에 맡기지 않고 본문 표제·실제 회차·작가 메타를 비교해 keep을 수동 확정했다.
- EPUB 내부 콘텐츠 동일: 12그룹. 현재 콘텐츠 SHA와 좌표가 일치하는 관계만 자동 격리 대상으로 삼는다.
- 강한 TXT 본문 관계 49간선: 기존 후보 밖 22간선(20 component)은 21권 격리, 기존 후보 안 27간선은 15권 격리·10쌍 판본 관계 보존·2쌍 unresolved 보존으로 판정했다.
- false-positive queue 3권: 앞/뒤·분산 본문 유사도가 모두 매우 낮아 서로 다른 작품으로 복원 계획에 고정했다.
- 최초 실행 계획: 수동 격리 44권, false-positive 복원 3권, `same_work_distinct_variant` 10쌍, unresolved 보존 2쌍. 모든 실행 항목은 양쪽 현재 raw SHA-256에 묶였다.
- full sweep 뒤 새로 남은 미기록 near 6쌍은 line-level 공통 본문 99.70~99.95% 또는 순서형 4 KiB anchor 44/64를 재검증해 별도 follow-up plan으로 격리했다. 최종 수동 격리는 50권이다.
- 보존 예외: decode-lossy TXT 68권, 짧은 TXT 7권, 구조 오류 EPUB 1권, PDF 16권. decode 대체 판독에서는 추가 동일본문 그룹이 없었다.

독립 안전 리뷰에서 다음 회귀도 재현해 수정했다.

- preload 뒤 같은 크기·mtime으로 파일이 교체되어도 dev/ino/ctime identity 변화로 stale 처리하고 strong 결과를 폐기한다.
- managed 대표 분석도 공용 `ReadBudget`을 사용하며 보고된 read bytes 밖의 선행 전체본문 읽기를 금지한다.
- 수동 plan은 schema v2와 양쪽 expected SHA가 없으면 실행할 수 없고, 작업 충돌·복원 목적지 중복·같은 managed work의 잘못된 distinct 복원을 mutation 전에 차단한다.
- 실제 이동 전에 no-clobber intent JSON을 먼저 쓰고, 실제 run manifest가 그 파일 identity를 고정한다. 성공/실패 terminal JSON은 별도 cleanup stem으로 남긴다.
- false-positive 복원 operation은 승인 plan SHA·input plan SHA·review 방향/fingerprint·intent SHA를 가진 operation group에 직접 연결한다. 중단 재개는 이 provenance와 목적지 inode/SHA가 모두 일치할 때만 허용한다.
- `_최근` 링크는 파일 이동·`distinct_work` 결정·처분 기록이 끝난 뒤 생성하며, 중단 뒤 남은 링크는 이전 intent와 정확한 대상이 일치할 때만 재사용한다.
- 실제 실행 JSON에는 plan·본문 근거·fingerprint·backup·run/operation·최종 Doctor/index 상태를 기록한다.

## 5. 운영 증거

- 구현 커밋: `e72d3ff` (`feat: rebaseline library dedup in v1.4.0`)
- 운영 중 발견한 stale review 선택 수정: `91a6885` (`fix: bind restores to current fingerprints`)
- 검증: Python `695 passed`, frontend `npm run build`, `compileall`, `git diff --check` 통과.
- full sweep 전 DB backup:
  `.dedup_state/backups/before_v1_4_0_full_sweep_20260728_123351_cd554485.sqlite3`
- full sweep report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/strong_candidates_20260728_125923_861536.json`
  (`sha256=4cb6ae01700aa6c27062dc43341760655288a8af64a008b4bb939eb654719284`)
  - eligible 16,831 / available 16,831 / analyzed 16,830 / known failed 69
  - fingerprint preparation read 100,184,909,872 bytes, 전체 read 111,095,688,362 bytes
  - TXT normalized exact 8쌍, EPUB content exact 12쌍
  - decode/EPUB 구조 오류 때문에 fail-closed `completed=false`; 파일 이동 0
- 최초 warm report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/strong_candidates_20260728_130034_999343.json`
  (`sha256=ff3a662e72cb6564820d6753f197887a019d7c0569895d52783907a6a6a6690d`)
  - fingerprint cache hit 16,830 / miss 0, pair cache hit 2,717, read 55,979,564 bytes
- 보존 판본 10쌍 decision 1~10:
  `/Users/twkim/Documents/txt_temp/dedup_logs/manual_house_cleanup_1_4_0_20260728_130713_114421.json`
  (`sha256=bc5d91322f6dcae31f7cfa7b71c2406af67300ebdf8b612b6417dd80091b92a9`)
- stale review preflight 실패 로그(이동/operation 0):
  `/Users/twkim/Documents/txt_temp/dedup_logs/manual_house_cleanup_1_4_0_20260728_130855_998406.json`
  (`sha256=0a5b2edfcc78b9e39736b6aa1c5929737cc9171df274e7b080ce7e5df1757416`)
- 최초 수동 plan:
  `/Users/twkim/Documents/GitHub/python/test/file_check/manual_duplicate_plan_20260728_v1_4_0.json`
- 수동 44권 격리 + false-positive 3권 복원 terminal report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/manual_house_cleanup_1_4_0_20260728_131223_725629.json`
  (`sha256=9c163260f25b68b0109bc7c6ba86d7eb1ab935b8cc2b948ce676110ab44c7475`)
  - restore run `actual-87a32087-56a4-46bb-a0ff-32d2847f3179`
  - discard run `actual-986053a3-5814-4190-a57b-1ee1bc5bcb43`
  - restore decisions 11~13, operations 3620~3666
- exact EPUB 12권 자동 격리 terminal report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/house_cleanup_1_4_0_20260728_131519_698948.json`
  (`sha256=5b8d927a32e10dcf11bb2a86fdb09549815c6520ad13e6d270d68994b3388528`)
  - run `actual-adcfe9fc-b3ab-4340-89c8-779da4830d50`, operations 3667~3678
- 후속 near 6쌍 plan:
  `/Users/twkim/Documents/GitHub/python/test/file_check/manual_duplicate_plan_followup_20260728_v1_4_0.json`
- 후속 near 6권 격리 terminal report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/manual_house_cleanup_1_4_0_20260728_132401_567028.json`
  (`sha256=6414bee425371f9aba55e725e1d4d9aa17e0f88d24a6ce040819908fab7de76d`)
  - discard run `actual-e2f364a5-c654-41d0-bf17-c11695519b2d`, operations 3679~3684
- 최초 재기준 완료 auditor report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/strong_candidates_20260728_132521_653576.json`
  (`sha256=39621e0bf234d2d7356a263ae9962b9342bb54a3348735f5869e59610d823257`)
  - `completed=true`, stop reason 없음, actual read 0
  - TXT/EPUB exact 0, 미기록 near 0
  - near 5쌍 = 결정된 보존 판본 3쌍 + 수동 보류 2쌍
  - contained-version 59쌍은 이 시점 구현에서 모두 review-only였음
- 최신판 자동 교체 사전 dry-run:
  `/Users/twkim/Documents/txt_temp/dedup_logs/dedup_20260728_140912_888557.json`
  (`sha256=dca6a75fd47e1b2cf26059873bb74e6b9ed86bddb7abe6273fa24ae2a148a5b7`)
  - 지원 도서 16,788개, auditor 후보 2,656쌍
  - fingerprint cache hit 16,771 / miss 0, pair cache hit 2,656 / miss 0, actual read 0
  - 같은 시작 회차의 더 긴 TXT에 분산 고유 anchor 5개가 순서대로 일치한 4건만 교체 예측
  - 작가: 양쪽 누락 2건, 양쪽 명시·호환 1건, 짧은 쪽 누락 1건
- 최신판 자동 교체 DB backup:
  `.dedup_state/backups/before_v1_4_0_contained_upgrade_20260728_141200.sqlite3`
- 최신판 자동 교체 실제 실행 report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/dedup_20260728_141326_974695.json`
  (`sha256=d796cdbf66db38cf482afd609bfcc512eaac24a5199afe7df7a8b5a700cd95a6`)
  - run `actual-6b8f5c3a-3881-496a-8d98-3d68d4fb5123`, operations 3685~3688
  - `contained_version` 4건 모두 `status=superseded`, `user_quarantine committed`
  - 짧은 범위 `1-1210`, `1-522`, `1-138`, `1-135`를 각각 더 긴 `1-1350`, `1-587`,
    `1-425`, `1-251` 판본으로 교체
  - 각 기록에 양쪽 normalized SHA·길이, 고유 anchor 5개, anchor offset span 0~19,
    review/operation ID와 최종 격리 경로 보존
- 정리 후 동일 로직 dry-run:
  `/Users/twkim/Documents/txt_temp/dedup_logs/dedup_20260728_142527_150381.json`
  (`sha256=ecdeb8c9028580605a6ccdee182b97a028357e8bbf94a374020c2885461d6bff`)
  - 지원 도서 16,784개, auditor 후보 2,652쌍
  - fingerprint cache hit 16,767 / miss 0, pair cache hit 2,652 / miss 0, actual read 0
  - 추가 최신판 교체 0, exact mutation 0, 기타 suspect 이동 0, warning 0
  - 현재 active pair-cache의 나머지 contained-version 55쌍은 엄격한 자동 교체 조건 밖이라 보존
- 최종 상태:
  - 지원 도서 16,784개(TXT/EPUB/PDF), 실제 disk = DB = index
  - baseline 16,847 - 수동 격리 50 + 복원 3 - EPUB 격리 12 - 최신판 격리 4 = 16,784
  - 새 operation 69개 모두 committed, source 잔존 0, destination 누락 0
  - Doctor 0, active run 0, unfinished operation/group 0
  - raw exact group 0, normalized/content exact group 0
  - index generation `bfc1bce1fce64492a558efe2e89c3654`
    (`2026-07-28T14:13:45+09:00`)
  - 실제 bytes purge 없음. 모든 66권은 `txt_temp/trash_bin` 아래 복구 가능.
