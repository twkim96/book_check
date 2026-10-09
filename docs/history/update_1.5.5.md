# 중복 정책 1.5.5와 ctime 검증 보완

## TXT 본문 95% 판정

- 같은 작품의 명시된 본편·외전 회차 범위가 같고 순서형 본문 증거가 95% 이상이면 정규화 본문이 작은 파일을 복구 가능한 quarantine으로 보낸다. 길이 차이가 5% 미만이어도 긴 본문을 보존한다.
- 한쪽의 본편·외전 범위가 명확히 넓고 본문 증거가 충분하면 넓은 범위를 보존한다. `450+외전5`, `1-450화 + 외전5`와 에필로그 좌표를 구분한다.
- 외전 재배치, 화·권 교차, 범위가 명확하지 않은 관계는 자동 격리나 warning 이동 없이 원래 경로에 남긴다.
- 최소 본문량, 반복 줄 제외, 순서·연속 불일치 한도, 이동 직전 pinned 본문 검증과 보호·대표·판본·사람 보존 경계는 유지한다. 기존 legacy 마커 예외는 마커 사본만 폐기하고 clean 사본을 보존한다.
- auditor와 pair/strong-proof 정책 세대를 갱신하여 이전 판정 cache를 새 정책의 증거로 재사용하지 않는다. 서버/UI 세대는 1.5.4, auditor 정책 세대는 1.5.5다.

## ctime만 변경된 파일

- manifest·backup·copy destination·active-run 파일은 dev/inode/size/mtime와 전체 SHA가 같을 때만 ctime을 정정한다. 단일-link, no-follow descriptor와 검증 중 pathname 교체 검사를 유지한다.
- manifest는 현재 UID의 `0600` 파일이어야 한다. 검증 기록과 현재 projection만 갱신하며 기존 manifest bytes·SHA, fingerprint와 완료 journal은 보존한다.
- 명시 복원은 원 quarantine journal의 inode/size/mtime/SHA를 확인하고 새 preview plan에 현재 identity를 고정한다. 복원 뒤 현재 분석과 fingerprint 연결을 갱신한다.
- Scanner는 검증된 ctime 변경에서 사람 판정과 대표·판본 연결을 보존한다. 내용 변경·inode 교체·symlink/hardlink는 기존 차단·재검토 경로를 유지한다.
- drift가 없으면 기존 빠른 경로를 유지한다. 새 manifest는 compact JSON으로 기록하며 기존 증거를 다시 쓰지 않는다.

## 격리량 집계

중복 정리 summary에 실행 전후 파일 수·bytes·카테고리와 증가량, 계획/생성 수량·bytes를 기록한다. 이 집계 자체가 자동 영구 삭제를 승인하지 않는다.

## 검증과 적용

관련 회귀는 `public_tests/test_ordered_body_dedup_1_4_1.py`, `test_epub_duplicate_audit.py`, `test_library_management.py`와 네 ctime 회귀 파일이다. 공개 테스트는 합성 임시 파일을 사용하며 개인 `tests/` helper에 의존하지 않는다.

코드 게시와 실행 중인 서버 적용은 별도 단계다. 서버 재시작이나 실제 라이브러리 이동·삭제를 실행한 것으로 해석하지 않는다. 기존 actual-run backup·manifest·journal·root lock과 Doctor·SHA 검증을 거쳐 적용한다.
