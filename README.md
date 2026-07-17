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

# 네트워크/DB 변경 없이 다음 대상만 확인
python3 run_platform_catalog.py refresh --dry-run

# 수집 현황 및 지표별 상위 작품
python3 run_platform_catalog.py status
python3 run_platform_catalog.py top --order-by series-download --limit 20

# 제목 비교 로직 변경 후 기존 not_found/error만 플랫폼별 한 번 재검사
python3 run_platform_catalog.py retry-failed-once --dry-run
python3 run_platform_catalog.py retry-failed-once
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
채택합니다. `retry-failed-once`는 시작 시 실패 행의 cutoff를 저장하므로 중간 취소 후 다시
실행해도 이미 재검사한 플랫폼 행은 건너뛰며, 완료 후에는 다시 실행되지 않습니다.
Kakao는 최신 BFF search/overview JSON API를 사용하며 일시 오류는 재시도 가능한
`error`로 남기며, Kakao 검색은 동명 웹툰을 피하도록 웹소설 분류로 제한합니다.
`catalog_title_metrics` view에는 시리즈 다운로드·평점, 카카오 조회·평점,
노벨피아 조회·추천의 여섯 컬럼이 있습니다.

응답 구조가 예상과 다르면 `not_found`가 아니라 재시도 가능한 `error`로 기록합니다.
정상적인 `not_found`도 30일 뒤 자동 재조회합니다. 마지막 성공 지표는 DB에 보존하지만
`top` 명령은 현재 상태가 `ok`인 지표만 상위 목록에 표시합니다.

SQLite를 사람이 편하게 확인하기 위한 Google Sheet는 완전한 단방향 미러입니다. Sheet에서
수정한 값은 DB로 가져오지 않으며, 동기화 중 SQLite는 `mode=ro`와 `query_only`로만 엽니다.

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
전체 값을 먼저 쓴 뒤 성공한 경우에만 `작품 현황`, `수집 오류` 탭을 교체합니다.

## 테스트

```bash
python3 -m pytest -q public_tests
```

공개 테스트는 임시 디렉터리의 합성 파일만 사용하며 실제 라이브러리를 변경하지 않습니다.
개인 운영에서 축적된 전체 회귀 fixture는 실제 제목 형태가 포함될 수 있어 공개 저장소에서
제외합니다.
