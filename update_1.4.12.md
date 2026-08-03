# file_check 1.4.12 — 닫힌 좌표 보정과 EPUB spine 동일성

## 목표

1. `총 N화`, 소수 특별판 권차, 작가 괄호 뒤 복사본 접미사를 확인된 문법 안에서만 바로잡는다.
2. ZIP asset/압축만 달라 기존 EPUB 비교가 놓친 같은 판본은 실제 spine 본문과 OPF 식별자로 자동 정리한다.
3. 서로 다른 EPUB 권·판본은 OPF의 복수 독립 증거로 검토 큐를 만들지 않고 정상 입고한다.
4. 새 규칙이 다른 house 제목·좌표나 기존 fingerprint generation에 예상 밖 영향을 주지 않음을 전체 inventory로 확인한다.
5. 승인된 기존 큐의 참중복은 복구 가능한 격리로, 오탐은 journaled restore로 정리하고 최종 Doctor/index를 맞춘다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.12`
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3`
- DB schema: `v15` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.12`
- fingerprint/pair normalizer compatibility: `1.3.0` 유지
- archive object/version: `1.4.10` 유지

본문 fingerprint 의미는 바뀌지 않는다. 새 EPUB fallback은 기존 fingerprint 결과가 다른 pair에만 적용되므로
pair cache만 새 generation으로 만들고 100GiB급 fingerprint sweep은 요구하지 않는다.

## 파일명 좌표 보정

- `(?<!문자/숫자)총 N화/회/장/편`은 `1~N` 본편 span이다. `총 N권/부`와 제목 속 `총`은 제외한다.
- 소수 bare 좌표는 1~99, 소수점 셋째 자리 이하이며 닫힌 특별판 qualifier와 inventory 시리즈 문맥이
  모두 있을 때만 rational volume으로 승격한다.
- `N (명시 작가) -2`의 마지막 숫자는 작가 extractor가 괄호 내용을 실제 작가로 독립 확인하고 접미사가
  2~9일 때만 transport copy suffix로 제거한다.
- 사용자 title override, 작가 충돌, 외전 단독, 명시 범위, 날짜형 숫자 등 1.4.11 fail-closed 경계는 유지한다.

## EPUB spine 증거 계약

`inspect_epub_spine_text()`는 no-follow FD로 EPUB를 열고 다음을 검증한다.

- member 수·파일 크기·총 uncompressed·spine member 크기 한도
- 암호화·symlink·중복 NFC member·절대/상위탈출 href 거부
- `container.xml`/단일 OPF, manifest id, spine idref 및 HTML/XHTML 문서의 완전성
- source dev/inode/ctime/size/mtime와 raw SHA의 분석 전후 동일성

자동 중복은 같은 filename core/좌표/작가 경계 안에서 OPF UUID/ISBN이 겹치고, spine 순서의 보이는
본문이 공백 제거 후 정확히 같으며 양쪽 모두 50,000자 이상일 때만 성립한다. 격리 직전에도 저장된
spine hash·글자수와 현재 OPF 식별자 겹침을 다시 계산한다.

서로 다른 판본 자동 통과는 양쪽 식별자가 비어 있지 않고 서로 겹치지 않으며, OPF 제목에서 같은 base의
다른 좌표가 확인되고, 출판사 또는 발행일 집합까지 서로 다를 때만 `different`로 확정한다. 그보다 약한
관계는 계속 `metadata_only` 사람 검토다.

## 검증 범위

- [x] 기존 bare-volume/제목/Folderling 집중 회귀 53건
- [x] spine hash + stable identifier, distinct OPF edition 합성 회귀
- [x] spine 증거의 실제 auto-quarantine 및 mutation-time 재검증 회귀
- [x] 전체 Python 회귀: `845 passed in 21.02s`
- [x] frontend production build / normalizer parity 35건 / compileall / diff check
- [x] 실제 inventory filename 영향 비교와 새 pair read-only 감사
- [x] 승인 큐 journaled restore/quarantine, index 재생성, final Doctor 0

## 실제 서재 정리 대상

- 복원: `비블리아 고서당 사건수첩 2`, `시원찮은 그녀를 위한 육성방법 FD 2`
- 최종 복구 가능 격리: `7번째 환생(총243화)` 중복본, `여동생만 있으면 돼` 동등 EPUB 13개
- 새 spine 증거로 재판정: `드래곤 라자` 동일 권 EPUB과 `비블리아 ... 1 ... -2` 복사본
- 좌표 재투영/시리즈 합류: `옆집 천사님 ... 11.5 (특별판)`

정리 실행기는 자동 판정 범위를 넓히지 않는다. 기존 warning 큐의 파일은 사용자 승인 계획에 현재 SHA가
고정된 경우에만 일괄 격리할 수 있고, 파일명 좌표가 다른 house EPUB은 공통 식별자와 50,000자 이상
spine 본문 완전일치 증거가 있을 때만 강한 중복 정리를 계속할 수 있다. 그 밖의 좌표 충돌은 이전처럼
fail-closed다.

Scanner의 normalizer rekey 백업 여부는 이번 스캔이 실제로 다시 분석할 `active house` 행만 센다.
비활성 retired/quarantine의 과거 normalizer 세대는 보존하되, 그 행만 남았다는 이유로 매 실행마다
수백 MB 상태 DB 백업을 반복 생성하지 않는다.

## 영향성 감사 결과

- 새 정책으로 house 17,687개, 후보/결과 2,824쌍을 감사했다. EPUB `epub_equivalent`는 12쌍이었고,
  최종 정리 뒤 활성 house↔house `epub_equivalent` 열린 검토는 0건이다.
- cold pair 재감사에서 약 17.48GiB를 읽었다. TXT read budget에 걸린 `deep_check_deferred` 16건은
  새 파일명/EPUB 규칙의 영향 대상이 아니며 파일 이동도 일으키지 않았다.
- `총243화` 해석 변경 대상은 승인 큐의 `7번째 환생` 1개뿐이었다. 소수 특별판과 작가 뒤 복사본
  접미사 변경도 각각 `옆집 천사님 11.5 (특별판)`, `비블리아 ... 1 ... -2`로 한정됐다.
- 한때 발견한 `Alter.1`/`Alter.2` 같은 제목 점 구분자 회귀는 실제 Scanner 적용 전에 수정했고,
  해당 경계를 고정하는 회귀 테스트를 추가했다.
- Scanner를 연속 실행해도 비활성 quarantine의 구 normalizer 세대만으로 새 상태 DB 백업을 만들지
  않음을 확인했다. 이 점검 중 생긴 미참조 중복 백업 4개(약 2.9GiB)는 어떤 actual run에서도
  참조하지 않음을 확인한 뒤 제거했고, run별 복구 백업은 모두 보존했다.

## 실제 적용 결과

모든 파일 변경은 root lock, 사전 DB 백업, actual manifest, 파일 SHA/identity 재검증과 operation
journal을 거쳤다. 영구 삭제는 없으며 아래 격리는 모두 복구 가능하다.

1. `드래곤 라자` 1~8권의 loose EPUB 8개를 기존 관리 시리즈 대표와 spine 본문·공통 OPF
   식별자로 대조해 `strong_equivalent_duplicates`로 격리했다.
   - run: `actual-632d56e9-2a2d-4532-95df-60a3d4874560`
   - operations: `14648..14655`
   - backup: `.dedup_state/backups/before_house_cleanup_20260803_013507_691954_cb8132e4.sqlite3`
   - manifest: `.dedup_state/manifests/actual-632d56e9-2a2d-4532-95df-60a3d4874560-8f9b97f2-e3c1-4249-bc61-c76555c1c3f8.json`
2. 오탐 2권을 원래 보관 위치로 복원하고 `_최근` 링크를 만들었다.
   - `ㅂ/비블리아 고서당 사건수첩/비블리아 고서당 사건수첩 2 (미카미 엔).epub`
   - `ㅅ/시원찮은 그녀를 위한 육성방법 FD 2 (마루토 후미아키).epub`
   - run: `actual-c08c8a86-f864-4e90-bf94-46f6c7db5a6c`, operations `14656..14657`
3. 사용자 승인 SHA 고정 계획으로 큐/house 참중복 15개를 `user_discard_quarantine`으로 옮겼다.
   - `7번째 환생(총243화)` 1개, `여동생만 있으면 돼.` 13개,
     `비블리아 고서당 사건수첩 1 (미카미 엔) -2` 1개
   - run: `actual-ee4e30a6-b292-4104-970a-2e40ae54e446`, operations `14658..14672`
4. 전체 library에서 유일한 auto-ready 분권 그룹이던 `옆집 천사님 ... 11.5 (특별판)`을 기존
   1~11권 폴더로 합쳤다. 다른 작품은 이 규칙으로 이동하지 않았다.
   - run: `actual-dc3a6395-a642-492f-997c-718681705c98`, operation `14673`
5. 새 spine 증거가 전체 house에서 추가로 발견한 참중복 3개를 대표와 다시 대조해 격리했다.
   - `무당 귀환 1-152화`, `현령회귀(縣令回歸) 1-172화`, `9번째 적합자 ... [높푸름]`
   - 각 pair의 공통 OPF 식별자와 684,400~905,977자 spine 본문 SHA 완전일치를 mutation
     직전에 다시 확인했다.
   - run: `actual-f1ea668f-855a-4aac-a942-a61ae29bb7fe`, operations `14674..14676`
   - backup: `.dedup_state/backups/before_house_cleanup_20260803_015017_893417_48560825.sqlite3`
   - manifest: `.dedup_state/manifests/actual-f1ea668f-855a-4aac-a942-a61ae29bb7fe-b8a74bf2-6aef-454b-b500-d1c33548f8cc.json`

## 최종 정합성

- full Doctor: `[]`
- active actual runs: 0, unfinished operations: 0
- operations `14648..14676` 29건은 모두 `committed`이며, 원본 잔존·목적지 누락은 0건
- active house DB rows: 17,681
- 프로젝트/house/확장 `file_index.json` SHA-256:
  `2242824c89df566a9a4aef46a2e6fd941a836f6f6e27526a555a6419af062f6f`
- index: `generated_at=2026-08-03T01:50:50+09:00`, `normalizer_version=1.3.3`,
  지원 도서 파일 17,677개와 시리즈 디렉터리 285개
- DB와 인덱스의 파일 수 차이 4개는 EPUB 해제 폴더의 `cover.jpg` 3개와 `hwp` 1개로,
  확장 검색 대상이 아닌 의도된 제외다. index에는 DB에 없는 도서 파일이 0개다.

## UI 후속 마감

- 밝은 테마에서 원색 노랑이 배경과 섞이던 `needs_review`·warning·collision 표시는
  테마의 본문색을 혼합한 `--warning-text`를 공통 사용하도록 바꿨다. 실제 저장된 밝은 테마
  (`background=#e0e0e0`, `text=#1b1a18`)에서 상태 글자의 대비는 약 `5.19:1`이다.
- Folderling 타임라인은 특정 이벤트명에 종속하지 않고, 표시되는 이벤트 중 같은 `phase`가
  연속될 때 하나의 카드와 `×N` 배지로 압축한다. 다른 phase가 끼면 반드시 새 그룹을 시작하며,
  마지막 이벤트의 시각·상태를 표시한다. 마지막 이벤트에 error/fallback이 없으면 같은 묶음에서
  가장 최근에 확인된 error/fallback을 보존한다.
- 실제 작업 `3abe80b3-638a-4bbb-a048-6b683e9d9b4b`에서 표시 대상 214개가 26개 카드로 줄었다.
  `auditor_progress ×14` 다음 `cold cache`를 경계로 새 `auditor_progress ×14`가 만들어졌고,
  별도 연속 phase인 `series_group_item` 163개도 `×163`으로 압축됨을 브라우저에서 확인했다.
- frontend production build와 `git diff --check`를 통과했다. 이 변경은 표시 계층에만 있으며
  Folderling 판정, 작업 이벤트 원본, 상태 DB 및 도서 파일에는 손대지 않는다.
