# file_check 1.4.8 — run-local verified inventory 재사용

## 목표

1. Folderling snapshot이 이미 확인한 17천여 house 파일을 auditor가 다시 index decode·제목 parse·stat하지
   않도록 한다.
2. snapshot 이후 외부 파일 변경, stale normalizer, 다른 root의 inventory는 fail-closed한다.
3. mutation 직전 SHA/identity, final Doctor, 최종 index projection은 그대로 유지한다.
4. standalone auditor와 Scanner fallback 동작은 바꾸지 않는다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.8`
- DB schema: `v15` 유지
- `NORMALIZER_VERSION`: `1.3.1` 유지
- fingerprint/pair policy: `1.4.2` 유지
- fingerprint normalizer compatibility: `1.3.0` 유지

inventory 전달은 본문 fingerprint나 pair 분류 의미를 바꾸지 않으므로 기존 cache generation을 보존한다.

## 구현 계약

### Scanner snapshot receipt

`validate_index_snapshot()`의 단일 house walk가 공개 index entries와 별도로 내부 auditor inventory를 만든다.
내부 payload는 다음에 묶인다.

- schema version 1
- `NORMALIZER_VERSION`
- canonical house root
- DB projection으로 계산한 inventory revision
- 각 파일의 canonical run path, NFC rel path, dev/inode/ctime/mtime/size
- 현재 core title, 작가, 좌표, 완결·외전·분리 marker 분석

공개 `file_index.json`에는 dev/inode/ctime을 추가하지 않는다.

### Auditor handoff

Folderling의 verified snapshot일 때만 `clean_duplicates()`와 최초/재기준 auditor에 payload를 전달한다.
auditor는 계약을 검사한 뒤 `AuditEntry`를 직접 만들며 `file_index.json`을 다시 decode하거나 house 파일을
다시 stat하지 않는다. persistent/read-only fingerprint bulk lookup도 receipt identity를 사용한다.

감사 종료의 `_snapshot_changes()`는 모든 입력을 현재 stat한다. 변경 파일의 pair 결과는 제거되고
`stale` stop reason이 남는다. cache miss 본문 분석과 cache 저장도 기존 identity 검사를 유지한다.

### 명시적 제외

- final index projection: 실행 중 입고·격리·폴더링 결과 때문에 항상 새로 검증
- mutation target SHA/identity: 항상 현재 파일로 재검증
- standalone auditor: 영속 receipt가 없으므로 기존 index/stat 경로
- snapshot fallback: Scanner가 DB/index 불일치를 발견했으므로 inventory를 전달하지 않음
- manifest 전체 공유: temp·unpack·비지원 부속까지 포함하는 별도 증거이므로 이번 버전에 합치지 않음

## 회귀 검증

- 일반 index loader와 verified inventory가 동일한 `AuditEntry` 생성
- verified loader가 `_entry_from_stat()`을 호출하지 않음
- schema/normalizer/root/revision 불일치 차단
- 일반 auditor와 inventory auditor의 pair 결과 동일
- snapshot 뒤 파일 변경 시 `stale`, `completed=false`
- Folderling 결과의 auditor metrics가 `verified_snapshot`을 보고
- full mutation pipeline의 final Doctor/index 계약 유지

## 운영 read-only 기준

동일 프로세스에서 snapshot을 유지한 실제 17,612파일 house-only warm 감사 비교:

- 1.4.7 방식: snapshot 6.100초 + auditor 8.740초 = 14.840초
- 1.4.8 방식: snapshot 6.197초 + auditor 3.166초 = 9.363초
- 절약: 약 5.48초, 전체 구간 약 37% 단축
- fingerprint identity 재-stat 생략: 17,580건
- 본문 read 0, pair hit 3,244, stop reason 0, `completed=true`
- process maximum RSS: 355,663,872 → 340,656,128 bytes
- macOS peak memory footprint: 238,814,912 → 167,167,872 bytes

운영 측정은 `cache_write=False`이며 도서·DB·index·report를 변경하지 않았다.

## 완료 조건

- [x] 전체 Python 회귀: `792 passed in 20.43s`
- [x] frontend 1.4.8 build
- [x] compileall / diff check / normalizer parity 35 cases
- [x] 최종 코드·문서를 독립된 1.4.8 commit으로 고정
