# file_check 1.4.16 — Chrome 다중 사이트 제목 검색과 선택 텍스트 단축키

## 목표

1. EnterJoy, Tcafe21, Pastebin에서 같은 로컬 중복 검색 결과를 사용한다.
2. 일반 우클릭 메뉴를 보존하면서 `이 제목으로 중복 확인` 항목을 제공한다.
3. 제목 또는 Pastebin 한 줄의 `Command+우클릭` 즉시 검색을 유지한다.
4. 선택한 텍스트를 `Command+Shift+L`로 즉시 검색한다.
5. 개인 라이브러리 인덱스는 공개하지 않고 확장 소스와 회귀 검증만 저장소에서 재현 가능하게 만든다.

## 구현 범위

### 사이트별 검색 대상

- EnterJoy 상세 제목: `#at-main > div.view-wrap > section > article > h1`
- Tcafe21 상세 제목: `.at-content > .view-wrap > h1`
- Pastebin 개별 줄: `.highlighted-code .source li > div`

Pastebin의 `.source` 전체를 검색어로 사용하지 않는다. 선택 텍스트가 있으면 선택 범위를 우선하고,
선택이 없으면 실제로 우클릭한 한 줄만 사용한다.

### 입력 경로

1. 일반 우클릭
   - 브라우저 기본 메뉴를 막지 않는다.
   - `이 제목으로 중복 확인`을 클릭한 뒤 기존 검색 모달을 표시한다.
2. `Command+우클릭`
   - 검색 가능한 제목·줄·선택 텍스트가 있을 때만 기본 메뉴를 가로채고 즉시 모달을 표시한다.
   - 검색어가 없으면 기본 우클릭을 유지한다.
3. `Command+Shift+L`
   - Chrome `commands` API의 browser-scoped 명령이다.
   - 현재 페이지에 실제 선택 텍스트가 있을 때만 기존 모달을 표시한다.
   - 지원 사이트 밖이거나 선택이 없으면 조용히 종료한다.

### 단축키 선정 근거

macOS, Chrome, Safari, Firefox, Edge의 공식 기본 단축키 목록을 비교했다.

- `Command+Shift+X`: Firefox의 주소 입력 방향 전환과 충돌하여 제외
- `Command+Shift+K`: macOS Finder의 네트워크 창과 충돌하여 제외
- `Command+Shift+L`: 확인한 macOS·브라우저 기본 목록에서 충돌이 발견되지 않아 채택

확장 명령은 global로 등록하지 않아 브라우저가 포커스일 때만 활성화된다. 설치된 다른 확장과 충돌해
Chrome이 기본 키를 할당하지 못하면 `chrome://extensions/shortcuts`에서 사용자가 바꿀 수 있다.

확인 기준:

- [Apple macOS shortcuts](https://support.apple.com/en-us/102650)
- [Apple Safari shortcuts](https://support.apple.com/guide/safari/cpsh003/mac)
- [Google Chrome shortcuts](https://support.google.com/chrome/answer/157179)
- [Mozilla Firefox shortcuts](https://support.mozilla.org/en-US/kb/keyboard-shortcuts-perform-firefox-tasks-quickly)
- [Microsoft Edge shortcuts](https://support.microsoft.com/en-us/edge/keyboard-shortcuts-in-microsoft-edge)
- [Chrome extensions commands API](https://developer.chrome.com/docs/extensions/reference/api/commands)

## 공개 저장소 경계

기존 `.gitignore`의 `extension/` 전체 제외를 좁혀 다음 로컬 생성물만 계속 제외한다.

- `extension/file_index.json`: 개인 라이브러리 파일명·경로 인덱스
- `extension/_metadata/`: Chromium이 생성한 로컬 ruleset 메타데이터
- `extension/__pycache__/`: Python 검증기 캐시

확장 실행 소스, manifest, DNR rule, 팝업, 스타일, normalizer와 회귀 검증기는 추적한다. 변경 파일에서
API key, token, password, credential 값은 발견되지 않았다.

## 버전 범위

- 관리 서버/UI/auditor/house cleanup report: `1.4.16`
- Chrome extension manifest: `2.10`
- DB schema: `v15` 유지
- Python/Chrome `NORMALIZER_VERSION`: `1.3.3` 유지
- fingerprint version/policy: `5` / `1.4.2` 유지
- pair policy: `1.4.12` 유지
- archive object/version: `1.4.10` 유지

schema migration, filename projection 재기준, fingerprint/pair cache 무효화, house 전체 스캔은 요구하지
않는다. 실제 도서 DB, index, house/temp 파일 이동·격리·삭제도 수행하지 않는다.

## 검증 결과

2026-08-09 현재 아래 게이트를 완료했다.

- [x] `node --check`로 `content.js`, `background.js` 구문 검사
- [x] `check_context_search.mjs`
  - 세 사이트 context selector
  - 일반 우클릭 비차단과 `Command+우클릭` 가로채기
  - `Command+Shift+L` command → 선택 텍스트 → content modal 전달
  - manifest `2.10`과 세 사이트 주입 범위
- [x] 실제 페이지 DOM 확인
  - EnterJoy 상세 제목 1개
  - Tcafe21 상세 제목 1개
  - Pastebin 개별 줄 191개, 첫 줄 단독 추출
- [x] Python/Chrome normalizer parity: `1.3.3`, 37건
- [x] 공개+운영 Python 전체 회귀: `871 passed`
- [x] 공개 회귀/coverage: `468 passed`, backend `72%` (`fail-under=70` 통과)
- [x] production Python pyflakes 0건, backend/tools compileall 통과
- [x] frontend TypeScript typecheck와 production build 통과
- [x] manifest JSON, CI YAML, `git diff --check` 통과

검증은 브라우저 DOM read-only 확인과 합성 회귀만 사용했다. 실제 도서 DB, index, house/temp 파일 이동,
격리, 삭제, Folderling actual 실행은 수행하지 않았다.
