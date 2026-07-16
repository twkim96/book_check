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

## 플랫폼 카탈로그 (1.2.4)

Folderling과 별개로, 보유 작품의 플랫폼별 최신 인기·평점 지표를 상태 DB에 보관합니다.
기본 실행은 아직 값이 없는 플랫폼만 최대 25개 제목씩, 제목 batch 사이 3초 간격으로
조회합니다. 파일 이동·삭제와 file_index.json 갱신은 하지 않습니다.

~~~bash
# terminal control server의 새 버튼 대상
python3 run_platform_catalog.py refresh

# 네트워크/DB 변경 없이 다음 대상만 확인
python3 run_platform_catalog.py refresh --dry-run

# 수집 현황 및 지표별 상위 작품
python3 run_platform_catalog.py status
python3 run_platform_catalog.py top --order-by series-interest --limit 20
~~~

첫 실제 실행에서 schema v7 DB는 v8로 전환되며, 전환 전 SQLite backup을
.dedup_state/backups/에 남깁니다. catalog_title_metrics view에는 시리즈 관심·평점,
카카오 조회·평점, 노벨피아 조회·추천의 여섯 컬럼이 있습니다.

## 테스트

```bash
python3 -m pytest -q public_tests
```

공개 테스트는 임시 디렉터리의 합성 파일만 사용하며 실제 라이브러리를 변경하지 않습니다.
개인 운영에서 축적된 전체 회귀 fixture는 실제 제목 형태가 포함될 수 있어 공개 저장소에서
제외합니다.
