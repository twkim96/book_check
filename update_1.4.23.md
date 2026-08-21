# file_check 1.4.23 Sheet integrated views and Chrome lookup metadata

기준:

- 1.4.22 운영 마감 커밋: `4d63342018394e15277594e5d6b55c865118c92f`
- 1.4.23 시작 기준 커밋: `c23b5f3b97a43b413b0d1fc21db2d3e93825594b`
  (`feat: flatten genres and harden NovelPia metadata`)
- SQLite schema: v16 유지

1.4.22는 Folderling self-lock 보강과 운영 read-only sanity까지 완료한 상태로 닫는다. 1.4.23은
Sheet의 작품 단위 통합 조회수와 Chrome 선택 제목 조회 정보 표시를 추가한다.

## Google Sheet 계약

`도서 목록`의 고정 열은 다음 A:E 다섯 열이다. 값이 전부 비어 있던 `장르 후보` 열은 제거한다.

1. A `원본 도서명`
2. B `작가`
3. C `장르`
4. D `보유 범위`
5. E `조회 수`

E열은 현재 `status=ok`인 플랫폼 수치만 대상으로 아래 값을 합산한다.

- 시리즈: `download_count`
- 카카오: `view_count`
- 노벨피아: `view_count`

일부 플랫폼만 수집된 경우에는 수집된 값만 합산한다. 세 플랫폼 모두 실제 수치가 없으면 `0`으로
오해하지 않도록 빈칸으로 둔다. 추천 수·좋아요 수·평점은 합산하지 않고 기존 플랫폼별 상세 열에만
유지한다.

통합 장르는 C열 `장르`로 표시한다. 시리즈·카카오·노벨피아 상세 묶음은 기존 순서와 값을 유지한다.
상단 두 행과 A:E를 고정하고, E열에도 기존 숫자 열과 같은 `#,##0` 형식을 적용한다.

## Chrome 선택 제목 조회 정보

앞선 미출시 변경을 1.4.23 범위에 포함한다.

- 일반 우클릭은 브라우저 기본 메뉴와 기존 확장 메뉴를 유지한다.
- `Command+Shift+L`은 선택 제목의 로컬 중복 검색을 유지한다.
- Command+우클릭 모달은 로컬 중복 검색과 함께 기존 정보 아이콘 경로의 조회수·추천 메타데이터를 표시한다.
- 확장 2.11부터 EnterJoy·Tcafe21·Pastebin과 함께 Chating Wiki(`chating.wiki` 및 하위 도메인)에서도
  콘텐츠 스크립트와 기존 우클릭 메뉴를 활성화한다.
- 확장 검색 모달은 최대 z-index로 해결할 수 없는 사이트 dialog stacking을 피하도록 네이티브
  `<dialog>.showModal()` top layer를 사용하고, 사용할 수 없는 환경에서는 기존 fixed 최대 z-index로 폴백한다.
- Chating Wiki 자료함의 `.group-material-copy > strong` 제목에도 기존 로컬 중복 돋보기와 웹 정보 아이콘을
  주입한다. 2초 주기 재스캔을 유지해 `자료 더 보기`로 동적 추가된 카드도 중복 없이 처리한다.
- 초기 사이트 변경으로 추가된 게시판 행의 `.cw-board-item__title > strong`도 제목으로 인식한다. 같은 행의
  조회수 `<em>`과 자료 태그는 selector 밖에 두어 검색 제목에 섞이지 않게 한다.
- 아이콘 호버 툴팁은 `popover=manual` 비차단 top layer로 표시한다. 위치는 viewport 기준 fixed로 계산하고,
  Popover API를 사용할 수 없으면 기존 최대 z-index fixed 툴팁으로 폴백한다.
- Chating Wiki 자료함 제목 요소에서만 URL-form 공백 구분자 `+`를 공백으로 바꾼 뒤 검색한다. 공용
  normalizer는 변경하지 않아 `C++`·`1+1` 같은 identity-bearing `+`를 계속 보존한다.
- 모달이 닫혔거나 다른 제목으로 바뀐 뒤 도착한 응답은 렌더링하지 않는다.

## 변경하지 않는 계약

- SQLite schema v16
- 플랫폼 remote identity 및 stored-ID provenance 규칙
- normalizer, fingerprint, pair, auditor, archive 버전
- Sheet 단방향·read-only SQLite projection과 임시 탭 교체 방식
- 기존 플랫폼별 원격 수치, 장르, 태그, 링크 열

## 검증

- 최종 A:E Sheet projection/format 집중 공개 회귀: **16 passed**
- Python 전체 회귀: **1024 passed**, urllib3/LibreSSL 환경 warning 1건
- server health 1.4.23 회귀: 전체 회귀에 포함해 PASS
- Chrome context-search: PASS (`sites=4`, `Command+Shift+L`)
- Chrome normalizer parity: PASS (`version=1.3.3`, `cases=37`)
- frontend 1.4.23 typecheck/build: PASS
- compileall: PASS
- 변경 Python 파일 pyflakes: PASS
- `git diff --check`: PASS
