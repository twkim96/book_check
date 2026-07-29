# file_check 1.4.6 — 제목 접두사와 metadata cut 경계 재감사

## 목표

1. `[갱신 19禁완) 실제 제목 ...]`에서 `갱신`만 `core_title`로 남는 오류를 닫힌 문맥으로 수정한다.
2. 같은 방식으로 실제 제목 앞의 상태·운반 접두사와 제목 내부 숫자를 잘못 metadata로 자르는 house
   파일을 전수 색출한다.
3. 정상 제목 단어를 광범위하게 지우지 않고 Python/Chrome 확장의 결과를 동일하게 유지한다.
4. 수정된 parser로 운영 DB와 repo/house/extension 인덱스를 최신화하되 도서 파일은 변경하지 않는다.
5. 파일명만으로 시리즈를 알 수 없는 예외는 자동 추측 대신 백업·이벤트·보고서가 있는 수동 override로
   정리한다.

## 버전 범위

- 관리 서버/UI/auditor release: `1.4.6`
- `NORMALIZER_VERSION`: `1.3.0` → `1.3.1`
- DB schema: `v15` 유지
- `FINGERPRINT_VERSION`: `5` 유지
- fingerprint/pair policy compatibility: `1.4.2` 유지
- fingerprint/pair normalizer compatibility token: `1.3.0` 유지
- local Chrome extension manifest: `2.5`

이번 변경은 파일명과 `core_title`의 의미만 바꾼다. TXT/EPUB 본문 정규화와 pair 판정 의미는 바뀌지
않았으므로 기존 policy hash를 유지한다.

## 원인

기존 `_LEADING_POST_STATUS_RE`는 `19禁완)`이 문자열 선두에 있을 때만 상태표시로 인식했다.
`갱신 19禁완)`처럼 앞에 수정 상태가 하나 더 붙으면 이 정규식이 실패하고, 후속 metadata cut이
`19禁완`의 `완`에서 실제 제목 전부를 잘랐다. 마지막 noise 제거 후에는 `갱신`만 남았다.

house 전수 검토에서는 같은 구조의 추가 오류도 확인했다.

- `Lv2부터 ...`와 `재벌은 1968부터 ...`: `2부`/`1968부`를 부 단위로 오인
- `k200 장갑차`: `200 장`을 장 단위로 오인
- `24／7 1권`: `7 1권`을 공백 범위로 오인
- `어게인1997 ... -134`: 작가 tag 제거 뒤 `1997-134`를 역방향 회차 범위로 오인
- `좀비묵시록 82-08 001-449`: 제목의 `82-08`을 실제 회차보다 먼저 자름
- `글밈 -2회차-...`: `-2`를 회차 꼬리로 오인해 운반 접두사만 남김
- `꾸롶`, `[작가]` 문맥의 `CSS`·`판`: 변환/운반 접두사가 실제 제목에 포함됨

## 구현 계약

### 상태 접두사

`신규`, `신작`, `갱신`, `업데이트`, `업뎃`, `재업/재업로드`, `수정/수정본`, `교체`, `추가`는 선두
상태표시와 닫는 구두점 문맥에서만 제거한다. 일반 제목 중간의 같은 단어는 보존한다.

후속 hotfix는 `신규 19禁완) 사랑을 먹고 자라는 마법소녀`와 `신작) 일러스트로 일인군단`을 같은
닫힌 상태표시로 처리한다. `회귀한 신규교사`, `신작을 쓰는 천재 작가`는 보존하고,
`(신작-떠따) 치타는 웃고있다`처럼 `신작` 뒤에 복합 태그가 이어지면 `신작-`만 부분 절단하지 않는다.
hotfix 전후 active house 17,616개 raw parser 결과를 비교한 `core_title` 변화는 0개다.

### 운반 접두사

`글밈`은 TXT 첫 줄의 실제 제목, `꾸롶`은 TXT/EPUB 파일 구조로 확인한 닫힌 source prefix다.
`CSS`와 `판`은 EPUB OPF의 `dc:title`로 실제 제목을 확인했지만 일반 제목 단어일 수도 있으므로,
`[명시 작가] + 별도 제목 + 좌표`가 모두 있는 경우에만 제거한다.

### metadata cut

- 단일 단위 뒤에는 일반 문자 경계를 둔다. `완/완결`, `부작`, `합완`, `외전`, `본편` 등 확인된
  붙임 metadata suffix만 예외로 계속 자른다.
- 공백 범위의 첫 숫자 앞이 `/`, `／`, `\\`, `.`이면 앞 숫자를 범위 시작으로 사용하지 않는다.
- `-N회차`는 회차 꼬리가 아니라 제목 시작으로 보존한다.
- 내려가는 단위 없는 숫자 범위는 4자리 제목 숫자 또는 뒤의 별도 실제 범위가 증명될 때 제목으로
  보존한다. `토지 3-1부` 같은 실제 부 좌표는 기존대로 자른다.

### cache 수명

기존 `fingerprints.normalizer_version`은 실제로 본문 정규화 호환 token인데 파일명 parser 버전과 같은
상수를 사용하고 있었다. 이를 `FINGERPRINT_NORMALIZER_COMPAT_VERSION="1.3.0"`으로 분리했다.
pair hash도 `PAIR_NORMALIZER_COMPAT_VERSION="1.3.0"`을 사용한다.

- fingerprint policy hash:
  `d45a6cb3c452e742340605aa07a991dbe6bb2197a60c53af416af9bf7988d467`
- pair policy hash:
  `d3393217b0dccc6c5d24b544dddbaa256b997ce9501b44582c7c6f05967c947c`

파일명 parser만 바뀌어도 합성 warm cache가 fingerprint 2/2, pair 1/1을 재사용하고 본문 read 0임을
회귀 테스트로 고정했다.

## house 영향 분석

1.3.0 인덱스 17,612개 실파일과 1.3.1 결과를 title override를 보존한 채 전수 비교했다.

- 자동 core rekey: 13파일, 10 source key
- 기존 수동 title override가 이미 새 parser 결과와 같은 `Lv2부터 ...`: 12파일
- 자동 target collision: 2작품
  - `데뷔 못 하면 죽는 병 걸림`: 기존 2파일 + CSS prefix EPUB 1파일
  - `괴담에 떨어져도 출근을 해야 하는구나`: 기존 TXT 1파일 + 판 prefix EPUB 1파일
- 예상하지 않은 unchanged target 충돌: 0
- 파일명에 작품명이 없는 `은하영웅전설 제 N권 ...`: 10파일 수동 override

자동 rekey 대상은 다음 계열이다.

- `글밈` 1
- `꾸롶` 4
- `CSS` 1, `판` 1
- `24／7` 2
- `k200 장갑차` 1
- `어게인1997` 1
- `재벌은 1968부터` 1
- `좀비묵시록 82-08` 1

`제 N권 부제`는 파일명만으로 `은하영웅전설`을 복원할 수 없다. 일반 `제 N권` 전체를 같은 작품으로
합치는 오탐이 더 위험하므로, 확인된 부모 폴더 10파일만 `title_override_json`과
`work_management_events(action=manual_core_title_override)`에 기록했다.

## 운영 적용

### 사전 계획

- 보고서:
  `.dedup_state/reports/normalizer_impact_1_4_6_preflight_20260729_225021.json`
- plan SHA-256:
  `fd846acb112d2ef0f4ff70edf568c2b97535a763342480cd4e7645bcb6607ad7`
- 사전 Doctor: 0

### DB와 index

Scanner가 1.3.1 재분석 전에 다음 백업을 남겼다.

`.dedup_state/backups/before_normalizer_rekey_20260729_225039_022461_b8c32a16.sqlite3`

Scanner catalog rekey 결과는 9 key migration, 성공 플랫폼 metadata 1건 보존, 오래된 실패 결과 26건
폐기다. 수동 override 전에는 별도 백업을 남겼다.

`.dedup_state/backups/before_normalizer_manual_override_1_4_6_20260729_225141_961416_e2bd77d2.sqlite3`

수동 override는 10파일, `제 → 은하영웅전설` catalog rekey 1건이며 오래된 실패 결과 3건을 폐기했다.
실행 보고서는 다음에 있다.

`.dedup_state/reports/normalizer_manual_override_1_4_6_20260729_225146.json`

코드·DB·index·감사·서비스 결과를 합친 최종 실행 보고서는 다음에 있다.

`.dedup_state/reports/normalizer_rekey_1_4_6_execution_20260729_230545.json`

최종 DB snapshot index를 repo, `txt_house`, `extension` 세 surface에 배포했다.

- generation: `0b85809a71794ccbb788a25a0e142680`
- inventory revision:
  `ac77b16d86e4ff308eb249941a80997d6da8768c00fdabe2349919e3c4ae71fd`
- file entries: 17,612 / 전체 entries: 17,877
- 세 `file_index.json` SHA-256:
  `752d6702bba86ea06884ff7190bd24fdaf127b2f8235136531a8c2bb6831535f`
- metadata sync: current 17,612 / stale 0 / missing 0 / index-missing-DB 0
- unindexed active 4는 지원 도서가 아닌 unpack 부속 `cover.jpg` 3개와 `hwp` 1개다.

도서 파일 이동·이름 변경·격리·삭제는 0건이다.

## 중복 오탐 영향 검증

변경 영향 core만 모은 30파일 targeted auditor는 9 pair를 끝까지 검사했다.

- completed: `true`, stop reason 없음
- `different`: 4
- `metadata_only`: 5
- strong/near-identical 자동 격리 후보: 0
- input change: 0
- read: 123,543,601바이트

같은 `은하영웅전설` core가 된 본편과 외전의 같은 권 번호 4 pair는 본문이 모두 `different`로
확인되어 오탐 자동 격리 근거가 생기지 않았다. `백작과 하녀` 단권/외전과 `데뷔 못 하면 죽는 병
걸림` EPUB 판본은 metadata-only에 머물렀다.

추가 전체 house read-only 감사는 17,612파일, 3,282 pair를 검사했지만 기존 최신 policy fingerprint가
3,148파일에만 있어 14,068,151,009바이트를 읽었고 `deep_check_deferred` 5건으로 전체 완료 판정은
내리지 않았다. input change는 0이다. 이는 1.4.6 대상 core 검증 실패가 아니며, 위 targeted 감사는
별도 2GiB 예산 안에서 완료했다. 기존 cache hit 2,606건과 fingerprint miss 0은 제목 버전 상승으로
호환 cache가 무효화되지 않았음을 보여 준다.

### cache-writing full fingerprint sweep

후속 유지보수는 공용 house/temp 잠금과 SQLite online backup 아래 current-policy cache 부채를 실제로
backfill했다.

- 사전 Doctor/미완료 operation/active actual run: `0/0/0`
- 백업:
  `.dedup_state/backups/before_full_fingerprint_sweep_1_4_6_20260729_233536_457654.sqlite3`
  (`sha256=a9a08ec1f7268f823a6c889b703c27f2b2dfd37a2656ac56a570e71e3759ec57`)
- sweep 전 current-policy hit: 2,606/17,580
- 처리: TXT 12,293 + EPUB 2,681 = 14,974파일
- 새로 지속된 fingerprint: 14,973, 최종 hit 17,579/17,580
- 진행 로그 기준 fingerprint read 83.29GiB, 정밀 비교 282쌍 2.23GiB
- `deep_check_deferred`: 5 → 0, input change 0

strict full sweep의 `completed=false`는 cache backfill 실패가 아니라 안전하게 fingerprint를 만들 수 없는
실파일 70개 때문이다. 기존 TXT 69개는 `decode_lossy`이며, 남은 EPUB
`고대산거종전일상 1-363화 完난독화,원본특수폰트 [수운계].epub`은 ZIP 안에 같은
`OEBPS/content.opf`가 8번 들어 있어 `EPUB contains duplicate normalized member names`로 fail-closed했다.
중복 증거의 기준 OPF를 임의 선택하지 않는다.

- JSON report:
  `/Users/twkim/Documents/txt_temp/dedup_logs/strong_candidates_20260729_235657_528241.json`
- execution log:
  `.dedup_state/reports/full_fingerprint_sweep_1_4_6_execution_20260730_000350.json`

실제 일상 경로와 같은 cache-writing warm 감사는 11.391초, 본문 read 0, fingerprint hit 17,579,
pair-cache hit 3,244, `completed=true`, stop reason/input change 없음으로 끝났다. 따라서 malformed/손실 파일을
억지로 성공 처리하지 않으면서도 구세대 cache 때문에 전체 후보 본문을 다시 읽던 문제와
`deep_check_deferred`는 해소됐다. 도서 이동·이름 변경·격리·삭제는 0건이다.

## 최종 검증

- `pytest -q`: **783 passed in 19.60s** (최종 재실행)
- Python/Chrome fixture: **47 passed**, parity **35 cases**
- `compileall backend public_tests`: 통과
- `library_frontend npm run build`: 통과 (`file-check-library-ui@1.4.6`)
- `PRAGMA integrity_check`: `ok`
- Doctor: 0
- 미완료 operation/group/actual run: 0/0/0
- index generation validation: 통과
- 전용 PM2 `server-control--book_check`만 재시작, `/health` database `ok`, version `1.4.6`
- 재시작된 서버 `/api/review/titles/preview`에서 `신작) 일러스트로 일인군단` →
  `일러스트로일인군단` 확인
