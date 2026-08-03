# file_check 1.4.11 — 숫자 권 문맥 추론과 자동 시리즈 합류

## 목표

1. `작품명 1`, `작품명 2`처럼 `권`이 생략된 분권을 별도 입고 폴더 없이 현재 inventory 문맥으로 판정한다.
2. house의 기존 단독 파일과 temp의 신규 파일도 서로를 증명해 같은 실행에서 작품 폴더로 묶이게 한다.
3. 단독 숫자·날짜·합본 회차·외전·작가 충돌은 제목 일부를 잘못 자르지 않도록 fail-closed한다.
4. 새 권 좌표가 기존 중복 안전선을 우회하지 않으며, warm 실행에서 본문 재독해와 전체 house 재-stat을 만들지 않는다.
5. 사용자가 밝은 배경을 선택해도 타임라인·보고서·카탈로그 상세 등 모든 상태 화면이 같은 테마를
   따르게 하고, 파일 목록의 검토 상태가 제목보다 먼저 시선을 빼앗지 않게 한다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.11`
- Python/Chrome `NORMALIZER_VERSION`: `1.3.2`
- DB schema: `v15` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.2` 유지
- fingerprint/pair normalizer compatibility: `1.3.0` 유지
- archive object/version: `1.4.10` 유지

이번 변경은 파일 본문 정규화나 pair classification 공식을 바꾸지 않는다. Scanner의 파일명 분석·index
projection을 새 generation으로 만들기 위해 normalizer 버전만 올리며, 기존 TXT/EPUB fingerprint와 pair
cache를 용량 큰 본문 sweep으로 무효화하지 않는다. archive 형식도 바뀌지 않아 1.4.10 객체와 복원 계약을
그대로 사용한다.

## 밝은 테마와 검토 목록 가독성

- Folderling 타임라인·결과, 카탈로그 플랫폼 상태와 펼친 상세, 누적 보고서, 작업·이벤트·로그,
  검토/격리/분권 상태면의 고정 다크 배경과 고정 밝은 글자를 제거했다.
- 성공·주의·오류·중립 표시는 사용자가 저장한 배경·본문·포인트 색에서 파생한 semantic token을 공유한다.
  밝은 테마에서도 상태 글자가 흐려지지 않도록 본문색 비중을 높였고, 포인트 위 글자도 실제 명도에 따라
  검정/흰색 중 대비가 높은 쪽을 선택한다. 보조 설명색도 밝은 사용자 팔레트에서 일반 크기 글자의
  대비를 유지하도록 본문색 비중을 높였다.
- 파일 카탈로그의 `열린 검토 N건` 큰 배지는 제거했다. 검토 상대 파일명을 행의 첫 줄에 두고,
  같은 줄 오른쪽의 작은 `검토 N건` 링크로 상세 창을 여는 구조로 바꿨다.
- 설정 화면에서 현재 편집 중인 세 색상에 이름을 붙여 사용자 프리셋을 추가하고 삭제할 수 있다.
  사용자 프리셋은 최대 24개이며 서버의 `appearance-presets.json`과 브라우저 저장소에 함께 남는다.
  이름 중복과 내장 프리셋 이름 재사용은 차단하고, 내장 프리셋 자체는 삭제 대상이 아니다.
- 테마 설정 저장 형식과 기존 localStorage/서버 설정값은 그대로 사용한다. 중복 판정·격리·도서 이동
  정책에는 변화가 없다.

## 최종 판정 계약

### 별도 폴더가 필요 없는 inventory 문맥

파일 하나만 보면 제목 숫자인지 권수인지 알 수 없는 꼬리는 그대로 둔다. 다음 중 하나가 현재 house/temp
inventory에서 증명될 때만 `1~99`의 독립된 마지막 정수를 권 좌표로 승격한다.

1. 같은 후보 core에 서로 다른 bare 숫자가 2개 이상 있음
2. 같은 core에 `N권`/`N부`처럼 명시적 분권 좌표가 있음
3. 같은 core가 이미 한 managed work로 승인되어 있음

대표 흐름은 다음과 같다.

```text
house: 판타지소설 1.txt
temp : 판타지소설 2.epub

inventory 문맥 확인
→ 두 파일의 core_title = 판타지소설
→ 좌표 = 1권, 2권
→ 기존 all_auto_ready 시리즈 단계가 초성/판타지소설 폴더로 묶음
```

파일명은 `권`을 강제로 삽입하거나 바꾸지 않는다. DB `file_analysis`, `files` coordinate, 최종
`file_index.json`만 같은 의미를 공유하고, 기존 journaled volume-group mutation이 실제 폴더 이동을 맡는다.

### 오탐 방지 경계

- 두 작가가 모두 명시되어 서로 다를 때만 자동화를 차단한다. 작가 누락은 충돌이 아니다.
- `1-100`, 명시적 `화/권/부/장/편`, 100 이상, 날짜형 꼬리, 숫자뿐인 제목, 모호한 span,
  `〔D2〕` 판본, 사용자 `[[제목]]` override는 bare-number 규칙으로 재해석하지 않는다.
- `외전 1`은 본편 1권으로 사용하지 않는다. 단권+외전과 외전+외전은 계속 사람 검토이며,
  서로 다른 본편 좌표 2개 이상과 외전이 함께 있는 경우만 기존 계약대로 자동 묶는다.
- `10 소책자 한정판`처럼 닫힌 판형 꼬리는 10권 좌표를 가질 수 있지만 파일명은 보존한다. 일반 10권과
  함께 존재하면 동일 좌표 충돌/본문 중복 단계가 먼저 판단하며 제목만으로 삭제하지 않는다.
- 서로 다른 1권·2권은 중복이 아니라 형제 권이다. 같은 좌표만 기존 SHA/TXT/EPUB 본문 증거로 경쟁한다.

### 전체 실행 경로 일치

- Scanner는 전체 house identity를 한 번 기록한 뒤 DB에서 문맥 core/coordinate를 계산하고 같은 in-memory
  index에 반영한다. 두 번째 filesystem walk나 본문 read는 없다.
- auditor는 검증된 house snapshot과 temp entry를 합친 뒤 같은 순수 문맥 함수를 사용하므로 후보 생성과
  DB review 방향이 어긋나지 않는다.
- cache-write auditor와 Folderling은 temp 좌표를 intake 전에 DB에 투영한다. house singleton과 temp
  singleton도 서로를 증명할 수 있다.
- temp→house journaled ingest는 증명된 bare core/coordinate를 보존한다. 모든 intake 뒤 house 문맥을 다시
  계산하고 기존 `all_auto_ready` backlog까지 자동 적용한다.
- 현재 normalizer projection과 file identity가 같은 warm row는 SQL 비교만 하고 `os.stat()`이나 본문 읽기를
  반복하지 않는다. 숫자 꼬리 모양이 아닌 이름은 cheap syntax prefilter에서 끝난다.

## catalog/index 정합성

Scanner와 platform metadata sync는 과거 core에서 **최종 문맥 core**로 직접 rekey한다. 중간의 raw-name
core를 migration으로 기록하지 않으므로 warm rescan에서 `A → B → A` chain/cycle이 생기지 않는다.
기존 catalog target 충돌 검사는 실제 쓰기 전에 계속 중단하며, 사람 title override도 보존한다. 여러 권의
과거 title key가 같은 작품 key로 합쳐질 때 동일 플랫폼의 성공값이 둘 이상이면 파일명 정렬로 하나를
임의 채택하지 않는다. 그 플랫폼 값만 폐기해 새 작품명으로 재조회하고, 서로 다른 플랫폼의 단일 성공값과
이미 성공한 canonical target은 기존 계약대로 보존한다.

`NORMALIZER_VERSION=1.3.2` 때문에 최초 실행은 파일명 분석과 index를 한 번 재생성한다. 이는 본문
fingerprint sweep이 아니며, `FINGERPRINT_NORMALIZER_COMPAT_VERSION`과
`PAIR_NORMALIZER_COMPAT_VERSION`은 `1.3.0`으로 유지한다.

## 검증 범위

- [x] 서로 다른 bare 숫자 2개 승격 / 단독 숫자 유지
- [x] 명시적 권 좌표와 managed work가 singleton 문맥을 제공
- [x] house singleton + temp singleton 교차 증명
- [x] 명시 작가 충돌, 100 이상, 범위, 날짜 꼬리 fail-closed
- [x] 외전이 본편 bare 권의 증거가 되지 않음
- [x] source-site 꼬리와 `소책자 한정판`의 동일 core/좌표 projection
- [x] TXT bare cohort의 journaled intake, `auto_ready` 분류, 시리즈 폴더 적용, final Doctor 0
- [x] Scanner 연속 2회 실행의 contextual rekey idempotence
- [x] warm contextual projection의 추가 stat/body read 0
- [x] 전체 Python 회귀: `833 passed in 19.40s`
- [x] frontend 1.4.11 production build
- [x] 밝은 사용자 테마(`#e0e0e0`/`#1b1a18`/`#149058`)에서 고정 다크 surface 제거 및 상태 대비 확인
- [x] 파일 검토 상대 제목 우선 배치와 작은 검토 링크 확인
- [x] 사용자 프리셋 추가·조회·중복 차단·삭제 집중 테스트 `4 passed` 및 settings production build
- [x] compileall / normalizer parity 35 cases / diff check

## 실제 inventory 읽기 전용 영향 확인

2026-08-02에 실제 house+temp의 지원 확장자 17,632개를 **파일명만** 읽어 순수 문맥 함수를 적용했다.
DB 갱신, 본문 읽기, 도서 이동은 수행하지 않았다.

- 문맥 승격 대상: 32작품, bare 파일 181개
- 전 작품명 표본 검토: 드래곤 라자, 피를 마시는 새, 재벌집 막내아들, 무극신갑 등 실제 분권 cohort
- 숫자가 제목 일부로 보이는 강한 오탐 후보: 0건
- temp 확인: `티어문 제국 이야기 10 소책자 한정판 ...epub`이 기존 명시 권수 문맥으로
  `티어문제국이야기` 10권 후보가 됨
- 단일 bare 파일이 승격된 나머지 사례도 같은 core의 명시적 권 좌표가 이미 있는 13권·23권 사례였음

## 운영 적용 범위

이번 개발·검증은 임시 합성 house/temp/state DB만 사용한다. 실제 `txt_house`/`txt_temp` 도서를 이동하거나
격리하지 않는다. 배포 후 최초 one-button 실행은 1.3.2 index projection을 새로 만들고, 승인된 actual run의
기존 volume-group journal 경로에서만 실제 loose 분권을 묶는다.
