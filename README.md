# file_check

`file_check`는 개인 txt/epub 라이브러리를 스캔하고, 제목·편수·본문 근거로 중복 후보를
검토 큐에 모으며, 승인된 변경만 journal 기반으로 적용하는 Python 도구입니다.

## 안전 원칙

- 기본 중복 검사는 dry-run입니다.
- 애매한 항목은 자동 삭제하지 않고 review queue로 보냅니다.
- 실제 이동은 상태 DB, backup, manifest, 일회성 승인, 복구 기록을 사용합니다.
- `.dedup_state`, 인덱스, 로그와 실제 라이브러리는 Git에 포함하지 않습니다.

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
Spreadsheet ID는 Git에 넣지 말고 컨트롤서버의 로컬 환경변수에만 둡니다. 동기화는 임시 탭에
전체 값을 먼저 쓴 뒤 성공한 경우에만 `도서 목록`, `수집 오류` 탭을 교체합니다. 이전 버전의
`작품 현황` 탭이 있으면 성공적인 첫 동기화 때 정리합니다.

## 테스트

```bash
python3 -m pytest -q public_tests
```

공개 테스트는 임시 디렉터리의 합성 파일만 사용하며 실제 라이브러리를 변경하지 않습니다.
개인 운영에서 축적된 전체 회귀 fixture는 실제 제목 형태가 포함될 수 있어 공개 저장소에서
제외합니다.
