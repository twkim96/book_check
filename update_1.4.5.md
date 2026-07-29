# file_check 1.4.5 — house cleanup 안전선과 감사 cache 수명주기

## 목표

1. 사람 검토용 `all-pending` scope가 형제 권·분할 회차·managed identity 안전선을 우회해 자동
   격리하는 경로를 닫는다.
2. strong/weak 관계가 섞인 component에서도 사람 검토 큐 파일의 현재 비교 대상과 판정 근거를
   보존한다.
3. pair 판정 버전 변경을 이유로 내용 의미가 그대로인 fingerprint 전체를 다시 만들지 않는다.
4. warm 감사가 unchanged house 전체를 writer transaction으로 UPDATE하지 않게 한다.
5. ordered-body 정밀 비교의 시퀀스 cache를 마지막 사용 직후 해제하고 실제 peak를 계측한다.

## 버전 범위

- 관리 서버/UI: `1.4.5`
- auditor release: `1.4.5`
- pair policy compatibility: `1.4.2` 유지
- fingerprint policy compatibility: `1.4.2` 유지
- `FINGERPRINT_VERSION`: `5` 유지
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.0` 유지

제목 정규화, TXT/EPUB fingerprint 의미, DB 구조는 바뀌지 않았다. 실제 의미가 그대로인 cache를
의도적으로 재사용하므로 DB migration과 full fingerprint sweep은 필요하지 않다.

## 수용한 리뷰와 적용 범위

### 1. all-pending 안전선

좌표 호환성, unresolved assignment, managed variant identity 검사를 `queueable`에만 적용하지 않고
모든 scope의 plan 생성 전에 강제한다. `all-pending`의 strong 관계는
`strong_equivalent_duplicates`로 자동 격리하지 않고 `house_human_review`로 이동한다.

기본 `queueable`의 current strong 관계는 기존 설계대로 자동 최종 quarantine한다. 이번 수정은
일상 자동화 자체를 약하게 만든 것이 아니라, 사람이 전체 미결 관계를 요청한 확장 scope에 자동
격리 권한까지 따라붙던 우회를 제거한 것이다.

### 2. mixed strong/weak component

모든 간선을 한 번에 연결해 깊은 노드부터 옮기던 계획을 두 단계로 분리했다.

1. `text_equivalent`/`epub_equivalent` strong component의 최종 대표를 먼저 정한다.
2. weak 간선 endpoint를 각 strong component의 최종 대표로 치환한다.
3. 원 pair와 최종 pair가 다르면 현재 fingerprint로 새 open review를 만들고, 원 review ID·원 pair·
   최종 pair를 `strong_component_rebind` evidence에 남긴다.
4. strong 격리 후에도 weak 큐 파일은 살아 있는 최종 대표와 직접 연결된 review를 가진다.

strong component에 보호 파일이 둘 이상 있거나 weak endpoint가 보호 component이면 fail-closed한다.

### 3. fingerprint/pair cache 버전 분리

`_analysis_policy_hash()`에서 변동하는 `AUDITOR_VERSION`을 제거했다. 기존 1.4.2 hash와 byte-for-byte
호환되도록 legacy JSON field 이름과 `FINGERPRINT_POLICY_VERSION="1.4.2"` 값을 유지한다.
pair 쪽도 `PAIR_POLICY_VERSION="1.4.2"`를 별도 lifetime으로 두고
`_pair_configuration_hash()`와 `pair_cache.auditor_version`에 사용한다. 따라서 배포용
`AUDITOR_VERSION="1.4.5"`만 바뀌어도 fingerprint나 pair cache를 무효화하지 않는다. 실제 pair
분류·evidence 의미가 바뀌는 릴리스만 `PAIR_POLICY_VERSION`을 올린다.

실제 정규화나 EPUB fingerprint contract가 바뀌는 릴리스만 `FINGERPRINT_POLICY_VERSION` 또는
`FINGERPRINT_VERSION`을 올려야 한다.

### 4. warm metadata reconcile

`PersistentAuditCache`는 active house/temp file과 house `file_analysis`를 한 SELECT로 읽는다. source,
size, mtime, device, inode, ctime, normalizer version, analyzed name/stat이 모두 같은 house entry는
기존 file ID를 바로 재사용한다. 신규·변경·stale projection과 temp entry만 한 transaction에서
`reconcile_file_metadata()`한다.

감사 통계에 `metadata_reconcile_skips`, `metadata_reconcile_writes`,
`metadata_reconcile_seconds`를 추가했다. warm auditor는 unchanged house의 `last_seen_at`을 갱신하지
않으며 실제 파일 관측 freshness는 Scanner가 소유한다.

### 5. ordered-body cache 수명과 메모리 계측

정밀 비교 전에 각 파일의 남은 sequence 참조 수를 계산한다. 마지막 비교가 끝나면 해당
`NormalizedLineSequence`와 추정 retained byte를 즉시 cache에서 제거한다. 실패·I/O budget 예외에서도
`finally`에서 참조를 해제한다.

다음 통계를 추가했다.

- `ordered_body_cache_peak_items/peak_bytes`
- `ordered_body_cache_evictions`
- `ordered_body_cache_final_items/final_bytes`
- `audit_peak_rss_bytes`

이 방식은 본문 판정, 20/64 GiB read budget, 95% 자동선은 바꾸지 않고 감사 종료까지 모든 sequence를
보유하던 수명만 줄인다.

## 회귀 검증

- `all-pending` 형제 1권/2권·서로 다른 managed variant 차단
- `all-pending` strong의 human review queue 이동
- `near A-B + exact B-C`를 `exact B-C` 후 `near A-C`로 재연결하고 evidence lineage 보존
- release version 변경 시 fingerprint hit 2/2·pair cache hit 유지·본문 read 0
- pair policy version 변경 시 fingerprint hit 2/2·pair cache만 miss·본문 read 0
- unchanged warm house metadata reconcile call 0
- ordered-body 비교 후 cache final item/byte 0, eviction/peak RSS 계측

## 운영 snapshot 성능 검증

원본 `.dedup_state/dedup_decisions.sqlite3`를 SQLite `.backup`으로 `/tmp`에 복사한 뒤 실제
`file_index.json`과 house를 읽어 metadata-only warm 감사를 수행했다. 원본 DB와 도서에는 쓰지 않았다.

- house entry: 17,612
- candidate pair: 3,279
- metadata reconcile: skip 17,612 / write 0 / 0.250초
- 본문 read: 0바이트
- 전체 auditor duration: 11.033초 (`/usr/bin/time`: 11.37초)
- peak RSS: 219,267,072바이트
- completed `true`, stop reason 없음

1.4.2 운영 cache와의 정책 hash도 그대로 유지된다.

- fingerprint policy hash: `d45a6cb3c452e742340605aa07a991dbe6bb2197a60c53af416af9bf7988d467`
- pair policy hash: `d3393217b0dccc6c5d24b544dddbaa256b997ce9501b44582c7c6f05967c947c`

## 최종 검증

- `PYTHONPYCACHEPREFIX=/tmp/file-check-pycache /opt/anaconda3/bin/pytest -q tests public_tests`
  - **766 passed in 20.31s**
- `python -m compileall -q backend`: 통과
- `git diff --check`: 통과
- `library_frontend npm run build`: 통과
  - `file-check-library-ui@1.4.5`
  - Vite production bundle 생성 완료

테스트와 성능 측정은 합성 fixture 또는 `/tmp` DB snapshot만 변경했다. 실제 house/temp 도서 이동·격리·
삭제와 운영 DB migration은 수행하지 않았다.
