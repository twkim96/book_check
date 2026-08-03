# file_check

`file_check`는 개인 txt/epub 라이브러리를 스캔하고, 제목·편수·본문 근거로 중복 후보를
검토 큐에 모으며, 승인된 변경만 journal 기반으로 적용하는 Python 도구입니다.

## 안전 원칙

- 기본 중복 검사는 dry-run입니다.
- 애매한 항목은 자동 삭제하지 않고 review queue로 보냅니다.
- 짧은 판본이 긴 판본에 포함됨을 강하게 증명한 경우는 애매한 항목이 아니며, 긴 판본을
  채택하고 짧은 판본을 복구 가능한 격리함으로 자동 이동합니다.
- 실제 이동은 상태 DB, backup, manifest, 일회성 승인, 복구 기록을 사용합니다.
- `.dedup_state`, 인덱스, 로그와 실제 라이브러리는 Git에 포함하지 않습니다.

### 같은 house 경로 재입고 안전 계약 (DB schema v15)

제목 정리로 house 파일을 temp에 되돌린 뒤 새 intake identity를 만드는 경우, 과거 file row는
실제 경로가 아닌 `.dedup_state/retired_paths/<file_id>/...` 가상 경로에 보존해야 합니다. operation과
fingerprint에는 원래 경로가 그대로 남으므로 이 이동은 이력 삭제가 아니라 `canonical_path` 점유 해제입니다.

- schema v15 migration은 과거 버전이 남긴 **비활성 + committed 제목 requeue** 실경로만 가상 경로로
  옮깁니다. 출처가 불명확한 비활성 행은 추측해서 고치지 않습니다.
- house 입고는 파일시스템 목적지가 비어 있는지 확인한 뒤, DB에서 같은 `canonical_path`를 점유한 행도
  **파일 복사 전에** 검사합니다. 증명된 과거 제목 tombstone만 은퇴시키고, 그 밖의 점유자는 source를
  temp에 그대로 둔 채 fail-closed합니다.
- 파일 복사는 끝났지만 DB 반영이 끊긴 `fs_done` 복구는 destination의 inode·size·mtime·SHA-256이
  journal과 모두 같고, 경쟁 행에 committed 제목 requeue 이력이 있을 때만 원복 없이 입고를 확정합니다.
  그 외에는 기존 보수적 rollback을 유지합니다.
- 제목 requeue 뒤 남은 `_최근` 링크가 끊어져 있더라도 그 링크의 기록된 target이 이번 house 목적지와
  정확히 같으면 링크를 지우거나 다시 만들지 않고 그대로 재사용합니다. target이 다르거나 일반 파일이면
  기존 항목을 보존하고 입고를 중단합니다.
- Folderling 성공 판정은 최종 Doctor 0건까지 포함합니다. index fallback이 성공했더라도 미완료 operation이
  있으면 다음 actual run을 시작하지 않습니다.

이 계약의 회귀는 `public_tests/test_legacy_canonical_path_recovery.py`에서 과거 tombstone migration,
이동 전 DB 충돌 차단, journal 기반 `fs_done` 합류 복구를 함께 검증합니다.

## 동일 작품 자동 중복 정리 계약 (1.4.15)

1.4.1의 순서형 본문 계약, 1.4.2의 강한 EPUB/입고 보정, 1.4.3의 전체 시리즈 자동 묶음,
1.4.4의 강한 동일성 최종 격리·격리 수명주기, 1.4.5의 house cleanup 안전선·감사 cache 계약,
1.4.6의 제목 접두사·메타데이터 cut 경계, 1.4.7의 warm cache·검증 receipt,
1.4.8의 run-local inventory 재사용, 1.4.9의 검증된 상태 백업 cold archive,
1.4.10의 archive/review 경합·경로 안전 보강, 1.4.11의 숫자 권 문맥 추론,
1.4.12의 닫힌 좌표 보정·EPUB spine 동일성, 1.4.13의 legacy 실행 차단·분권 분석/복구,
1.4.14의 상태 저장소 모듈 ownership, 1.4.15의 플랫폼·stale override·staging recovery 계약은
각각 [`update_1.4.1.md`](update_1.4.1.md),
[`update_1.4.2.md`](update_1.4.2.md),
[`update_1.4.3.md`](update_1.4.3.md),
[`update_1.4.4.md`](update_1.4.4.md),
[`update_1.4.5.md`](update_1.4.5.md),
[`update_1.4.6.md`](update_1.4.6.md),
[`update_1.4.7.md`](update_1.4.7.md),
[`update_1.4.8.md`](update_1.4.8.md),
[`update_1.4.9.md`](update_1.4.9.md),
[`update_1.4.10.md`](update_1.4.10.md),
[`update_1.4.11.md`](update_1.4.11.md),
[`update_1.4.12.md`](update_1.4.12.md),
[`update_1.4.13.md`](update_1.4.13.md),
[`update_1.4.14.md`](update_1.4.14.md),
[`update_1.4.15.md`](update_1.4.15.md)에 기록합니다.

> **이 절은 구현 세부사항이 아니라 프로그램의 핵심 설계 계약입니다.**
> 오탐 방지를 이유로 모든 포함 관계를 다시 수동 검토로 돌리면 안 됩니다. `file_check`를 만든
> 목적은 흔한 최신화 중복을 자동 정리하고, 정말 판단할 수 없는 예외만 사람에게 남기는 것입니다.

대표 동작은 다음과 같습니다.

```text
기존 house: 판타지소설 1-100.txt
신규 temp : 판타지소설 1-150 외전포함.txt

검증 성공 → 1-150을 house에 채택
           → 1-100을 txt_temp/trash_bin/superseded_versions에 격리
```

반대로 house에 `1-150`이 있는데 신규 `1-100`이 들어오면 신규 짧은 파일을 자동 격리합니다.
이미 house에 두 판본이 함께 남아 있어도 같은 현재 증거가 성립하면 긴 판본만 남깁니다. 실제 bytes를
바로 삭제하지 않으며, 격리 파일과 operation journal로 복구할 수 있습니다.

### 1.4.4 강한 동일성 최종 격리 계약

다음 증거는 제목·회차·완결·외전 표기보다 강합니다. 현재 두 파일을 mutation 직전에 다시 읽어
같음이 재확인되면 신규 temp뿐 아니라 과거 house-house 중복도 자동으로 복구 가능한 격리에 보냅니다.

1. 원시 파일 전체 SHA-256이 같음
2. TXT 정규화 본문 SHA-256 또는 EPUB의 모든 내부 member 이름·내용 SHA-256이 같음
3. EPUB의 읽기 payload가 같고 차이가 Calibre bookmark와 OPF의 제목·UUID·수정일 같은 설명
   metadata뿐임. 이 경우에도 manifest, spine 순서, navigation, 본문, 이미지, CSS 등 실제 읽기
   자원은 전부 같아야 합니다.

TXT 정규화 본문 또는 EPUB strict/reading payload가 같은 파일은 이제 사람 검토용
`suspected_duplicates`에 머물지 않습니다. mutation 직전 현재 파일을 다시 읽어 같음이 확인되면
`trash_bin/strong_equivalent_duplicates`의 **최종 복구 가능 quarantine**으로 이동하고 DB에는
`user_quarantine` journal을 남깁니다. `review_queue_move_count`에는 포함하지 않으며
`strong_equivalent_quarantine_count`로 별도 집계합니다.

강한 본문 증거도 명시적인 **같은 작품의 서로 다른 분권 좌표**를 합치지는 않습니다. 같은 core의
`1권`과 `2권`, `1-100`과 `105-200`처럼 서로 다른 형제 권·분할 구간은 중복이 아니라 시리즈 구성일
수 있으므로 자동 격리를 차단합니다. 반면 `1-100`과 `1-150`, `1-150`과
`1-130 외전 1-20`, `1-150화`와 `1-9권`처럼 기존에 허용한 중첩·외전 총량·회차판/단행본판 관계는
현재 본문 증거가 같으면 자동 처리합니다. 좌표가 없는 합본이 `1권`과 `2권` 양쪽을 이어 하나의
동일성 component로 만드는 것도 pairwise 좌표 검사로 차단합니다.

서로 다른 core의 파일이 원시 바이트 전체까지 같은 legacy exact 사례는 이름이 잘못 붙은 같은 파일로
처리할 수 있습니다. 단, 같은 core에서 명시적 형제 권 좌표가 충돌하면 원시 SHA가 같아도 report-only로
남겨 도서 소실을 우선 방지합니다. 보호 파일, 서로 다른 managed work/variant에 이미 사람 결정으로
연결된 파일, 하나의 입력이 여러 managed 대표와 동시에 같아지는 관계, house 대표 없이 temp 파일끼리만
같은 관계도 자동 처리하지 않습니다.

macOS의 NFD 파일명과 DB의 NFC canonical path는 같은 경로로 연결합니다. Finder에서 파일을 열거나
이름 편집창에 들어간 뒤 inode·mtime·ctime이 바뀐 경우에도 시각만으로 keep을 정하지 않고, 현재
bytes와 본문/EPUB payload를 다시 읽어 결정합니다. 오래된 quarantine을 영구 삭제할 때도 과거
fingerprint ID나 대표 assignment가 그대로인지 요구하지 않습니다. exact 격리는 현재 house의 원본
바이트 사본을 다시 해시하고, 사람 승인 격리는 승인 계획 SHA·현재 manifest·양쪽 현재 SHA-256을 묶은
`user_approved_purge_revalidation` journal이 있을 때만 metadata drift를 허용합니다.

강한 동일성으로 확정되지 않은 TXT 판본에는 1.4.1의 서로 보완하는 두 자동 확정 경로가 있습니다.

1. **엄격한 포함판 경로(1.4.0 유지)**: 같은 단위·시작 회차에서 더 큰 종료 회차가 선언되고,
   짧은 전체 본문이 긴 본문의 정규화 접두 SHA와 같거나 본문 전역의 고유 앵커 5개가 같은
   순서·간격으로 일치하면 긴 판본을 채택합니다.
2. **순서형 본문 경로(1.4.1 추가)**: 아래 좌표 관계 중 하나이고, 버릴 판본의 충분히 긴 본문
   대부분이 남길 판본에서 같은 순서로 확인되면 최종 중복으로 확정합니다.
   - 동일 좌표: `1-150` 대 `1-150`
   - 완전 중첩: `1-100` 대 `1-150`
   - 본편·외전 합계 동등: `1-150` 대 `1-130 외전 1-20`
   - 회차판·단행본판: `1-150화` 대 `1-9권`

순서형 본문 경로는 숫자만 보고 `150화 = 9권` 같은 환산식을 만들지 않습니다. 단위가 다르면
양쪽이 1에서 시작한다는 후보 조건만 사용하고, 실제 동일성은 전역 본문 증거로만 확정합니다.
본편과 외전도 좌표 의미를 합쳐 버리지 않고 각각 보존한 채 총 범위가 같은지만 후보 생성에
사용합니다.

순서형 자동 격리는 TXT 두 파일에 대해 다음 조건을 **모두** 만족할 때만 실행합니다.

1. 정규화한 `core_title`이 정확히 같습니다.
2. 위 네 좌표 관계 중 하나이며 좌표가 모호하지 않습니다.
3. 양쪽에 작가가 모두 명시된 경우 서로 충돌하지 않습니다.
4. 버릴 쪽 정규화 본문이 100,000자 이상입니다.
5. NFC와 공백 차이를 제거한 본문 줄을 정확히 비교했을 때, 버릴 쪽 글자 가중치의 **95% 이상**이
   남길 쪽에서 같은 순서로 일치합니다. 64회를 넘게 반복되는 줄은 동일성 증거에서 빼되 전체
   분모에는 남겨 상투 문구가 점수를 부풀리지 못하게 합니다. 비교 그래프도 500,000개 노드로
   제한하며 초과하면 자동 확정을 포기하므로 반복문이 많은 파일에서도 메모리가 무한히 늘지 않습니다.
6. 한 덩어리로 이어진 불일치가 버릴 본문의 약 2%를 넘지 않습니다(아주 짧은 구간에 대해서만
   2,048자 최소 허용). 전체 점수만 높고 특정 장이 통째로 개정된 판본은 자동 격리하지 않습니다.
7. 현재 fingerprint와 파일 identity를 실행 직전에 다시 확인하고 같은 증거를 재계산합니다.
8. 관리 중인 work/variant 또는 사람이 저장한 보존 관계가 서로 충돌하지 않습니다.

95%는 서로 다른 작품을 가르는 일반 유사도 점수가 아닙니다. **정확히 같은 core와 호환 좌표가
먼저 성립한 긴 TXT 판본**에만 적용하는 최종 본문 증거입니다. 90% 이상 95% 미만은 강한 의심으로
`warning` 검토에 남기고 자동 격리하지 않습니다. 실자료 보정에서 이미 보존한 개정판 관계도
96~100%까지 나온 사례가 있어 90%를 자동선으로 쓰지 않습니다.

남길 파일은 다음 순서로 고릅니다.

1. 같은 단위의 완전 중첩이면 선언 범위가 넓은 판본
2. 그 외에는 기존 `choose_keep`: 비교 가능한 편수, 5%를 넘는 본문 길이 차이, 완결 표기,
   짧고 깔끔한 파일명, 안정적인 파일명 순서

기존 house 대표를 새 temp 판본으로 교체할 때는 새 판본을 먼저 journal 입고하고 대표 관계를
넘긴 다음 기존 파일을 격리합니다. 반대로 신규 판본이 덜 적합하면 신규 파일만 격리합니다.
어느 경우에도 실제 bytes를 즉시 삭제하지 않습니다.

작가 정책은 의도적으로 다음처럼 적용합니다.

- **양쪽에 작가가 모두 적혀 있고 서로 다를 때만 자동 교체를 차단합니다.**
- 한쪽 작가만 없거나 양쪽 모두 없으면 작가 정보는 판정에 사용하지 않습니다.
- 작가 미표기는 흔한 정상 입력이므로 warning 사유가 아닙니다.

Finder에서 파일을 열거나 이름 수정창에 들어간 영향도 최신판 선택 근거로 쓰지 않습니다. `atime`은
identity 비교에서 제외하고, `mtime`·`ctime`만 바뀌어도 기존 본문 판정을 그대로 믿지 않고 stale로
폐기해 현재 bytes를 다시 분석합니다. 즉 메타데이터 시각이 더 새롭다는 이유로 책을 격리하지 않습니다.

일상 Folderling 감사의 누적 읽기 예산은 20 GiB입니다. actual run에서 cold cache 때문에
`body_budget_exhausted` 또는 `deep_check_deferred`만 발생하면 파일 mutation 전에 기존 cache를 이어 받아
64 GiB·파일당 정밀 후보 128쌍으로 한 번 자동 재기준합니다. stale input, decode/구조 오류 등 다른
중단 사유가 함께 있거나 재시도도 완료되지 않으면 기존처럼 fail-closed합니다. dry-run과 사람이 주입한
auditor report에는 이 자동 재시도를 적용하지 않습니다.

다음 관계는 review/warning에 남깁니다.

- 양쪽에 명시된 작가가 서로 다름
- 시작·종료 회차가 충돌하거나 모호하며 위 네 좌표 관계로 설명되지 않음
- 서로 다른 managed work/variant 관계
- 반복 문구 때문에 분산 앵커가 고유하지 않음
- 앵커 순서·간격이 흔들려 중간 삽입, 누락 또는 별도 판본 가능성이 있음
- 순서형 본문 일치가 90% 이상 95% 미만이거나 연속 불일치 구간이 너무 큼
- decode 실패, EPUB 구조 오류처럼 현재 본문 증거를 만들 수 없음

위 목록의 좌표·작가 조건은 포함판/95%처럼 확률적 판본 추론에 적용됩니다. 현재 원시 SHA, TXT
정규화 SHA, EPUB strict/reading-payload SHA가 완전히 같은 경우에는 1.4.4 강한 동일성 계약이 먼저
적용되지만, 같은 core의 명시적 형제 권·서로 떨어진 분할 회차 차단은 이 경우에도 유지됩니다.

같은 current fingerprint pair에 과거의 약한 open review가 있어도 새 auditor가 더 강한 판정을
증명하면 classification과 evidence를 현재 결과로 갱신합니다. 예를 들어 과거 `metadata_only`였던
EPUB이 reading-payload 검사로 `epub_equivalent`가 되면, 사람 검토에 계속 남기지 않고 같은 실행의
강한 중복 mutation 대상으로 사용합니다.

엄격한 포함판 교체는 dedup JSON의 `contained_upgrade_count`와 `status=superseded`에, 순서형
자동 격리는 `ordered_body_quarantine_count`와 `status=ordered_duplicate`에 기록합니다. 후자는
양쪽 normalized SHA-256, 전체·일치 글자 수, 일치율, 최대 연속 불일치, 좌표 모드, house ingest
operation ID, quarantine operation ID와 최종 `trash_bin/ordered_body_duplicates` 경로를 남깁니다.
관리 화면의 `본문 95% 자동 격리`에서도 복구 가능한 파일을 확인할 수 있습니다.

이 정책을 수정할 때는 반드시 `public_tests/test_contained_version_upgrade.py`와
`public_tests/test_ordered_body_dedup_1_4_1.py`의 동일 좌표·중첩·외전 합계·회차/권 교차·95% 경계·
연속 개정·작가 충돌·managed variant 보존 회귀를 함께 변경하고, README에 자동/수동 경계가 왜
바뀌는지 명시해야 합니다. 원시/정규화/EPUB payload 경계를 바꿀 때는
`public_tests/test_legacy_exact_cleanup.py`, `public_tests/test_epub_duplicate_audit.py`,
`public_tests/test_strong_equivalent_autocleanup_1_4_2.py`도 함께 검증해야 합니다.

### 1.4.4 격리 수명주기와 전수 정리

복구 가능한 quarantine과 영구 삭제는 별도 단계입니다. 자동 중복 판정은 파일을 즉시 unlink하지 않고
항상 quarantine journal을 먼저 만듭니다. 영구 삭제는 사용자가 선택한 plan SHA를 명시적으로 승인한
뒤 다음을 다시 만족해야 합니다.

1. 격리 파일의 현재 SHA-256이 원래 journal 또는 plan-bound 재승인 journal과 같습니다.
2. 남길 파일이 현재 house에 활성 상태로 있고, DB identity와 실제 파일 identity가 같습니다.
3. exact 격리는 남길 현재 house 파일의 전체 raw SHA가 격리 파일과 다시 같습니다.
4. 오래된 사람 승인 격리의 대표가 이동·교체됐다면 현재 keep과 격리를 같은 actual manifest에서 읽고
   승인 plan SHA를 가진 committed operation group으로 다시 묶습니다.
5. 삭제 직전 백업과 manifest를 만들고, 각 unlink를 독립 `quarantine_purge` operation으로 기록합니다.
6. 종료 조건은 `PRAGMA integrity_check=ok`, Doctor 0, 선택한 quarantine 잔여 0입니다.

2026-07-29 전수 감사에서는
[`quarantine_cleanup_plan_20260729_v1_4_4.json`](quarantine_cleanup_plan_20260729_v1_4_4.json)의
SHA `a23b9e42ddd3b273d1ae0224ac0b1b4d384d0417316b17de0e08af95d78fadd5`를 승인해 다음을 적용했습니다.

- 별개 작품·고유 외전·90% 미만 판본 20권 복원
- 더 긴 판본 3건 대표 교체
- 검토 큐 50권 전부 판정(47권 중복 격리, 2권 대표 채택, DB 밖 1권 중복 격리)
- 격리 2,248개, 8,445,820,651바이트 영구 삭제
- 이미 실체가 없던 과거 격리 2건을 별도 승인 group으로 정합화
- 최종 `txt_temp/trash_bin` 일반 파일 0개, Doctor 0, integrity check `ok`

감사 재현 도구는 `tools/legacy/cleanup_quarantine_1_4_4.py`, 당시 계획 생성기는
`tools/legacy/build_quarantine_cleanup_plan_1_4_4.py`입니다. 실행 결과 원본은
`txt_temp/dedup_logs/quarantine_cleanup_1_4_4_20260729_143535_894414.json`에 남습니다.

### 1.4.5 house cleanup 안전선과 감사 cache 계약

일상 Folderling과 `run_house_cleanup_once.py`의 기본 `queueable` scope에서 현재
`text_equivalent`/`epub_equivalent`가 증명되면 1.4.4 계약대로 자동 최종 quarantine합니다. 반면
`all-pending`은 사람이 미결 관계 전체를 훑기 위한 확장 scope이지 자동 격리 권한을 넓히는 옵션이
아닙니다. 이 scope의 strong 관계도 `house_human_review`로만 이동합니다. 두 scope 모두 다음 안전선을
동일하게 강제하며, 옵션으로 우회할 수 없습니다.

- 명시적 형제 권·분할 회차 좌표가 충돌하거나 범위가 모호하면 이동하지 않습니다.
- `legacy_unresolved`/`decision_required` 파일은 자동 처리하지 않습니다.
- 양쪽이 managed라면 같은 variant에 속할 때만 하나의 중복 component로 처리합니다.
- 보호 대표를 포함한 weak 관계는 자동 처리하지 않습니다.

strong과 weak review가 한 chain에 섞이면 strong component를 먼저 하나의 최종 대표로 축약합니다.
그 뒤 `near_identical` 같은 weak 관계의 endpoint를 그 최종 대표로 다시 연결하고, 원 review ID·원 pair·
최종 pair를 새 evidence에 남긴 뒤 사람 큐 이동을 시작합니다. 따라서 weak 파일이 큐에 들어간 다음
그 비교 대상이 후속 strong 격리로 사라져 판정 근거가 고아가 되는 순서는 허용하지 않습니다.

본문 fingerprint와 pair 판정 cache는 서로 다른 버전 계약을 사용합니다.

- `FINGERPRINT_POLICY_VERSION`과 `FINGERPRINT_VERSION`은 TXT 정규화 또는 EPUB 내용 fingerprint의
  의미가 실제로 바뀔 때만 올립니다.
- `PAIR_POLICY_VERSION`은 후보 pair 분류·evidence 정책 버전입니다. 이것만 바뀌면 pair cache만 새로
  계산하고 기존 fingerprint는 그대로 재사용합니다.
- `AUDITOR_VERSION`은 배포 버전입니다. 실행 방식만 바뀐 배포는 기존 policy hash를 유지합니다.
  1.4.12는 EPUB pair 판정 의미가 실제로 확장되어 pair policy만 `1.4.12`로 올리며,
  fingerprint version/policy `5`/`1.4.2`는 유지합니다.

감사 시작 시 active house 경로·identity·`file_analysis`를 한 번에 읽습니다. 파일명, 크기,
mtime/ctime, inode/device, normalizer projection이 모두 현재인 house 파일은 `last_seen_at`을 포함한
UPDATE를 하지 않고, 신규·변경·projection stale 파일만 `reconcile_file_metadata()`로 갱신합니다.
Scanner가 실제 관측 시각의 소유자이며 warm auditor는 변하지 않은 1만여 행을 writer transaction으로
다시 쓰지 않습니다.

순서형 본문 비교가 읽은 `NormalizedLineSequence`는 파일별 남은 비교 참조 수를 계산해 마지막 사용
직후 cache에서 해제합니다. 감사 통계에는 ordered-body cache peak/final item·추정 byte·eviction 수와
프로세스 peak RSS를 남깁니다. I/O 예산과 별개로 모든 시퀀스를 감사 종료까지 붙잡아 두지 않습니다.

### 1.4.6 제목 접두사와 metadata cut 경계 계약

게시글 상태표시나 배포처 표기가 실제 작품명보다 앞에 있어도 그 접두사 일부만 `core_title`로 남겨서는
안 됩니다. 다만 흔한 단어를 무조건 지우는 방식도 금지합니다. 다음 닫힌 문맥에서만 접두사를 제거합니다.

- 선두의 `신규`, `신작`, `갱신`, `업데이트`, `업뎃`, `재업로드`, `수정본`, `교체`, `추가`는 `19禁`·`19금`·
  `완결` 상태표시와 닫는 구두점이 함께 있거나, 그 상태어 자체가 대괄호/괄호로 닫힌 경우만 제거합니다.
  따라서 `나는 매달 치트키를 갱신할 수 있다`의 `갱신`, `회귀한 신규교사`의 `신규`,
  `신작을 쓰는 천재 작가`의 `신작`은 제목으로 보존합니다.
- `글밈`, `꾸롶`은 실제 house 본문/파일로 확인한 운반 접두사입니다. `CSS`, `판`은 정상 제목일 수도
  있으므로 바로 뒤에 `[명시 작가] + 별도 작품명 + 권/회차 좌표`가 모두 있을 때만 제거합니다.
  `CSS 완벽 가이드`처럼 이 문맥이 없는 제목은 보존합니다.
- 단일 `화/권/부/장/편`은 뒤가 문자라고 곧바로 회차로 자르지 않습니다. `1부완`, `3부작`,
  `2권합완`처럼 확인된 붙임 메타 접미사는 계속 자르되, `Lv2부터`, `1968부터`, `k200 장갑차`는
  제목으로 보존합니다.
- `24／7 1권`의 `7 1권`, `어게인1997 ... -134`, `좀비묵시록 82-08 001-449`,
  `-2회차-작품명`처럼 제목 숫자와 뒤 좌표가 우연히 이어진 경우에는 실제 뒤쪽 좌표에서 자릅니다.

이 절의 접두사 보정 당시 Python과 Chrome 확장은 같은 `NORMALIZER_VERSION=1.3.2`와 같은 단일 파일
분석 결과를 사용했습니다. 현재 1.4.15는 아래 절의 닫힌 좌표 보정을 포함해 `1.3.3`을 사용합니다.
해당 변경은 파일명·`core_title` 의미만 바꿉니다. TXT/EPUB 본문 fingerprint 의미는 바뀌지 않았으므로
`FINGERPRINT_NORMALIZER_COMPAT_VERSION`과 `PAIR_NORMALIZER_COMPAT_VERSION`은 `1.3.0`에 고정하고,
1.4.2 fingerprint/pair policy hash를 유지합니다. 제목 parser 버전 상승만으로 house 본문 전체를 다시
읽어서는 안 됩니다.

파일명이 `제 1권 여명편`처럼 작품/시리즈명을 전혀 포함하지 않으면 일반 정규식으로 부모 작품을
추측하지 않습니다. 이번 house의 해당 10권은 확인된 부모 폴더를 근거로 `은하영웅전설` 수동 override를
남겼습니다. 이런 문맥 의존 예외를 범용 접두사 규칙으로 자동 확장해 다른 `제 N권` 도서를 잘못 합치는
것보다, 로그가 있는 개별 override가 안전합니다.

### 1.4.7 warm cache와 실행 검증 receipt 계약

1.4.7은 중복 판정 의미를 바꾸지 않고 반복 검증 비용만 줄입니다. 따라서
`FINGERPRINT_VERSION=5`, fingerprint/pair policy `1.4.2`, 본문 normalizer compatibility `1.3.0`을
그대로 유지하며 기존 house fingerprint와 pair 결과를 다시 만들지 않습니다.

- current pair cache hit는 `pair_cache`의 `created_at`이나 evidence를 다시 쓰지 않습니다. 대응하는 open
  review도 현재 classification/evidence와 같으면 조회 한 번으로 확인하고 그대로 둡니다. 다만 review가
  누락되거나 더 약한 과거 증거로 손상된 예외는 기존 review ID와 상태를 보존해 선별 복구합니다.
- `cache_write=False` 순수 계획도 read-only 연결로 pair cache를 사용합니다. 계획 실행은 DB를 바꾸지
  않으면서 warm pair를 본문 재독 없이 재사용합니다.
- fingerprint bulk preload는 SHA·길이·상태·identity만 먼저 읽고 큰 front/tail anchor column은 pair
  cache miss이면서 정밀 TXT 비교가 실제로 필요한 파일만 지연 로드합니다.
- 원버튼 Folderling은 공용 root lock 안에서 최초 full Doctor를 통과하고 발급한 **정확히 같은 run ID·
  DB·house/temp root**의 opaque 실행 검증 receipt만 받습니다. 이 receipt는 같은 프로세스에서 한 번
  소비되며 중복 readiness/snapshot Doctor만 생략할 수 있습니다.
  승인 token이 다르거나 일반 진입점이면 생략하지 않습니다. 변이 직전 full Doctor와 SHA/identity 검증,
  실행 종료 final full Doctor는 항상 유지합니다.
- 승인 백업의 SHA와 integrity는 파일 identity가 그대로인 동안만 process-local receipt로 재사용합니다.
  inode/ctime/size/mtime 중 하나라도 달라지면 즉시 전체 SHA/integrity 검증으로 돌아갑니다.
- 보고서 기본 목록은 파일명·mtime으로 정렬·페이지를 먼저 정한 뒤 그 페이지의 요약만 읽습니다.
  JSON-only 보고서는 앞쪽의 `kind/summary`만 제한적으로 decode하며 큰 결과 배열을 읽지 않습니다.
  검색은 모든 summary 후보를 확인하되 TXT/JSON 모두 제한된 앞부분만 읽습니다.
- 자동 분권 후보가 0건이면 기존 분석 cache를 무효화하고 같은 228건을 다시 분석하지 않습니다. active
  actual run 확인은 그대로 수행합니다.

`.dedup_state`의 과거 fingerprint·pair·manifest·backup을 단순 삭제하는 수명주기는 도입하지 않습니다.
이 데이터는 decision/review/operation/actual-run evidence가 참조하므로, hot DB와 압축 archive의 참조
계약 및 복구 절차가 먼저 설계되어야 합니다. 1.4.7은 현재 참조 증거를 보존합니다.

### 1.4.8 run-local inventory 재사용 계약

원버튼 Folderling의 최초 snapshot은 active house 파일을 실제로 walk하면서 DB identity와 현재
`file_analysis`, 공개 `file_index.json`을 모두 대조합니다. 1.4.8은 이 검증에서 얻은 내부 inventory를
같은 root lock의 바로 다음 auditor에 전달합니다.

- inventory에는 공개 index에 넣지 않는 dev/inode/ctime/mtime/size와 현재 parser 결과가 들어갑니다.
- schema, normalizer version, house root, inventory revision이 하나라도 다르면 재사용하지 않습니다.
- auditor는 전달된 identity로 시작 snapshot을 만들고, fingerprint bulk lookup에서 같은 파일을 다시
  stat하지 않습니다.
- 감사 종료 때 전체 입력 identity를 다시 stat합니다. snapshot 이후 바뀐 파일은 결과에서 제거하고
  `stale`로 fail-closed합니다.
- 본문 cache miss 분석과 pair cache miss 저장, 모든 실제 mutation은 기존 current identity·SHA 검사를
  그대로 수행합니다.
- final Doctor와 최종 DB→index projection은 실행 중 이동 결과를 반영해야 하므로 재사용하지 않습니다.

이 inventory는 메모리 안에서만 전달하며 JSON/DB에 영속화하지 않습니다. standalone auditor와 snapshot
fallback은 기존 `file_index.json + current stat` 경로를 그대로 사용합니다. 따라서 오래된 inventory를
다음 실행에 상속하거나 외부 API가 신뢰 token처럼 주입할 수 없습니다.

### 1.4.9 검증된 상태 백업 cold archive 계약

`.dedup_state/backups`의 SQLite 원본은 단순 보관 파일처럼 보여도 actual-run 승인·복구 증거가 될 수
있습니다. 따라서 1.4.9의 보관 도구는 **현재 DB의 모든 `actual_runs.backup_path` 및
`settings.approved_backup`에서 참조되지 않는 백업만** 대상으로 삼습니다. 참조 중인 백업, 최신
미참조 2개, symlink와 hardlink는 그대로 둡니다.

- `plan`은 읽기 전용이며 source inode/ctime/mtime/size, 대상 경로, 개수·바이트와 plan SHA-256을
  고정합니다. `apply`에는 동일한 개수와 plan SHA-256을 명시해야 하고, lock 안에서 계획을 다시 만들어
  한 항목이라도 달라졌으면 아무 작업도 시작하지 않습니다.
- approved/active actual run이나 미완료 operation/group이 있으면 보관을 시작하지 않습니다. 각 원본은
  SQLite `integrity_check`, SHA-256, 단일-link regular-file 조건을 통과해야 합니다.
- gzip 객체는 결정적 header로 생성하고 압축 SHA와 원문 SHA를 같은 no-follow descriptor에서 검증합니다.
  실행 intent와 객체별 metadata를 먼저 fsync한 뒤, SQLite writer transaction 안에서 참조·미완료 상태를
  다시 검사하고 현재 source identity/SHA가 그대로일 때만 hot 원본을 unlink합니다.
- actual-run 승인은 같은 writer transaction 안에서 백업 증거를 다시 확인합니다. 따라서 최초 검증과
  승인 사이에 maintenance가 백업을 소비하거나, archive 최종 확인 직후 승인이 생기는 양방향 race를
  허용하지 않습니다.
- `restore`는 metadata에 기록한 raw SHA를 사용자가 다시 명시해야 합니다. 압축 SHA·원문 SHA·크기와
  복원 SQLite integrity를 확인한 뒤 원래 backup 경로에 no-clobber로 복원하며, cold 객체는 삭제하지
  않습니다. 보관 intent·완료·복원 보고서는 `.dedup_state/reports`에 남습니다.

이 버전은 범위를 의도적으로 백업 tier 1단계로 제한합니다. actual-run이 참조하는 백업과 manifest,
hot DB의 과거 fingerprint/pair row, 고아로 보이는 `-wal/-shm`은 자동 삭제하거나 재작성하지 않습니다.
그 증거의 참조·복구 계약을 별도 버전에서 먼저 정의하기 전에는 용량 절감을 이유로 건드리지 않습니다.
또한 기존 hot retention은 완료 actual-run 백업을 cold object로 전환하지 않고 최신 10개 밖에서 직접
정리하는 구세대 정책입니다. 1.4.10은 이를 “전체 장기 수명주기 완료”로 간주하지 않습니다. cold object
ID와 actual-run 참조를 연결하고 retention도 같은 archive/restore 계약을 통과시키는 후속 schema 없이는,
모든 참조를 무조건 보호해 실행마다 대형 SQLite 백업을 누적하거나 과거 참조를 임의로 바꾸지 않습니다.

### 1.4.10 archive 경로와 warm review 경합 안전 계약

1.4.10은 중복 판정 기준을 바꾸지 않고 1.4.7·1.4.9의 실행 경계를 보강합니다. 따라서
fingerprint/pair policy `1.4.2`와 기존 본문 cache는 유지합니다.

- `.dedup_state/backups` 자체가 symlink이면 plan 단계에서 즉시 중단합니다. 계획에는 symlink를 따라간
  real path가 아니라 관리 루트 아래 lexical path를 기록하고, backup/cold archive의 모든 디렉터리
  component와 source/archive 파일을 `openat + O_NOFOLLOW`로 다시 엽니다. 양쪽 경로를 `resolve()`해서
  같다는 이유만으로 관리 루트 밖 파일을 읽거나 unlink하지 않습니다. 단, macOS가 같은 위치에 제공하는
  `/var`↔`/private/var`, `/tmp`↔`/private/tmp`만 안정적 OS 별칭으로 정규화해 DB 참조가 표기 차이로
  archive 대상에 다시 들어가지 않게 합니다.
- gzip 객체만 fsync된 뒤 metadata 기록 전에 중단된 경우, 원본과 기존 gzip의 raw SHA-256·size가
  정확히 같을 때만 metadata를 재구성하고 원래 절차를 재개합니다. gzip이 다르거나 symlink/hardlink면
  원본을 보존하고 중단합니다.
- metadata fsync 뒤에도 hot 원본을 지우기 직전 durable metadata를 no-follow로 다시 읽고 gzip 압축
  SHA-256·size를 다시 해시합니다. 이 마지막 증거가 달라졌으면 source unlink를 하지 않습니다.
- cold gzip과 metadata는 `cold_archive/backups` 디렉터리 FD를 임시파일 생성부터 `linkat` publish,
  fsync와 source 소비 직전 재검증까지 계속 유지한 채 생성합니다. 도중에 cold root가 외부 symlink로
  교체되면 외부에는 gzip/JSON을 쓰지 않고 hot source를 보존합니다.
- `restore`는 `backups` 디렉터리 FD를 임시파일 생성부터 SQLite integrity, no-clobber link, directory
  fsync와 최종 SHA 검증까지 계속 보유합니다. 도중에 관리 경로가 rename/symlink로 교체되면 고정 FD
  아래 임시파일과 부분 복원본을 정리하고, 교체된 외부 디렉터리에는 아무 파일도 쓰지 않습니다.
- warm pair cache hit가 open review를 복구할 때는 repair 대상 양쪽의 현재 filesystem identity와 DB의
  current fingerprint를 다시 확인합니다. stale이면 pending review를 만들거나 갱신하지 않습니다.
- open review 동기화는 정렬된 fingerprint만 보지 않습니다. 현재 representative → protected → managed →
  house → 경로 순위로 candidate/reference 방향을 다시 계산하고, 방향이 바뀌면 과거 open row를
  supersede한 뒤 새 방향으로 하나만 만듭니다.
- 같은 pair cache miss를 두 auditor가 동시에 계산해도 `ON CONFLICT DO NOTHING`으로 한 canonical row에
  수렴합니다. 늦은 실행은 먼저 커밋된 completed row를 다시 읽어 review를 동기화하며 UNIQUE 오류로
  전체 감사를 실패시키지 않습니다.
- 같은 fingerprint miss를 두 standalone auditor가 동시에 계산해도 immutable fingerprint unique key에
  `ON CONFLICT DO NOTHING`으로 수렴합니다. 늦은 실행은 canonical fingerprint를 다시 읽고 본문 증거가
  같은지 확인한 뒤 그 ID를 사용하며, 증거가 다르면 조용히 섞지 않고 감사를 중단합니다.

Folderling의 full SQLite integrity 검사는 preflight, 첫 mutation 직전, 최종 DB projection의 세 안전
경계에 유지합니다. 현재 schema를 여는 동작과 preflight receipt에 묶인 auditor 초기화는 structural
validation만 수행합니다. 최종 projection은 read-only SQLite가 만들 수 있는 빈 WAL을 먼저 안정화한 뒤
receipt를 발급하고, full Doctor를 실행한 다음 같은 receipt가 여전히 current인지 확인합니다. 따라서
Doctor 도중 또는 반환 직후 commit도 새 검증 증거로 덮어쓰지 않습니다. 이후 DB row projection·house
walk·index publication 동안 DB main/WAL/journal identity가 바뀌어도 terminal Doctor가 receipt 재사용을
거부합니다. 파일 Doctor·schema·미완료 operation 검사는 항상 다시 수행하며, storage identity나 run ID가
다르면 즉시 full integrity 검사로 돌아갑니다.

## 권별·부별·회차 분할 시리즈 폴더 계약 (1.4.3)

> Folderling의 마지막 시리즈 단계는 이번 입고에서 영향받은 제목만 처리하지 않습니다.
> DB의 현재 `auto_ready` 전체를 매 실행 다시 계산하고 실제 폴더에 적용합니다. 따라서 과거 버전에서
> loose 상태로 남은 기존 분권도 한 번에 정리되며, 이후 새 권·새 회차 분할본이 들어오면 같은 실행에서
> 기존 파일과 함께 작품 폴더로 묶입니다. 단, `auto_ready`가 되려면 파일 수가 아니라 **서로 다른
> 시리즈 시작 좌표가 2개 이상**이어야 합니다.

- `1부 1권`과 `2부 1권`은 서로 다른 복합 좌표입니다. 부 번호를 버리고 권 번호만 비교하지 않습니다.
- `외전 1`, `외전 2`는 서로 다른 외전 좌표입니다.
- 같은 core의 `1-100화`, `105-200화` 또는 `1-100화`, `100-200화`처럼 **시작점이 다른** 회차
  분할본은 시리즈 좌표로 취급합니다. 범위가 일부 겹쳐도 시작점이 다르면 별개 분할본으로 같은 폴더에
  보존합니다. 이미 이런 실제 분할 cohort가 있을 때 `100-150화`가 추가되는 것도 허용합니다.
- `1-200.txt + 1-200.epub`, `1-180.txt + 1-200.epub`처럼 시작점이 같은 합본·완결 판본끼리는
  시리즈가 아닙니다. 시작 `0`과 `1`도 둘 다 작품 처음부터라는 뜻이므로 같은 좌표입니다. 이런 쌍은
  자동 폴더링뿐 아니라 분권 검토 화면에서도 제외하고, 내용 중복 여부는 앞 단계 dedup이 담당합니다.
- 동일 `1권`의 TXT·EPUB만으로도 시리즈 폴더를 만들지 않습니다. 다만 실제 `1권 + 2권` 또는
  `1-100 + 101-200`처럼 서로 다른 좌표가 이미 2개 이상이면, 그 시리즈 안의 동일 좌표 병행 포맷은
  보존할 수 있습니다. 폴더 묶기는 내용 중복 판정을 대신하지 않습니다.
- 같은 core의 웹연재 합본이 있어도 실제 권별 cohort가 2권 이상이면 그 cohort만 시리즈 폴더 판정에
  사용합니다. 합본은 임의로 권별 폴더에 섞거나 삭제하지 않습니다.
- 작가는 양쪽에 모두 명시되어 서로 다를 때만 폴더 자동화를 차단합니다. 한쪽 또는 양쪽의 작가
  누락은 정상 입력으로 취급합니다.
- 새 EPUB/PDF 권별 묶음은 좌표가 중복되지 않고 각 부의 권수가 연속이면 입고 도중에도 작품 폴더를
  만들 수 있습니다. 파일 형식이나 입고 시점과 무관하게 마지막 전체 자동 단계가 loose TXT/EPUB/PDF,
  기존 단독 권과 이번 신규 권, 빠진 권이 있는 묶음까지 다시 평가합니다.
- 외전이 포함된 자동 묶음에는 서로 다른 본편 좌표가 최소 2개 필요합니다. `1권.txt + 외전.epub` 같은
  **단일 본편+외전**, `외전 1 + 외전 2` 같은 **외전끼리** 관계는 오류 가능성이 높아
  `side_story_requires_two_main_coordinates`로 사람 승인을 요구합니다. 반면
  `1권.txt + 2권.epub + 외전.txt`는 형식이 달라도 자동으로 묶습니다. 사람이 관리 화면에서 실제 같은
  작품임을 확인하면 이 차단만 명시적으로 해제하여 journal 기반으로 묶을 수 있습니다.
- 검토 큐에서 house로 복원·수용한 파일은 새 경로의 `file_analysis` identity를 같은 operation에서
  갱신합니다. 따라서 바로 이어지는 시리즈 판정에서도 새 권이 누락되지 않습니다.
- 전체 자동 단계는 실행 시작 manifest에 있던 기존 house 파일뿐 아니라 같은 actual run의 committed
  `house_ingest`/queue 수용 destination도 inode·ctime·size·mtime·SHA-256으로 다시 증명한 뒤에만
  staging합니다. 자동으로 새로 만든 관계는 `strong_match`, 수동 예외 승인은 `human_decision`으로
  구분하며, 기존 `human_decision` 관계를 자동 실행이 덮어쓰지 않습니다.
- 파일 이동으로 `_최근` 링크가 끊어질 때는 그 링크가 이동 전 경로를 정확히 가리킨다는 소유권을
  증명한 경우에만 새 작품 폴더로 원자적으로 재지정합니다. 다른 링크나 일반 파일은 건드리지 않습니다.

시리즈 폴더 분석과 실제 이동은 `public_tests/test_folderling_volume_auto.py`,
`public_tests/test_volume_review.py`, `public_tests/test_volume_group_apply.py`의 복합 좌표, 병행 포맷,
합본 공존, 작가 누락, staging/journal 회귀와
`public_tests/test_volume_false_series_restore.py`의 선택 복원 회귀를 함께 통과해야 합니다.

### 숫자만 표기된 권의 문맥 추론 (1.4.11)

> **별도 입고 폴더나 파일명 강제 수정은 필요하지 않습니다.** Scanner, auditor, Folderling이 현재
> house와 temp 목록을 함께 보고 `작품명 1`, `작품명 2`의 숫자를 권 좌표로 승격합니다. 파일명은
> 그대로 보존하고 DB·index의 `core_title`과 권 좌표만 보강한 뒤, 기존 `auto_ready` 시리즈 단계가
> 작품 폴더를 만듭니다.

다음 중 하나가 있어야 문맥이 증명됩니다.

1. 같은 후보 core에 서로 다른 숫자가 2개 이상 있음
2. 같은 core의 `1권`처럼 명시적 권/부 좌표가 이미 있음
3. 같은 core가 사람이 승인한 managed work로 이미 연결되어 있음

따라서 `house/판타지소설 1.txt`만 있던 상태에서 `temp/판타지소설 2.epub`이 들어와도 두 파일이
서로를 증명해 같은 시리즈로 묶입니다. `1권.txt + 2.epub`, `1.txt + 2.txt`, 과거 loose 파일과 이번
신규 파일처럼 형식·입고 시점이 달라도 같습니다. 반면 근거가 하나뿐인 `고유 작품 7.txt`는 숫자를
제목에서 임의로 떼지 않습니다.

자동 승격의 경계는 다음과 같습니다.

- 작품명 뒤의 독립된 정수 `1~99`만 후보입니다. `1-100`, 명시적 `화/권/부`, 100 이상의 합본 숫자,
  날짜처럼 보이는 제목 꼬리, 숫자만 있는 제목, `〔D2〕` 판본 표시는 이 규칙으로 재해석하지 않습니다.
- `외전 1` 같은 외전 좌표는 본편 숫자 권의 증거로 사용하지 않습니다. 기존 계약대로
  **단권+외전**과 **외전+외전**은 사람 검토를 유지하고, 서로 다른 본편 좌표가 2개 이상인
  `1 + 2 + 외전`만 자동 묶습니다.
- 작가는 양쪽에 모두 명시되어 서로 다를 때만 차단합니다. 한쪽 또는 양쪽에 작가가 없는 것은
  정상 입력입니다.
- `[[제목]]` 사용자 보호, 모호한 범위, 충돌한 작가, 서로 다른 판본 표시는 fail-closed합니다.
- `10 소책자 한정판` 같은 닫힌 판형 꼬리는 10권 좌표를 공유할 수 있지만 파일명은 보존합니다.
  나중에 일반 10권이 들어오면 같은 좌표의 두 파일이 먼저 기존 본문 중복 경쟁/검토를 거치며,
  제목만으로 어느 하나를 삭제하지 않습니다.

문맥 승격 뒤에도 중복 안전선은 그대로입니다. 서로 다른 `1권`·`2권`은 형제 권이라 격리하지 않고
시리즈로 보존합니다. 같은 좌표 파일은 기존 exact/EPUB/TXT 본문 비교를 통과해야만 격리할 수 있습니다.
warm 실행은 저장된 house 분석을 증거로 재사용하고, 숫자 꼬리 가능성이 있는 이름만 가볍게 검사하며,
변화 없는 파일을 다시 stat하거나 본문을 읽지 않습니다.

이 변경은 `NORMALIZER_VERSION=1.3.2`, 배포/auditor `1.4.11`로 index와 제목 projection을 한 번
재생성합니다. 본문 정규화와 pair classification 자체는 바뀌지 않아 fingerprint/pair policy `1.4.2`,
본문 normalizer compatibility `1.3.0`을 유지합니다.

### 닫힌 좌표 보정과 EPUB spine 동일성 (1.4.12)

1.4.12는 실제 오탐·미탐에서 확인한 세 가지 파일명만 좁게 보정합니다.

- `(총243화)`처럼 `총 + 숫자 + 화/회/장/편`이 닫힌 메타로 쓰이면 단일 243화가 아니라 `1~243`으로
  해석합니다. `총`이 제목 단어에 붙거나 `총 N권/부`인 경우에는 적용하지 않습니다.
- `11.5 (특별판) (작가).epub`의 소수 좌표는 같은 시리즈 문맥이 있고 `특별판/특장판/한정판/소책자`가
  명시된 경우에만 `23/2권`으로 승격합니다. 일반 제목의 `3.11`은 그대로 제목입니다.
- `작품 1 (명시 작가) -2.epub`의 마지막 `-2`는 완전한 작가 괄호 뒤에 붙은 2~9 복사본 접미사일
  때만 무시합니다. 임의의 제목 `-2`나 범위 표기는 바꾸지 않습니다.

EPUB의 ZIP 내부 bytes가 다를 때는 기존 strict content와 reading-payload 비교 뒤에 다음 조건을 **모두**
통과한 경우만 `epub_equivalent`로 강화합니다.

1. 파일명 core·권/회차 좌표가 같고, 양쪽 명시 작가가 충돌하지 않음
2. OPF spine 순서의 HTML/XHTML에서 보이는 문자 50,000자 이상이 공백 정규화 후 정확히 같음
3. OPF의 UUID/ISBN 등 안정 식별자가 하나 이상 정확히 겹침
4. mutation 직전에 같은 no-follow·한도 검사와 spine hash/글자수/식별자 겹침을 다시 확인함

이미지·CSS·압축 방식이 달라도 같은 판의 본문임을 증명할 수 있지만, 제목 유사도나 짧은 본문만으로는
자동 격리하지 않습니다. 반대로 OPF 식별자가 서로 다르고, 내부 제목의 권 좌표가 다르며, 출판사 또는
발행일도 충돌하면 `different`로 닫아 `metadata_only` 사람 검토를 만들지 않습니다. 증거가 하나라도
부족하면 기존 warning/사람 검토 경계를 유지합니다.

이 버전은 `NORMALIZER_VERSION=1.3.3`, pair policy/auditor `1.4.12`입니다. 파일명 projection과 pair
cache만 새 세대로 계산하며, 기존 대용량 fingerprint는 다시 만들지 않습니다.

### Legacy 실행 차단과 분권 복구 계약 (1.4.13)

1.4.13은 중복·제목 파싱 의미를 넓히지 않고 제품 바깥의 우회 경로와 분권 작업의 복구 경계만
보강합니다.

- `tools/legacy/migrate_marker_position.py`는 과거 앞마커 파일을 찾는 dry-run 감사기로만 남습니다. `--run` 또는
  `migrate(..., dry_run=False)`는 파일 walk 전에 hard-fail하며, 실제 변경은 backup·manifest·journal·
  Doctor가 연결된 관리형 작업만 사용해야 합니다.
- 분권 검토와 Folderling 자동 합류는 공통 resolver를 사용합니다. normalizer version, 파일명,
  size·mtime·ctime이 현재 DB 분석과 모두 맞으면 저장된 작가를 보존하고, 하나라도 stale일 때만 현재
  파일명을 다시 해석합니다.
- 작가가 비어 있는 파일은 계속 정상 입력입니다. 같은 core와 안전한 서로 다른 권 좌표가 있고 양쪽에
  명시된 작가끼리 충돌하지 않으면 자동 묶습니다. 작가 누락만으로 검토 큐를 만들지 않습니다.
- 강제 종료로 `.volume_group_staging` 복사본이 남으면 다음 Folderling 시작 전에 검사합니다. terminal
  actual run, 미완료 operation 0건, manifest의 경로·개수·size·SHA 일치, 예상 밖 파일 0건을 모두
  만족할 때만 stage 복사본을 지웁니다. 알 수 없는 항목은 보존하고 새 actual run을 차단합니다.
- `unpack`의 JPG·ZIP 등 비지원 부속파일 영구 폐기는 의도된 운영 정책입니다. 모든 TXT·EPUB·PDF가
  먼저 안전하게 이동·격리되어 하나도 남지 않고, symlink와 tree identity 검사를 통과한 wrapper만
  정리합니다. 삭제 건수·byte는 결과 이벤트와 로그에 남지만 복구용 quarantine은 만들지 않습니다.

배포/auditor/UI는 `1.4.13`이며 schema `v15`, `NORMALIZER_VERSION=1.3.3`, fingerprint
version/policy `5`/`1.4.2`, pair policy `1.4.12`를 유지합니다. 따라서 제목·본문 cache 재기준은
발생하지 않습니다.

### 상태 저장소 모듈 ownership 계약 (1.4.14)

1.4.14는 중복 판정이나 파일 mutation 의미를 바꾸지 않는 구조개선 버전입니다. AI와 사람이 한 기능을
수정할 때 6천 줄짜리 저장소 전체와 무관한 삭제 경계까지 동시에 건드리지 않도록 다음 소유권을 둡니다.

- `state_schema.py`: schema version, DDL, 필수 table/view만 소유하며 import만으로 DB를 열지 않습니다.
- `state_repository.py`: SQLite 연결, schema migration/validation, transaction, canonical DB path만
  소유합니다.
- `volume_policy.py`: 권·부·회차·외전 좌표 생성과 호환성 판단의 단일 구현입니다.
- `file_analysis_repository.py`: current/stale filename 분석, contextual bare-volume 보정, catalog rekey,
  파일 분석 projection을 소유합니다.
- `decision_store.py`는 기존 import를 깨지 않는 compatibility facade이자 actual-run, backup/manifest,
  operation journal, recovery, Doctor, review/decision 상태 머신의 소유자입니다. 이 mutation 상태 머신은
  한 불변조건이므로 이번 버전에서 여러 파일로 쪼개지 않습니다.
- 파일 descriptor·SHA·no-follow identity는 이미 `mutation_io.py`가 소유하므로 별도 `file_identity.py`를
  만들어 중복하지 않습니다. 새 policy 모듈도 기존 `bare_volume_context.py`,
  `dedup_episode_relation.py`와 역할이 겹치면 추가하지 않습니다.

추출 모듈은 `decision_store`를 역으로 import하지 않습니다. 서버·CLI·기존 테스트는 계속
`decision_store.*`를 사용할 수 있고, facade의 validation/analysis hook도 유지합니다. 1.1.1/1.4.4
일회성 도구는 제품 backend에서 `tools/legacy/`로 이동했으며, 비호환 marker actual은 계속 파일 탐색
전에 hard-fail합니다.

배포/auditor/UI는 `1.4.14`이며 schema `v15`, `NORMALIZER_VERSION=1.3.3`, fingerprint
version/policy `5`/`1.4.2`, pair policy `1.4.12`, archive `1.4.10`을 유지합니다. 따라서 DB migration,
fingerprint/pair cache 재기준, house 전체 재분석은 발생하지 않습니다.

### 플랫폼·stale override·staging recovery 계약 (1.4.15)

1.4.15는 1.4.13·1.4.14 후속 리뷰에서 재현된 세 안전 결함을 좁게 보정합니다.

- `/tmp`와 `/var`의 `/private` 별칭 접기는 Darwin에서만 수행합니다. Linux의 `/tmp`, `/var`는
  원래 절대 경로를 유지해 Ubuntu CI와 Linux mutation 경로가 존재하지 않는 `/private`를 열지 않습니다.
- 파일 분석 identity가 stale이어도 명시적인 `title_override_json`이 있으면 저장된 core/readable/catalog
  제목은 보존합니다. author와 권·회차 좌표는 현재 파일명에서 다시 계산하므로 과거 작가 보존 정책과
  제목 override 정책을 섞지 않습니다.
- 분권 후보 조회는 현재 저장 core 일치 행뿐 아니라 stale identity 행도 공용 resolver로 재판정하고,
  resolver 결과 core가 실제로 일치하는 행만 자동 라우팅에 사용합니다.
- abandoned staging 복구는 같은 no-follow FD에서 읽은 manifest evidence를 cleanup에 전달합니다.
  manifest·stage identity, 전체 디렉터리 entry 집합, case/run/root 제거 완료 중 하나라도 달라지면
  recovered 성공으로 세지 않고 issue를 남겨 다음 actual run을 차단합니다.

`backend.*` package import는 공식 실행 계약에 추가하지 않습니다. 현재 실행기는 `backend/`를 하나의
top-level application module 경로로 공유합니다. package import를 별도 지원하면 `decision_store`의
process-local lock·receipt·recovery registry가 두 module identity로 갈릴 수 있으므로, 별도 loader
설계 없이 상대 import만 추가하는 수정은 더 위험합니다. 다만 1.4.14에서 추가한 제한적 `__all__`은
제거해 기존 top-level star import의 actual-run·journal·recovery·Doctor export 범위를 복원합니다.

배포/auditor/UI는 `1.4.15`이며 schema `v15`, `NORMALIZER_VERSION=1.3.3`, fingerprint
version/policy `5`/`1.4.2`, pair policy `1.4.12`, archive `1.4.10`을 유지합니다. DB migration,
fingerprint/pair cache 재기준, house 전체 재분석은 발생하지 않습니다.

## 구조

```text
backend/                    Python 구현
public_tests/               공개용 합성 fixture 회귀 테스트
run_folderling_one_button.py  기존 컨트롤서버 호환 실행기
scanner.py                    기존 Scanner 호환 실행기
deduplicator.py               기존 dry-run 호환 실행기
folderling.py                 기존 command 파일 호환 실행기
run_title_cleanup_candidates.py  1.2.7 제목 후보 read-only 감사기
run_title_cleanup_apply.py       1.2.7 제목 교정 재입고 dry-run/실행기
run_library_server.py             1.2.8+ 독립 도서 관리 웹 서버
library_frontend/                 React 기반 도서 관리 화면
```

mutable runtime 파일은 계속 프로젝트 루트에 생성됩니다.

```text
.dedup_state/
file_list.json
file_index.json
success.log
fail.log
```

## 환경 설정

소스 파일을 편집하지 않고 환경 변수로 경로를 바꿀 수 있습니다.

| 변수 | 기본값 |
| --- | --- |
| `FILE_CHECK_PROJECT_ROOT` | 이 저장소 루트 |
| `FILE_CHECK_HOUSE_DIR` | `~/Documents/txt_house` |
| `FILE_CHECK_TEMP_DIR` | `~/Documents/txt_temp` |
| `FILE_CHECK_STATE_DIR` | `<project>/.dedup_state` |

## 실행

Scanner:

```bash
python3 scanner.py
```

중복 검사 dry-run:

```bash
python3 deduplicator.py --dry-run --rescan
```

원버튼 entry point의 옵션 확인:

```bash
python3 run_folderling_one_button.py --help
```

`run_folderling_one_button.py`는 실제 파일 입고를 수행할 수 있으므로 라이브 환경에서는
상태 DB의 doctor 결과와 backup을 확인한 뒤 사용해야 합니다.

## 도서 관리 웹 서버 (1.2.8~1.3.0)

1.2.8부터 `file_check`는 기존 컨트롤서버와 분리된 로컬 웹 서버를 제공합니다. 기본 주소는
`http://127.0.0.1:9012`이며 외부망에 직접 노출하지 않습니다. 현재 화면에는 DB·index·입고
대기 상태를 보여주는 대시보드, 플랫폼 `ok` 정보가 없는 파일의 수동 제목 교정, 분권 후보 검토,
서비스 실행, 검토 큐를 흡수한 카탈로그, 작업 이력·보고서와 화면 설정이 있습니다.

로컬 Chrome 확장 코드는 공개 Git에서 제외된 `extension/`에만 둡니다. 해당 폴더의
`normalizer.js`는 Python `backend/normalizer.py`와 같은 `NORMALIZER_VERSION` 및
`core_title` 결과를 유지하며, `extension/check_normalizer_parity.py`로 양쪽 결과를 대조합니다.
`[[제목]]`·`{{구조}}`는 게시글 사이트나 번들 index가 생성하는 문법은 아니지만 계약 차이를
남기지 않기 위해 확장에서도 호환 처리합니다.

## 동일 좌표 중복 재검사 (1.2.10)

Folderling은 같은 `core_title`과 권/회차 좌표를 가진 TXT·EPUB 쌍을 필수 후보로
검사합니다. EPUB은 ZIP 컨테이너 전체 바이트가 아니라 내부 파일명과 비압축 내용을
비교하므로 재압축된 동일 도서를 찾을 수 있습니다. 운영 라이브러리의 제한된 읽기 전용
재감사는 `python3 run_same_coordinate_audit.py`로 실행하며 고유 파일 80개를 넘지 않습니다.
결과 보고서는 파일 이동 없이 temp의 `dedup_logs`에만 저장됩니다.

1.2.10은 보호 제목 literal의 실제 입고, schema v11 override 영속화, 기존 분권 폴더 자동
합류와 최종 doctor/index 검증까지 운영 인수를 완료했습니다. 다음 1.2.11에서는 Folderling,
제목 교정, 분권 묶기의 반복 전체 스캔을 줄이되 doctor와 중복 판정 기준은 유지합니다.

## 검증된 index snapshot 최적화 (1.2.11)

Folderling은 현재 house 경로 집합, DB file identity, 저장된 제목 분석과 기존 index가 모두
일치하면 사전 전체 Scanner 대신 기존 snapshot을 재사용합니다. 입고가 끝난 뒤에는 journal이
갱신한 DB에서 최종 index를 다시 투영하므로 같은 파일을 두 번째로 전체 재분석하지 않습니다.
제목 교정과 분권 묶기도 작업 직후 같은 projection을 사용합니다.

외부에서 파일을 추가·수정·삭제했거나 normalizer·DB 분석·index가 일치하지 않으면 최적화
경로를 사용하지 않고 기존 전체 Scanner로 자동 복귀합니다. doctor, backup, manifest, operation
journal과 중복 판정 기준은 변경하지 않습니다. `NORMALIZER_VERSION`과 auditor cache 버전도
제목 규칙 변경이 없으므로 유지해, 이전 실행에서 분석한 본문을 다시 읽지 않습니다.

배포 묶음의 날짜·포장 폴더를 house에 유지하지 않고 내부 도서만 개별 입고하려면
`txt_temp/unpack` 아래에 넣습니다. Folderling과 중복 감사, 웹 대시보드는 `unpack` 내부의
TXT·EPUB·PDF를 일반 입고 대상으로 집계하되, 실제 입고 단계에서는 파일별로 펼쳐 초성 폴더에
보냅니다. 모든 지원 파일이 입고·격리되어 남지 않으면 표지 JPG·지도 ZIP 같은 부속 파일과 포장
폴더를 함께 삭제하고 재사용할 `unpack` 루트만 남깁니다. 한 권이라도 실패해 지원 파일이 남거나
심볼릭 링크가 있으면 해당 묶음은 삭제하지 않습니다. 기존 `___*` 폴더도 같은 동작으로 호환합니다.

기존 분권 폴더와 같은 좌표의 파일이 temp에 남았지만 본문이 달라 중복으로 확정되지 않은
경우에는 해당 파일만 `trash_bin/warning/volume_coordinate_conflicts`에 journaled hold합니다.
같은 batch의 겹치지 않는 신규 권은 계속 처리해 기존 작품 폴더에 자동 합류합니다. hold 작업도
manifest와 operation recovery를 사용하므로 중단 시 원래 temp 경로로 복구할 수 있습니다.

## 서비스·작업 로그 UI (1.3.0)

도서 관리 서버의 대시보드는 운영 기본값으로 실행하는 원버튼을, `/services`는 각 작업의 목적,
대상 건수, 읽기·쓰기 범위, 사전 검사, 최근 실행을 자세히 보여줍니다. 두 화면은 별도 구현이 아니라
같은 단일 worker job과 기존 Python domain service를 호출합니다.

현재 Folderling 실제 입고, Scanner/index 갱신, 플랫폼 인기 DB 업데이트, 플랫폼 실패 결과 재검사,
기존 인기값 상향 갱신, 노벨피아 인증 누락 재검사, Google Sheet 동기화를 등록했습니다. 실행 중에는
다른 변경 job을 시작하지 않으며, 실행 불가 버튼에는 `대상 없음`, `doctor 문제`, `인증 누락`,
`다른 작업 실행 중` 같은 이유가 표시됩니다.

대시보드와 서비스 목록은 화면을 열 때마다 전체 SQLite `integrity_check`나 모든 house 파일의
identity를 다시 읽지 않습니다. 화면에서는 schema·미완료 operation·대표 파일 상태 같은 DB
운영 조건을 빠르게 확인하고, 플랫폼 대상 미리보기는 15초 동안 공유합니다. 전체 무결성,
파일 존재·size·mtime·inode Doctor는 Folderling 등 실제 변경 작업의 preflight와 사후 검증에서
기존처럼 fail-closed로 수행합니다. 대시보드 기본 통계와 서비스 버튼도 서로 독립적으로 표시해
플랫폼 대상 집계가 늦어져도 도서 현황 화면을 먼저 볼 수 있습니다.

`/jobs/<job_id>`에서는 서버를 다시 열어도 유지되는 진행률, 구조화 이벤트, 완료 결과와 원본 로그를
확인할 수 있습니다. 로그는 화면 검색·복사·다운로드를 지원합니다. Folderling의 기존
`success.log`와 `fail.log`도 해당 job 로그에 복사되며, 플랫폼 장시간 수집은 10작품 단위 진행
이벤트를 저장합니다. 기존 컨트롤서버의 `Folderling 실제 입고` 원버튼은 계속 유지합니다.

`/reports/dedup`는 `txt_temp/dedup_logs`에 누적된 과거 `dedup_*.txt`와
`strong_candidates_*.txt`, 새 구조화 JSON 보고서를 실행 시각·종류·요약과 함께 읽기 전용으로
조회합니다. 검색, 원문 열람, 복사와 다운로드를 지원하므로 서버 도입 전 실행과 컨트롤서버 원버튼
실행도 공통 이력으로 볼 수 있습니다. 새 dedup 실행은 schema-versioned JSON만 원본으로 저장합니다.
사람용 TXT는 `TXT로 내보내기`를 누를 때 메모리에서 즉시 생성되며 `dedup_logs`에 중복 저장하지
않습니다. 기존 TXT 보고서는 삭제하거나 변환하지 않고 그대로 호환합니다.
이 보고서는 dedup 단계 결과이므로 preflight Doctor·backup·index·치명적 오류는 `/jobs`의 구조화
이벤트와 raw log에서 별도로 확인합니다.

Folderling 작업 상세는 doctor, snapshot, 중복 판정, temp 입고, index 갱신, 최종 doctor를
타임라인으로 표시합니다. 파일별 결과 표에서는 정상 입고, 정확 중복, 검토 격리, warning,
실패와 제외를 구분하고 원본 후보·기존 유지 파일·실제 목적지·다음 조치를 함께 확인할 수 있습니다.
이 근거는 Folderling core가 직접 JSONL event로 기록하므로 화면을 위해 stdout 문구를 다시
해석하지 않습니다.

`/catalog`는 활성 house 파일을 core title 기준 작품으로 묶어 실제 보유 파일, 작가·범위와
시리즈·카카오·노벨피아 상태·인기 지표를 읽기 전용으로 검색합니다. `/review/queue`는 DB review와
`trash_bin`의 warning, 작가 충돌, 중복 의심, exact quarantine을 한 화면에서 조회합니다.
1.3.0에서는 이 두 화면이 파일이나 DB를 변경하지 않으며, 복원·격리·영구 삭제는 후속 버전의
확인형 작업으로 추가합니다. 대시보드는 doctor, 입고 대기, 검토 큐, 메타데이터 미확인과 최근
실패를 `확인할 일` 카드로 연결합니다.

검토 큐는 `관계 검토 · 미격리`, `실제 격리됨`, `격리 경로 확인 필요`를 별도로 표시합니다.
DB review와 실제 queue 파일이 같은 항목이면 한 행으로 합칩니다. EPUB 감사 결과가 약한
`metadata_only`이고 두 파일의 core title이 다르면 사람 review를 만들지 않습니다. 같은 EPUB
작품명에 마지막 분권 숫자만 다른 쌍도 이 범주에 포함됩니다. 강한 본문 동등·exact 판정과 같은
core title에서 `외전` 단독 EPUB과 본편 `N권` EPUB 사이의 `metadata_only` 관계도 제외합니다.
강한 본문 동등·exact 판정은 파일명에 외전이 있어도 그대로 검토 대상으로 유지합니다.
fingerprint가 갱신된 같은 파일쌍은 오래된 open review를 `superseded`하고 최신 증거 하나만
남깁니다.

### 전체 카탈로그 탐색기 (1.3.1)

`/catalog`는 작품·파일·폴더·격리의 네 읽기 전용 탭을 제공합니다.

- `작품`: 보유 파일과 시리즈·카카오·노벨피아 수집 상태
- `파일`: 활성·비활성·house·temp·queue 상태, 분석 좌표, work/variant, fingerprint와 검토·결정·작업 이력
- `폴더`: DB가 알고 있는 house 폴더를 먼저 조회하고, 상세를 열 때만 실제 파일을 읽어 DB 등록 파일과
  표지 같은 미등록 부속 파일을 구분
- `격리`: committed operation과 실제 `txt_temp/trash_bin`을 대조해 보관·누락·미추적·삭제 이력을 구분

1.4.4 전수 삭제 이후 격리 탭의 기본 조건은 `실제 보관`입니다. DB의 복원·수용·후속 격리 operation은
감사 이력으로 보존하지만, 이미 해소된 과거 목적지 경로를 `파일 없음`인 현재 격리 항목으로 다시
노출하지 않습니다. 영구 삭제된 항목은 `삭제 이력`을 명시적으로 선택했을 때만 보며, 현재 격리
inventory와 삭제 journal을 섞지 않습니다.

파일 두 개를 선택하면 core title, 작가, 권·부·회차 좌표, 크기, raw/normalized SHA와 기존 review·decision
근거를 나란히 비교할 수 있습니다. 관계 판정, 격리, 복원, 이동, 영구 삭제 버튼은 미리보기만 표시하며
1.3.1에서는 실행되지 않습니다. 제목 교정으로 퇴역한 `.dedup_state/retired_paths` 가상 경로도 실제
폴더나 격리 파일로 세지 않고 파일 이력에서만 표시합니다.

16,000개 이상 운영 규모를 위해 파일·작품 목록은 SQLite read model에서 페이지 단위로 읽습니다.
폴더 목록은 DB projection을 짧게 캐시하고 사용자가 `실제 상태 갱신`을 눌렀을 때 명시적으로 새로
계산합니다. 실제 폴더와 격리 파일 순회는 상세 확인 또는 격리 탭에서만 안전 상한을 두고 수행합니다.

### 사람 관계 판정과 격리 관리 (1.3.2)

파일 탐색기에서 두 파일을 선택하면 현재 fingerprint에 묶인 다음 관계를 저장할 수 있습니다.

- `같은 내용`: 같은 variant로 연결
- `같은 작품의 다른 판본·부속`: 같은 work의 별도 variant로 보존
- `제목만 같은 다른 작품`: 서로 다른 work로 분리

판정은 실행 전 두 파일의 현재 identity와 계획 SHA-256을 다시 확인하며, 판단 정정은 이전 decision을
지우지 않고 supersedes 이력으로 남깁니다. 아직 다른 파일과 공유되지 않은 최초 관계는 UI에서 취소할
수 있습니다.

`사용자 승인 격리`는 자동 동일 파일 판정과 별개입니다. 불필요한 판본을 선택하면 DB backup, 선택 파일
manifest, copy-verify-consume operation을 만든 뒤 `txt_temp/trash_bin/user_approved_discard`로 옮깁니다.
대표 파일을 격리할 때 같은 variant의 다른 활성 파일이 있으면 그 파일을 새 대표로 지정하고, 마지막
파일이면 활성 파일 유무로 variant/work 퇴역 영향을 표시합니다. 격리 후 index는 DB projection에서 다시
동기화합니다.

격리 탭의 `중복 아님 복원`은 원래 경로가 비어 있을 때만 동작합니다. 비교할 활성 파일과
`same_work_distinct_variant` 또는 `distinct_work` 판단을 반드시 함께 저장하므로 같은 fingerprint 근거로
즉시 다시 격리되지 않습니다. 목적지가 이미 있으면 자동 suffix나 덮어쓰기를 하지 않고 차단합니다.

영구 삭제는 실제 bytes와 operation 소유권, quarantined fingerprint, keep 파일/decision을 다시 검증한
선택 항목만 대상으로 합니다. 목록에서 대상을 선택한 뒤 모달에 표시되는 항목 수와 용량을 확인하고
`영구 삭제 실행`을 한 번 더 눌러야 하며, 자동 30일 삭제는 제공하지 않습니다. 삭제 후 파일 bytes는
복구할 수 없지만 DB identity, fingerprint, 원래 격리 operation과 purge journal은 남습니다.

상태 DB를 변경하는 작업은 실행 전 SQLite 백업을 만들며, `.dedup_state/backups`의 백업은 파일명이나
작업 종류와 무관하게 최신 10개만 유지합니다. 승인·실행 중이거나 journal이 미완료인 작업이 참조하는
백업은 개수 제한 밖에서도 보호합니다. 새 백업을 만들 때마다 같은 정책을 적용하므로 별도의 날짜 기반
정리 작업은 필요하지 않습니다.

### 도서·폴더 정리 작업공간 (1.3.3)

파일 상세의 `이름·위치 정리`는 활성 house 파일을 기존 house 폴더로 옮기거나 불필요한 꼬리표만
정리합니다. 새 파일명을 다시 분석한 core title, 읽기 제목, 플랫폼 검색 제목, 작가와 권·부·외전 좌표가
기존 DB projection과 모두 같을 때만 file ID·work·variant 관계를 유지한 journaled 이동을 허용합니다.
분석 결과가 달라지는 이름은 이 화면에서 실행하지 않고 기존 제목 교정으로 보내 Folderling 판정을 다시
받습니다. 작품 관계 변경도 위치 이동과 섞지 않고 파일 비교의 사람 관계 판정에서 별도로 승인합니다.

폴더 탐색기에서는 작품에 연결된 빈 관리 폴더를 만들거나, 이미 존재하는 폴더를 파일 이동 없이
`primary`, `edition`, `auxiliary` 역할로 등록할 수 있습니다. 관리 폴더의 이름·위치를 바꾸면 그 아래
DB 도서, JPG·ZIP 같은 미등록 부속 파일과 빈 하위 폴더를 하나의 operation group으로 묶어 house 안에서
통째로 이동합니다. symlink, house 밖 목적지, 기존 목적지 충돌, stale 파일 identity, 다른 작품이 섞인
기존 폴더 등록은 실행 전에 차단하며 자동 덮어쓰기나 `_dup_N` 이름은 만들지 않습니다.

schema v12의 `operation_groups`와 `work_folders`가 폴더 작업과 작품 역할을 보존합니다. 서버가 이전
schema v11 DB를 처음 열 때는 mutation lock 아래 검증된 SQLite 백업을 만든 뒤에만 migration합니다.
폴더 작업이 중단되면 `dedup_recover.py recover`가 상위 group을 우선 복구하고, 완료된 이동은 Doctor와
index를 다시 동기화합니다. 운영 DB는 서버를 재시작할 때 migration되며 단순 코드 테스트만으로는
변경되지 않습니다.

1.3.3은 합성·전체 회귀와 production build까지 완료했지만 운영 DB migration과 실제 UI 왕복 테스트는
후속 1.3.5 안정화 단계로 이월했습니다.

### 작품 병합·분리와 입고 라우팅 (1.3.4)

schema v13은 `works.status`, `variants.status`, `work_aliases`, `work_management_events`를 추가합니다.
작품 관리 화면에서 기존 work를 다른 work로 병합하거나 선택 variant를 새 work로 분리할 수 있으며,
대표 파일도 같은 variant의 활성 파일 중에서 다시 지정할 수 있습니다. 병합은 파일 bytes를 움직이지
않고 variant·관리 폴더·활성 alias 관계만 옮기며, source work는 삭제하지 않고 `retired` 이력으로 남깁니다.
두 작품에 primary 폴더가 각각 있으면 유지 work의 primary를 보존하고 source primary는 edition으로
전환합니다.

`core_title`, 읽기 제목, 폴더명 alias는 한 시점에 하나의 활성 work만 가리킵니다. alias에는 특정 관리
폴더를 입고 목적지로 지정하거나 work의 primary 폴더를 자동 사용하게 할 수 있습니다. 충돌 alias는 현재
alias ID를 명시해야 교체할 수 있고, 교체·해제 이력은 이전 행과 관리 event를 보존합니다.

Folderling은 신규 파일의 현재 DB 분석값을 활성 alias와 정확히 비교합니다. 사람이 지정한 alias가 맞으면
기존 자동 분권 추정보다 우선해 관리 폴더로 입고하고, 파일마다 별도 variant와 representative를 만들어
같은 권수의 다른 판본도 동일 내용으로 합치지 않습니다. alias 목적지에 같은 파일명이 이미 있으면
`_dup_N`을 만들거나 덮어쓰지 않고 수동 검토 대상으로 차단합니다. 파일 이동과 관계 저장은 같은
house-ingest transaction에 묶여 관계 저장이 실패하면 기존 operation recovery로 원위치할 수 있습니다.

1.3.4는 합성 API·Folderling fixture와 전체 회귀, production build까지만 수행합니다. 운영 DB 최종
migration, Re:제로 read-only 병합 preview, 실제 alias 입고·중단 복구와 UI 조정은 1.3.5에서 진행합니다.

### 운영 전환과 관계 안전성 보강 (1.3.5)

운영 도서 서버를 재시작해 schema v12 DB를 v13으로 전환했습니다. 서버는 migration 전에
`before_library_server_schema_*.sqlite3` 백업을 만들고 SQLite 무결성 및 SHA-256을 확인했으며,
전환 뒤 Doctor, active run, unfinished operation/group을 다시 검사했습니다. 운영 DB·index·house의
활성 지원 파일은 모두 16,679개로 유지됐습니다.

첫 운영 관계는 파일을 움직이지 않는 `궁귀검신` work #3의 primary 관리 폴더 등록과 core title alias로
검증했습니다. 폴더 등록과 alias 저장은 각각 전용 backup·manifest·관리 event를 남겼고, alias는
`/txt_house/ㄱ/궁귀검신`으로 정상 해석됩니다. 실제 `Re 제로…` 데이터는 두 work가 아니라 work #1과
미연결 폴더의 조합이므로 잘못된 병합을 실행하지 않았습니다.

운영 split preview에서 같은 관리 폴더의 판본 하나만 다른 work로 옮기면 파일 경로와 폴더 관계가
엇갈릴 수 있는 경우를 발견해 차단했습니다. 분리 대상 파일이 관리 폴더 안에 있으면 그 폴더도 반드시
선택해야 하고, 같은 폴더에 선택하지 않은 variant가 남으면 먼저 물리 폴더를 나눠야 합니다. 화면은
내부 blocker 코드 대신 이 이유를 한국어로 표시하고 실행 버튼을 비활성화합니다.

관리 폴더 생성·현재 폴더 등록 화면은 숫자 ID를 직접 입력하지 않습니다. 작품명, core title, 별칭 또는
작품 번호로 검색한 결과에서 파일 수와 현재 관리 폴더 수를 확인한 뒤 작품을 선택합니다. 파일 이동
화면은 같은 작품 관계, 동일 core title, 유사 core title 순으로 기존 house 폴더를 카드형 추천합니다.
각 카드에서 추천 근거, 상대 경로, 보유 파일 수·용량과 관리 폴더 여부를 확인하고 클릭해 선택하며,
폴더명·core title·보유 파일명 검색도 같은 화면에서 수행합니다. 초성 루트는 추천 노이즈에서 제외하고,
작품 폴더 안에 이미 있는 파일은 현재 위치를 우선 표시합니다. 직접 경로 입력은 접힌 고급 항목으로
유지합니다.

폴더 상세는 내부 DB 도서마다 `빠른 제목 교정`, `이름·이동`, `사용자 승인 격리`를 바로 실행합니다.
빠른 제목 교정은 별도 페이지로 이동하지 않고 기존 `TitleCorrectionProvider`의 preview/plan/apply를
그대로 호출하는 1파일 모달이므로 교정 규칙이나 temp 재입고 로직을 복제하지 않습니다.

`폴더 전체 격리`는 DB 도서와 JPG·ZIP 같은 부속 파일, 빈 하위 폴더를
`user_folder_quarantine` operation group으로 묶어
`txt_temp/trash_bin/user_approved_folder_discard/<기존 house 상대 경로>`로 이동합니다. 실행 전 DB backup,
전체 파일 manifest, 폴더 inode, 목적지 no-clobber를 확인하고 파일별 `user_quarantine` 이력도 남깁니다.
중간 DB 실패 시 폴더 전체를 원위치로 되돌리며, DB에 아직 fingerprint가 없는 미배정 파일은 실행
직전 백업 이후 fingerprint만 보강한 뒤 같은 확인 계획으로 처리합니다. symlink, stale identity,
기존 격리 목적지 충돌은 실행 전에 차단합니다.

1.3.5 전체 회귀는 `592 passed`, TypeScript/Vite production build와 운영 Doctor 0건을 확인했습니다.
실제 동일 작품 두 work의 merge 적용과 alias에 맞는 다음 정상 신규 파일의 Folderling 입고는 인위적인
테스트 파일을 서재에 남기지 않고, 해당 실사용 사례가 생길 때 계획 확인부터 검증합니다.

외부 코드 리뷰 후속 패치는 폴더 이동·전체 격리 직전에 승인 inventory와 actual manifest를 파일별로
재검증하고, 서로 다른 work를 가리키는 사람 지정 alias가 함께 맞으면 `route_conflict`로 입고를
보류합니다. `unpack` 정리는 사전에 관찰한 파일만 identity 확인 후 삭제하고 빈 디렉터리만 제거하므로,
정리 도중 늦게 들어온 파일은 삭제하지 않고 `cleanup_failed`로 남깁니다. EPUB 감사는 압축 원본 크기
제한을 raw hash 전에 적용하고 압축 원본과 해제 본문 읽기를 모두 I/O budget에 집계합니다. 관리 API는
loopback Host와 same-origin 요청만 허용하며 서버 자체도 loopback 주소에만 바인딩합니다.

검증된 index snapshot을 deduplicator와 auditor 입력으로 직접 재사용하는 최적화는 안전성 수정과
결합하지 않습니다. 현재 중복 순회는 느릴 수 있지만 판정 계약을 유지하며, snapshot 자료형과 cache
무효화 계약을 별도 성능 패치로 설계한 뒤 적용합니다.

### 코드 리뷰 경계 안정화 (1.3.6)

1.3.6은 1.2.7 이후 전체 코드 리뷰에서 실제 재현된 파일 유실·경로 이탈·관계 오염 경계를 우선
보강합니다. `unpack` 정리는 지원 파일 검사와 삭제 inventory를 별도로 만들지 않고 한 번의 no-follow
inventory에서 지원 파일과 비지원 부속 파일을 함께 확정합니다. inventory에 지원 TXT·EPUB·PDF가 하나라도
있으면 아무것도 지우지 않으며, inventory 뒤에 도착한 파일은 삭제 목록에 없으므로 그대로 보존됩니다.
JPG·ZIP 같은 비지원 부속 파일을 정상 입고 뒤 삭제하는 기존 계약은 유지하며 추가 본문 hash는 수행하지
않습니다.

폴더 전체 격리는 사용자가 지정한 원시 경로의 house 상대 위치를 먼저 확인하고, 최종 경로 또는 중간
구성요소가 symlink이면 `resolve()`한 실제 대상을 건드리기 전에 차단합니다. 작품 분리는 선택 폴더가
선택 variant를 실제로 포함하는지 양방향으로 검사합니다. 병합·분리로 현재 관계와 모순된 활성
`distinct_work`·`same_work_distinct_variant` 판정은 history를 삭제하지 않고 `active=0`으로 퇴역시키며,
Doctor도 남은 활성 판정 모순을 `active_decision_relation_conflict`로 보고합니다.

손상되거나 크기 제한을 넘은 EPUB 후보는 `epub_analysis_error`로 auditor를 불완전 종료해 Folderling
입고를 중단합니다. 새 압축 원본 제한이 이전 분석 cache에 가려지지 않도록 fingerprint generation을
4, auditor version을 1.3.6으로 올렸습니다. mutation 재검증도 auditor와 같은 UTF-16 BOM 규칙을 사용합니다.

인덱스 생성·로드 실패는 빈 정상 인덱스로 바꾸지 않고 즉시 실패합니다. house와 로컬 확장 인덱스 배포는
동일 디렉터리의 임시 정규 파일을 SHA-256으로 확인한 뒤 `os.replace()`하며, 기존 목적지가 symlink이면
외부 대상을 따라가지 않고 중단합니다. 사용자 지정 `--state-db`도 actual 승인부터 dedup·입고·index까지
같은 경로를 유지합니다.

대표 변경은 새 대표를 `protected=1`로 만들고, 대표 격리 시 대체 후보는 활성 managed 파일로 제한합니다.
파일 탐색기는 fingerprint가 없어도 백업 이후 자동 준비하는 격리 API를 그대로 사용할 수 있습니다.
exact keep은 protected 비대표보다 대표를 우선하며 0바이트 동일 파일도 exact 판정에 포함합니다. Folderling
결과는 검토 큐·report-only·legacy pass·실제 skipped 입력을 `review_required_count`로 합쳐 UI에서 단순
성공과 구분합니다. legacy pass 집계는 `.DS_Store` 같은 Finder metadata를 제외합니다. 예상된 관리
폴더 제외 항목은 별도 `excluded_count`로만 기록합니다.

이번 버전에는 디렉터리 입고 전체를 하나의 operation group으로 resume하는 재설계, 두 index surface를
한 generation manifest로 묶는 변경, 후보 그래프 조기 cap, strong-component 자료구조 변경, Doctor·hash·
snapshot 공유, Scanner 관찰 transaction 분리는 포함하지 않습니다. 디렉터리 입고는 현재 파일별 journal로
원본·입고 파일을 보존하지만 중간 실패 후 같은 목적 폴더가 남으면 수동 확인이 필요합니다. 나머지는
판정 결과와 cache invalidation 범위를 함께 바꾸는 성능 작업이므로 별도 버전에서 fixture와 운영 측정을
준비한 뒤 적용합니다.

1.3.6 검증은 `602 passed`, Python compileall, TypeScript/Vite production build와 `git diff --check`를
통과했습니다. 테스트는 임시 fixture만 사용했으며 운영 house·temp·DB에는 변경을 수행하지 않았습니다.

### 재실행 가능한 폴더 입고와 index generation (1.3.7)

1.3.7은 분권 폴더 입고가 일부 파일에서 중단돼도 같은 폴더를 다시 넣어 이어서 완료할 수 있게 합니다.
입고 전에 모든 파일을 symlink를 따라가지 않고 inventory하고, 상대 경로·파일 identity·크기·mtime·
SHA-256·목적 경로를 `directory_house_ingest` operation group에 저장합니다. 개별 파일 입고 journal은
`operation_group_id`로 이 계획에 연결됩니다. 실패한 group을 재실행하면 이전 group이 실제로 committed한
목적 파일과 현재 DB canonical path가 모두 일치하는 항목만 재사용하고 나머지만 입고합니다. 승인 뒤
추가된 파일, 변경된 source, 변조된 destination, symlink 또는 같은 이름의 다른 bytes는 자동 합치거나
덮어쓰지 않고 실패로 남깁니다. schema는 15이며 기존 DB migration은 종전과 같이 검증된 backup을 소유한
진입점에서만 수행합니다.

Scanner는 레거시 배열 형식의 `file_list.json`을 유지하면서 `file_index.json`에 `generation_id`를 넣고,
두 파일의 SHA-256·inventory revision·normalizer version·파일 수를 `index_generation.json`에 기록합니다.
두 JSON은 generation staging에서 먼저 다시 읽어 검증하며, 프로젝트 surface는 manifest를 마지막에
게시합니다. 게시 중 실패하면 이전 세 파일을 복원하고 실패 staging은 진단용으로 남깁니다. Folderling은
프로젝트·house·설치된 로컬 확장 surface를 배포하기 전에 이전 generation을 함께 snapshot하며, house나
확장 복사 실패 시 프로젝트까지 이전 세대로 되돌립니다. 확장 폴더가 공개 저장소나 설치 환경에 없으면
선택 surface로 간주해 건너뜁니다. deduplicator·auditor·플랫폼 수집기는 manifest가 존재할 때 generation
ID와 두 hash가 맞지 않으면 stale index로 중단합니다.

리뷰 후속 보강으로 Scanner의 UI·CLI 진입점도 Folderling과 같은 house/temp root lock에 참여합니다.
따라서 다른 서버 프로세스나 복구 명령과 Scanner가 겹쳐 DB reconciliation 또는 index 게시를 동시에
진행하지 않습니다. 디렉터리 입고가 `planned` 상태에서 강제 종료돼도 recovery가 자식 operation을 먼저
정리한 뒤 manifest·source·destination·DB를 비교해 완료된 group은 확정하고 나머지는 재실행 가능한 실패
상태로 전환합니다. unpack 부속 파일 정리는 고정한 directory FD와 `O_NOFOLLOW`를 사용하며, 일부 삭제 뒤
오류가 나면 실제 삭제한 파일 수와 bytes를 결과에 남깁니다.

프로젝트·house·로컬 확장 배포에는 지속 deployment journal을 남깁니다. 재시작 시 검증된 프로젝트
generation을 미완료 surface에 다시 게시하고 journal을 제거하며, snapshot 뒤 다른 프로세스가 만든 파일이나
바꾼 surface는 소유권을 증명할 수 없으므로 삭제하거나 이전 backup으로 덮지 않습니다. Scanner 자체 게시의
rollback도 복원 실패 시 backup을 보존합니다. standalone deduplicator의 확장 index 게시 역시 임시 정규
파일과 no-follow 원자 교체를 사용합니다.

사람 관계 작업은 병합·분리 preview 시 퇴역 예정 decision ID·판정·사유를 plan SHA에 포함해 확인 뒤 생긴
판정을 조용히 퇴역시키지 않습니다. 대표 파일 격리·폴더 전체 격리는 활성 구성원과 `managed` 대체 대표를
구분하며, 적격 대표가 없으면 variant를 잘못 퇴역시키거나 미관리 파일을 대표로 승격하지 않습니다. exact
그룹도 대표가 아닌 keep 후보는 복사 전에 report-only로 전환합니다.

파일이 하나도 없는 하위 디렉터리는 도서 의미 데이터가 아닌 컨테이너로 취급해 목적지에 복제하지 않습니다.
파일이 들어 있는 경로 구조만 재생성하며, 빈 source shell은 모든 승인 파일이 정상 입고된 뒤 정리합니다.

EPUB/PDF의 `작품명 38 (작가)` 형식은 단일 파일만으로도 작가가 명시되고, 완결·범위 표기가 없으며,
숫자가 1~99일 때 `38권`과 같은 volume 좌표로 추론합니다. 1.4.11부터 TXT나 작가 없는 숫자도 위의
**숫자 권 문맥 추론**이 성립할 때만 승격하며, 근거 없는 단독 숫자와 100 이상의 합본 회차는 기존 판정을
유지합니다. 따라서 `Re … 5`와 `Re … 5권`은 문맥이 확인되면 같은 권 좌표로 비교되어 exact·본문 감사·
동일 권 충돌 검사를 우회하지 않습니다. 기존에 하나의 managed work로 정리된 권수 폴더가 있으면 같은
core의 미관리 합본이 초성 루트에 남아 있어도 관리 집합을 목적지 근거로 사용하되, 미관리 파일의 같은
권 좌표도 충돌 검사에는 계속 포함합니다.

기존 house 작품이 없는 신규 EPUB/PDF도 같은 temp 배치에 동일 core·동일 작가의 권수가 2개 이상 있고,
중복 없이 연속된 정수 권수일 때만 `초성/읽기 제목` 폴더를 새로 만들어 한 work의 서로 다른 variant로
입고합니다. 권수 중복·누락, 작가 충돌, 혼합 좌표, 심볼릭 링크 또는 기존 목적지 충돌은 자동 묶지 않습니다.

Folderling 결과와 구조화 event에는 snapshot·dedup·intake·index 생성·배포·final Doctor의 단계별 시간을
기록합니다. auditor의 후보 수, 실제 read bytes, fingerprint/pair cache hit·miss도 dedup summary에 포함합니다.
검증된 snapshot entry는 deduplicator 첫 입력으로 직접 전달해 같은 `file_index.json`을 한 번 더 parse하는
작은 중복 I/O를 제거했습니다. 후보 graph 조기 cap, Scanner transaction 재설계, final Doctor 축소와
mutation 직전 SHA 재검증 제거는 판정·안전 gate 영향에 비해 운영 병목 근거가 부족해 이번 버전에서
적용하지 않았습니다.

재개·generation·강제 종료·동시 실행·symlink 경계 테스트는 임시 fixture만 사용합니다. 운영 적용 후에는 작은 신규 분권 폴더 한 건을
입고해 operation group committed, unfinished operation/group 0, final Doctor 0과 세 surface generation ID를
확인한 뒤 버전을 닫습니다. 리뷰 후속 코드 검증은 `628 passed`, Python compileall, TypeScript/Vite production
build와 `git diff --check`를 통과했습니다.

도서 관리 서버는 macOS SQLite WAL의 `-wal`/`-shm` coordination 파일을 안정적으로 유지하도록
query-only normal keeper를 서버 수명 동안 보유합니다. `/health`도 DB 파일 존재만 보지 않고 실제
읽기 전용 연결을 열어 확인하므로 DB가 열리지 않으면 503으로 보고합니다. 코드 변경을 자동으로
hot reload하지는 않습니다. 장시간 Folderling·플랫폼 작업을 중간에 끊을 수 있기 때문에, 배포한
코드는 컨트롤서버에서 `도서 관리` 서버만 한 번 재시작해 적용합니다.

```bash
# Python 의존성
python3 -m pip install -r requirements.txt

# 웹 화면 빌드
cd library_frontend
npm ci
npm run build
cd ..

# 운영 서버 시작
python3 run_library_server.py

# 경로와 포트를 바꿀 때
python3 run_library_server.py --help
```

컨트롤서버의 `Servers`에는 이 저장소를 작업 디렉터리로 하고 다음과 같은 명령을 일반 서버로
등록하면 됩니다. 컨트롤서버가 도서 DB를 직접 열거나 파일을 이동할 필요는 없습니다.

```text
command: .venv/bin/python run_library_server.py --server waitress --host 127.0.0.1 --port 9012
health:  http://127.0.0.1:9012/health
url:     http://127.0.0.1:9012/
```

제목 교정 화면은 새 파일명의 확장자를 자동 보존하고 같은 Python normalizer로 변경 후
`core_title`을 미리 보여줍니다. 실행 전 대상 건수와 plan SHA-256을 다시 확인합니다. 승인된
파일만 house에서 `txt_temp`로 이동하며 기존 DB 파일 행은 삭제하지 않고 비활성 이력으로
남깁니다. 다음 Folderling은 이를 새 입고 파일로 처리해 기존 중복 판정을 전부 다시 수행합니다.
대표·보호·관리 관계가 있는 파일은 1.2.8 화면에서 변경하지 않고 차단합니다.

제목처럼 보이는 등급·상태어를 실제 제목으로 보존할 때는 `[[19금]]`, 제목이 아닌 사람이 지정한
구조 정보를 운반할 때는 `{{힌트}}`를 사용합니다. 두 표시는 temp에서만 분석 의도를 전달하고
house 입고 파일명에서는 괄호가 제거됩니다. 구조 힌트는 기반 문법만 제공하며 실제 해석은 검증된
규칙부터 추가합니다. `숫자.숫자권`은 별도 힌트 없이 명시적 소수 권수로 인식합니다. 예를 들어
`작품 04.5권.epub`은 작품 core에 `04`를 남기지 않고 정확한 `4.5권` 좌표로 저장됩니다.

`/review/volumes`는 DB의 권·부·상중하 좌표와 현재 폴더 위치를 읽어 `자동 가능`, `검토 필요`,
`이미 한 폴더`, `제외`로 분류합니다. 포함 파일과 결과 트리, 이동 건수와 plan SHA-256을 다시
확인한 항목만 실행할 수 있습니다. 실행 시 모든 원본을 `txt_temp/.volume_group_staging`에 먼저
복사·검증하고 DB backup, actual-run manifest, `volume_group_merge` journal 아래에서 한 작품
폴더와 하나의 work로 묶습니다. 권 좌표가 겹치거나 작가·판본·목적 파일이 충돌하면 실행하지
않습니다. 다만 사용자가 같은 좌표의 서로 다른 판본을 함께 보관한다고 승인해 이미 한 폴더와
한 work로 연결한 경우에는 다시 충돌로 되돌리지 않습니다. 화면에는 `이미 한 폴더`로 표시하되
승인된 중복 권 좌표와 각 파일의 판본 정보는 상세 근거로 계속 보여줍니다.

목록 조회는 매 요청마다 파일명을 전부 다시 해석하지 않습니다. 현재 normalizer version과
파일 identity가 일치하는 행은 DB에 저장된 분석을 사용하고, 같은 DB revision에서 검색·정렬·페이지
요청이 반복되면 한 번 계산한 작품 그룹을 재사용합니다. 캐시는 시간으로 만료하지 않고 DB 본체나
WAL revision이 실제로 바뀌거나 분권 이동이 완료된 경우에만 무효화합니다. 서버 시작 시에도 목록을
백그라운드에서 미리 준비하므로 화면 요청이 전체 라이브러리 분석을 직접 떠안지 않습니다.

Folderling은 이미 정상적인 작품 폴더에 들어 있는 분권들과 제목·작가·권 좌표가 명확히 맞고
좌표가 겹치지 않는 신규 파일을 같은 폴더와 work에 자동 연결합니다. 마지막 `all_auto_ready` 단계는
이번 입고뿐 아니라 기존의 흩어진 분권도 함께 재평가해 자동 재배치하며, 애매한 후보만
`/review/volumes`에서 사람이 확인합니다.

완료·실패·서버 중단 작업은 `.dedup_state/library-server/`에 남습니다. 이 디렉터리와 실제
라이브러리 경로, 운영 DB, 인증 정보는 Git에 포함되지 않습니다.

`/settings`에서는 컨트롤서버와 같은 방식으로 배경·주요 글자·포인트 컬러를 직접 지정하거나
기본 프리셋을 선택할 수 있습니다. 저장된 세 색상에서 패널, 입력창, 테두리, 활성 메뉴 색상을
자동 계산해 전체 화면에 적용합니다. 성공·주의·오류 배지뿐 아니라 타임라인, 보고서, 카탈로그 상세,
작업·로그 화면도 고정된 다크 색상을 쓰지 않고 같은 테마에서 파생됩니다. 파일 카탈로그의 열린 검토는
검토 상대 제목을 먼저 보여주고 그 옆의 작은 `검토 N건` 링크로 상세를 엽니다. 설정은 브라우저 캐시와
`.dedup_state/library-server/appearance.json`에 함께 저장되며 Git에는 포함되지 않습니다. 설정 화면에서는
현재 편집한 세 색상을 사용자 프리셋으로 추가·삭제할 수 있습니다. 사용자 프리셋도 서버의
`appearance-presets.json`과 브라우저에 함께 보관하며, 내장 프리셋은 삭제되지 않습니다.

## 제목 정규화 후보 감사 (1.2.7)

1.2.7 후보 감사기는 현재 SQLite와 `file_index.json`을 읽기 전용으로 비교해, 파일명에서
명확한 회차 문법을 복원하고 배포 꼬리표를 제거했을 때 바뀌는 readable/query/core 제목을
보고합니다. `ⓒ/©작가`와 `[완] - 작가`처럼 확정할 수 있는 작가 정보는 지우지 않습니다.
저작권 표식이 있던 작가는 숫자 필명도 확실히 구분할 수 있도록 `[ⓒ작가]`로, 일반 작가는
기존 파일명 관례인 `[작가]`로 보존합니다.
네이버 시리즈·카카오페이지·노벨피아 중 하나라도 기존 `ok`인 source 제목이 바뀌면 종료
코드 3으로 실패합니다. 새 core가 기존 target과 만나는 경우에는 자동 병합하지 않고 별도
중복처리 대상으로 집계합니다.

```bash
# DB/index/house 및 플랫폼 데이터를 변경하지 않는 전수 감사
python3 run_title_cleanup_candidates.py

# 로컬 검토 보고서 생성
python3 run_title_cleanup_candidates.py \
  --json-out .dedup_state/reports/title_cleanup_1.2.7.json \
  --csv-out .dedup_state/reports/title_cleanup_1.2.7.csv
```

감사기는 실행 전후 SQLite 논리 snapshot과 index SHA-256이 같은지 확인합니다. 보고서에는
규칙별 문법 일치 수, 실제 변경 source 수, `not_found`/오류 상태, target 충돌과 보호 target
충돌이 포함됩니다. normalizer 버전 변경 뒤 `file-metadata-sync`는 기존 target 충돌이나 여러
source가 같은 target으로 모이는 경우 전체 트랜잭션을 중단하므로, 충돌 파일의 중복처리가
끝나기 전에는 catalog key와 성공 메타데이터가 합쳐지지 않습니다.

후보를 실제 파일에 적용하는 별도 진입점도 기본값은 dry-run입니다. dry-run은 교정 파일명,
house 원본 identity, temp 목적지 충돌, assignment/protection 상태를 다시 확인하고 적용
manifest SHA-256을 출력합니다.

```bash
# 실제 파일/DB 변경 없음
python3 run_title_cleanup_apply.py \
  --manifest-out .dedup_state/reports/title_cleanup_requeue_1.2.7.json

# 실제 실행은 직전 dry-run의 건수와 plan SHA-256을 둘 다 명시해야 함
python3 run_title_cleanup_apply.py --run \
  --confirm-count DRY_RUN_COUNT \
  --confirm-plan-sha256 DRY_RUN_PLAN_SHA256
```

실제 실행은 SQLite backup과 전체 house/temp actual manifest를 만든 뒤, 공용 mutation lock과
operation journal 아래 교정 파일을 `txt_temp`로 옮깁니다. 기존 file ID와 fingerprint는
비활성 이력으로 남기며 temp 경로에 연결하지 않습니다. 따라서 다음 Folderling 원버튼은
파일을 새 intake ID로 등록하고 기존 exact hash·회차·포맷·본문 중복처리를 그대로 수행합니다.
정리 후 이름이 같은 파일은 파일명에 `_dup_N`을 다시 붙이지 않고
`txt_temp/title_cleanup_collision_N/` 임시 하위 폴더에 분리해 두 파일을 모두 보존합니다.
삭제 여부는 Folderling 중복 증거로 결정합니다. 중단된 `planned/fs_done` 이동은 기존 recovery가
원래 house 경로로 되돌릴 수 있습니다.

## 플랫폼 카탈로그와 조회용 Google Sheet (1.2.6)

Folderling과 별개로, 보유 작품의 플랫폼별 최신 인기·평점 지표를 상태 DB에 보관합니다.
기본 실행은 아직 값이 없는 플랫폼만 최대 25개 제목씩 조회합니다. 한 제목의 Naver·Kakao·
Novelpia는 최대 3개 worker로 병렬 조회하되, 같은 플랫폼에는 동시에 요청하지 않습니다.
한 제목이 끝나면 1초 뒤 다음 제목을 시작합니다. 파일 이동·삭제와 file_index.json 갱신은
하지 않습니다.

병렬성은 제목 안에서만 사용합니다. 한 제목의 시리즈 결과가 이미 있으면 카카오·노벨피아
두 요청만 병렬 실행하고 세 번째 worker는 쉬며, 다른 제목을 같은 묶음에 섞지 않습니다.

실제 수집은 시작 즉시 대상 건수를 출력하고 첫 작품 및 이후 10작품마다 진행률·상태 누계·
예상 잔여시간을 출력합니다. 중간 취소 시 이미 끝난 작품은 DB에 남아 다음 실행에서 건너뜁니다.

~~~bash
# schema v10/file_analysis 대상만 확인하거나 실제 backfill
python3 run_platform_catalog.py file-metadata-sync --dry-run
python3 run_platform_catalog.py file-metadata-sync

# terminal control server의 플랫폼 DB 버튼 대상
python3 run_platform_catalog.py refresh --all

# 이미 성공값이 있는 플랫폼만 재조회해 증가한 인기값과 평점을 반영
python3 run_platform_catalog.py refresh-existing --all

# 네트워크/DB 변경 없이 다음 대상만 확인
python3 run_platform_catalog.py refresh --dry-run

# 수집 현황 및 지표별 상위 작품
python3 run_platform_catalog.py status
python3 run_platform_catalog.py top --order-by series-download --limit 20

# 현재 not_found/error를 플랫폼 쌍 규칙에 따라 재검사
python3 run_platform_catalog.py retry-failed --dry-run
python3 run_platform_catalog.py retry-failed

# 현재 세 플랫폼 모두 not_found인 작품을 인증 노벨피아 검색으로 한 번 보완
python3 run_platform_catalog.py retry-novelpia-auth --dry-run
python3 run_platform_catalog.py retry-novelpia-auth
~~~

첫 실제 실행에서 이전 DB는 schema v10으로 전환되며, 전환 전 SQLite backup을
.dedup_state/backups/에 남깁니다. 일반 Scanner/감사기는 schema를 자동 변경하지 않으며,
backup을 소유한 플랫폼/원버튼 진입점만 명시적으로 migration합니다.

schema v10의 `file_analysis`는 `core_title`, 표시 제목, 플랫폼 검색 제목, 작가·완결·회차
정보를 파일별로 보관합니다. Scanner는 파일명을 한 번만 분석해 DB와 `file_index.json`에
같이 반영하며, 플랫폼 수집기는 파일명을 다시 파싱하지 않고 이 테이블만 읽습니다.

플랫폼 검색에는 파일명의 압축 key 대신 회차·완결·작가 표기만 제거한 읽기 쉬운 제목을
사용하며 `메인 제목: 부제목`은 전체를 보존합니다. 최종 결과는 사이트가 붙인 총 회차와
`[단행본]`·`[독점]`·`[미니노블]` 표시만 제외한 전체 제목이 정확히 같고 core도 같을 때만
채택합니다. 일반 `refresh`는 처음 기록된 `not_found`와 `error`를 시간이 지나도 자동으로
재조회하지 않고, 아직 플랫폼 행이 없는 작품만 이어서 수집합니다. 실패 결과는 명시적인
`retry-failed`에서만 다시 조회합니다. 시리즈나 카카오 중 하나가 성공이면 그 두 플랫폼 중
실패한 부분만 재검사하고, 노벨피아만 성공한 작품은 건너뜁니다. 세 플랫폼이 모두
`not_found/error`이면 세 플랫폼을 모두 한 번씩 확인합니다. 버튼은 다시 사용할 수 있으며
매 실행 시점의 현재 실패 상태를 새로 판단합니다. 한 회차의 시작 cutoff를 DB에 저장하므로
중단 후 같은 버튼을 누르면 이미 시도한 행은 건너뛰어 이어서 처리하고, 완주 후 다음 클릭은
새 회차로 시작합니다.
`refresh-existing`은 현재 상태가 `ok`이고 대표 인기 수치가 있는 플랫폼만 다시 조회합니다.
시리즈는 다운로드 수, 카카오는 조회 수, 노벨피아는 조회 수 또는 추천 수가 기존보다 증가한
경우에만 결과를 채택합니다. 채택할 때 모든 카운트는 `max(기존, 신규)`로 저장해 감소를
막고, 시리즈·카카오 평점은 그 시점의 새 값으로 함께 갱신합니다. 동일·감소·검색 실패
결과는 기존 성공 행을 덮지 않습니다. 공개 노벨피아 검색이 `not_found`이면 설정된 인증
세션으로 한 번 더 확인합니다.
노벨피아는 비로그인 검색에서 19금 작품을 숨길 수 있습니다. 아래 두 환경변수가 모두
설정된 일반 `refresh`와 `retry-failed`는 공개 3플랫폼 조회 결과가 모두 `not_found`인 제목만 인증된
노벨피아 검색으로 즉시 한 번 더 확인합니다. 네이버나 카카오에서 찾은 작품은 인증
노벨피아 보완 대상에서 제외합니다.

~~~bash
# 두 환경변수는 실행 셸 또는 컨트롤서버 launchd/run.env에서 미리 설정
python3 run_platform_catalog.py refresh --all --require-novelpia-auth
~~~

계정값은 코드·DB·명령 인자·로그에 기록하지 않습니다. 컨트롤서버에서는 Git 제외된
`launchd/run.env`에만 값을 넣고 LaunchAgent를 다시 설치해야 합니다. 로그인 전에 CAPTCHA가
요구되거나 성인 본인인증이 끝나지 않은 계정이면 공개 결과를 `not_found`로 덮지 않고 즉시
실패합니다. 인증 검색 결과는 20작품씩 메모리에 보류하고 작은 성인모드 응답으로 세션을
한 번 확인한 뒤에만 DB에 저장합니다. 세션 만료면 환경변수에서 계정을 다시 읽어 로그인하고
그 20작품만 다시 검색합니다. 재로그인·재검증까지 실패하면 해당 구간을 DB에 쓰지 않고
실행을 중단하므로 다음 실행에서 그대로 이어집니다.
`retry-novelpia-auth`는 기존 세 플랫폼 `not_found`만 시작 시 cutoff 기준으로 한 번 처리하므로
중단 후 재실행해도 이미 인증 검색을 마친 행을 건너뜁니다.
Kakao는 최신 BFF search/overview JSON API를 사용하며 일시 오류는 재시도 가능한
`error`로 남기며, Kakao 검색은 동명 웹툰을 피하도록 웹소설 분류로 제한합니다.
`catalog_title_metrics` view에는 시리즈 다운로드·평점, 카카오 조회·평점,
노벨피아 조회·추천의 여섯 컬럼이 있습니다.

응답 구조가 예상과 다르면 `not_found`가 아니라 수동 재검사 대상인 `error`로 기록합니다.
마지막 성공 지표는 DB에 보존하지만
`top` 명령은 현재 상태가 `ok`인 지표만 상위 목록에 표시합니다.

SQLite를 친구와 함께 확인하기 위한 Google Sheet는 완전한 단방향 카탈로그입니다. Sheet에서
수정한 값은 DB로 가져오지 않으며, 동기화 중 SQLite는 `mode=ro`와 `query_only`로만 엽니다.
`도서 목록`은 플랫폼 검색 성공 여부가 아니라 활성 보유 작품을 기준으로 한 작품당 한 행을
만듭니다. A열 원본 도서명·B열 보유 범위·C열 작가와 상단 두 행을 고정하고, 그 뒤에 플랫폼별
`작품명 → 다운로드/조회/좋아요 → 평점 → 링크` 묶음을 차례로 표시합니다. 링크는 URL
문자열 대신 `열기` 하이퍼링크로 만듭니다. `not_found`·`error`·미조회 플랫폼도 도서 행은
유지하고 해당 플랫폼 정보만 빈칸으로 둡니다. `수집 오류`에는 재시도가 필요한 실제
`error`만 표시합니다.

~~~bash
# 의존성 설치
python3 -m pip install -r requirements.txt

# Google/SQLite 변경 없이 예상 행 수 확인
python3 run_platform_catalog.py sheet-sync --dry-run

# Sheet만 갱신
FILE_CHECK_GOOGLE_CREDENTIALS=/ignored/path/service-account.json \
FILE_CHECK_GOOGLE_SPREADSHEET_ID=spreadsheet-id \
python3 run_platform_catalog.py sheet-sync

# 플랫폼 수집 성공 후 Sheet까지 연속 실행
python3 run_platform_catalog.py refresh --all --sync-sheet
~~~

빈 Spreadsheet를 하나 만든 뒤 서비스 계정 이메일에 편집 권한을 공유해야 합니다. 인증 JSON과
Spreadsheet ID는 Git에 넣지 말고 로컬 환경변수나 아래 private 설정에만 둡니다. 동기화는 임시 탭에
전체 값을 먼저 쓴 뒤 성공한 경우에만 `도서 목록`, `수집 오류` 탭을 교체합니다. 이전 버전의
`작품 현황` 탭이 있으면 성공적인 첫 동기화 때 정리합니다.

컨트롤서버·launchd·PM2가 앱별 환경변수를 전달하지 않는 설치에서는 owner-only private 설정을
사용할 수 있습니다. 기본 경로는 `~/.config/book_check/google-sheet.json`이며 권한은 `0600`,
내용은 아래 두 키입니다. 두 환경변수가 모두 있으면 환경변수가 우선하며, 한쪽만 있으면 private
설정과 섞지 않고 오류로 처리합니다. 다른 경로를 쓰려면 `FILE_CHECK_GOOGLE_CONFIG`로 지정합니다.

~~~json
{
  "credentials_path": "/local-only/path/service-account.json",
  "spreadsheet_id": "spreadsheet-id"
}
~~~

## 테스트

```bash
python3 -m pytest -q public_tests
```

공개 테스트는 임시 디렉터리의 합성 파일만 사용하며 실제 라이브러리를 변경하지 않습니다.
개인 운영에서 축적된 전체 회귀 fixture는 실제 제목 형태가 포함될 수 있어 공개 저장소에서
제외합니다.
