# file_check 1.4.0 — 버전형 전수 중복 감사와 house 재기준

- 상태: 구현·운영 재기준 진행 중
- 시작 기준: `43ce099` (`fix: finalize v1.3.9 relationship choices`)
- 적용일: 2026-07-28
- 범위: 격리 불변식, 제목 무관 콘텐츠 중복 회수, 증분 fingerprint 전수 감사,
  실제 `txt_house` 정리와 수동 예외 기록

## 1. 목표

1. 최신 로직을 거치지 않은 기존 house 도서를 한 번 전수 재기준한다.
2. 이후에는 파일 identity와 감사 버전이 바뀐 항목만 다시 읽는다.
3. 제목이 달라도 전체 TXT 정규화 hash 또는 EPUB 내부 콘텐츠 hash가 같으면 후보로 회수한다.
4. 강한 의심 도서는 삭제하지 않고 journal quarantine에 보관하며 keep/discard 근거와 복구 경로를 남긴다.
5. 일반화하면 오탐 비용이 큰 cross-title near/contained 예외는 수동 계획으로 기록해 같은 안전 경계로 정리한다.

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

- [ ] 새 auditor를 full sweep으로 실행하고 구조화 report 저장
- [ ] 자동 회수 가능한 strong review를 journal quarantine
- [ ] cross-title strong-anchor 예외를 수동 JSON plan으로 quarantine
- [ ] 확인된 false-positive 3건을 house로 복원하고 `distinct_work` 기록
- [ ] 모든 격리에 원본·keep·근거·operation/run ID·복구 경로 기록

### D. 완료 검증

- [x] 전체 Python 회귀와 frontend production build
- [ ] final Doctor 0
- [ ] unfinished operation/group 0, active run 0
- [ ] 실제 house와 `file_index.json` 지원 파일 일치
- [ ] warm auditor에서 기존 fingerprint 재사용 확인
- [ ] 최종 strong 후보가 예상된 보존 판본·수동 보류만 남는지 재감사

## 3. 자동화와 수동 예외의 경계

- 자동 strong: 전체 TXT normalized hash 동일, EPUB 내부 콘텐츠 hash 동일.
- review-only: near-identical, contained-version, 앞/뒤 문구만 다른 판본.
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
- 실행 계획: 수동 격리 44권, false-positive 복원 3권, `same_work_distinct_variant` 10쌍, unresolved 보존 2쌍. 모든 실행 항목은 양쪽 현재 raw SHA-256에 묶였다.
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

완료 시 아래를 실제 값으로 갱신한다.

- 구현 커밋:
- 전수 auditor report:
- 자동 strong 격리 report/run:
- 수동 예외 plan/report/run:
- false-positive 복원 decision:
- 최종 index generation:
- 최종 검증:
