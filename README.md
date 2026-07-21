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

Folderling은 이미 정상적인 작품 폴더에 들어 있는 분권들과 제목·작가·권 좌표가 명확히 맞고
좌표가 겹치지 않는 신규 파일만 같은 폴더와 work에 자동 연결합니다. 기존의 흩어진 파일을
Folderling 실행 중 재배치하지 않으며, 애매한 후보는 `/review/volumes`에서 사람이 확인합니다.

완료·실패·서버 중단 작업은 `.dedup_state/library-server/`에 남습니다. 이 디렉터리와 실제
라이브러리 경로, 운영 DB, 인증 정보는 Git에 포함되지 않습니다.

`/settings`에서는 컨트롤서버와 같은 방식으로 배경·주요 글자·포인트 컬러를 직접 지정하거나
기본 프리셋을 선택할 수 있습니다. 저장된 세 색상에서 패널, 입력창, 테두리, 활성 메뉴 색상을
자동 계산해 전체 화면에 적용합니다. 설정은 브라우저 캐시와
`.dedup_state/library-server/appearance.json`에 함께 저장되며 Git에는 포함되지 않습니다.

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
