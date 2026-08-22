# file_check 1.4.24 platform cover URL metadata

기준:

- 1.4.23 커밋: `bc4ba45ce6a5ed515661212ed0bb3cfbe5ff32ca`
- SQLite schema: v16 → v17

## 목표

플랫폼별 원본 메타데이터 행인 `catalog_platform_stats`에 nullable `cover_url`을 추가한다. Series,
Kakao, NovelPia 수집기가 기존 작품 상세/overview 응답에서 대표 표지의 `https://` 직접 URL을 함께
수집하며, 플랫폼별 URL은 서로 합치지 않는다.

## 수집 계약

- Series: 기존 상세 HTML의 `og:image`
- Kakao: 기존 overview JSON `result.content.thumbnail` 이미지 키로 만든 카카오 CDN `o1` URL
- NovelPia: 상세 HTML `og:image` 우선, 검색 응답의 명시적 cover/thumbnail 필드는 상세 실패 시 보조
- `https://`와 host가 있는 직접 URL만 DB writer가 허용
- 표지를 확실히 읽지 못하면 `NULL`
- 이미지 다운로드 및 이미지 바이너리 저장 없음

## 갱신·보존 계약

- 최초 플랫폼 성공 row insert 시 `cover_url` 저장
- 같은 remote ID의 일반 갱신, 증가형 기존 지표 갱신, 장르·태그 메타데이터 갱신에서
  `cover_url`도 최신 응답값으로 교체
- `refresh-metadata`는 `status='ok' AND cover_url IS NULL`도 대상으로 포함하고 Series/Kakao/NovelPia
  모두 저장된 `remote_id` 상세에서 표지/장르/태그만 갱신한다. popularity 수치는 건드리지 않는다.
- 성공한 동일 identity 응답에서 표지가 사라지면 추측값을 보존하지 않고 `NULL`로 갱신
- transport/parser/identity 실패는 기존 성공 row와 표지 URL을 보존
- normalizer rekey가 성공 플랫폼 row를 옮길 때 `cover_url`도 함께 보존

## migration

- schema v17은 `catalog_platform_stats.cover_url TEXT`를 추가하고 SQLite CHECK로 `NULL` 또는
  `https://%`만 허용한다.
- 기존 v16 DB는 `run_platform_catalog.py`의 backup-owning schema migration을 통해서만 올린다.
- v17 validator는 `cover_url` 존재를 필수로 확인한다.

## 검증

- Series/Kakao/NovelPia 응답 fixture와 상세 우선순위
- HTTP/상대/잘못된 URL 차단 및 HTTPS 정규화
- v16 → v17 migration
- 신규/기존/증가형/메타데이터 writer의 cover 교체 및 NULL 갱신
- 실제 세 플랫폼 parser live response와 실제 DB sample row

## 완료 검증

- Python 전체 회귀: **1032 passed**, urllib3/LibreSSL 환경 warning 1건
- frontend 1.4.24 typecheck/build: PASS
- 변경 backend/new fixture pyflakes, compileall, `git diff --check`: PASS
- 실제 parser live response: Series/Kakao/NovelPia 모두 `status=ok`, `https://` cover 반환
- Kakao CDN `o1` URL: HTTP 200 `image/jpeg`
- 운영 DB migration: v16 backup integrity `ok` → 현재 v17 integrity `ok`, FK issue 0
- migration backup:
  `.dedup_state/backups/before_platform_catalog_schema_20260821_204301_971923_9f827f93.sqlite3`

## 실제 DB sample row

```text
매화검수 | series | 13180203 | https://comicthumb-phinf.pstatic.net/20250919_71/pocket_1758247518891yiWYt_JPEG/%BA%CF%B9%CC%C8%A5_%B8%C5%C8%AD%B0%CB%BC%F6.jpg?type=m600x314
레이센 | kakao | 46964089 | https://dn-img-page.kakao.com/download/resource?kid=R2mke%2FdJMcaihHy4y%2FVII8HimtGfhuMdCPlZX1hk&filename=o1
던전에서살아남는방법 | novelpia | 286594 | https://novelpia.com/imagebox/cover/c7bc315cd8679475ffb450bbe241e1ce_54021_ori.file
```

세 URL 모두 실제 요청 200을 확인했다. 현재 `cover_url IS NOT NULL AND cover_url NOT LIKE
'https://%'` row는 0건이다.

## 전체 backfill follow-up

- `file-metadata-sync`: 전체 18,019건 중 358건 변경, 17,661건 유지
- 후속 dry-run에서 `stale=0`, `missing_files=0`, `index_missing_db=0`으로 플랫폼 갱신 차단 상태 수렴
- 남은 `unindexed_active=4`는 지원 대상 file index에 없는 과거 active row(EPUB 내부 `cover.jpg` 3건,
  `.hwp` 1건)라 정상 동기화 범위 밖이며 플랫폼 갱신을 차단하지 않음
- PM2의 기존 NovelPia 자격 증명을 사용하는 Control Server `platform-metadata` 전체 작업으로 실행
  - job: `0ca138d1-c2d0-4157-8118-8597e1ce4daf`
  - 2026-08-21 21:16:45 ~ 2026-08-22 02:55:54 KST, 12,102개 작품 / 21,287개 플랫폼 행
  - `updated=21,260`, `identity_conflict=20`, `unavailable=7`, `error=0`
  - 저장된 remote ID 직접 조회 21,257건, NovelPia 인증 조회 3건, 검색 fallback 0건
  - 작업 자체의 file metadata sync는 18,019건 중 356건 변경, 17,663건 유지
- 27건을 추측값으로 교체하지 않고 보존했으므로 최종 job 상태는 `needs_review`

### 운영 DB 최종 cover 집계

| platform | `status='ok'` | cover 있음 | cover NULL |
| --- | ---: | ---: | ---: |
| Kakao | 9,806 | 9,777 | 29 |
| NovelPia | 1,204 | 1,203 | 1 |
| Series | 10,305 | 10,283 | 22 |
| 합계 | 21,315 | 21,263 | 52 |

- 활성 카탈로그의 cover NULL은 아래 미해결 27건이다.
- 나머지 cover NULL 25건(Kakao 13, NovelPia 1, Series 11)은 활성 house 파일이 없는 보존용 과거
  플랫폼 행이라 활성 카탈로그 backfill 대상이 아니다.
- 완료 후 `refresh-metadata --dry-run --all`은 25개 작품 / 29개 플랫폼 행을 반환한다. 이 중
  27행은 cover 미해결분이며, 나머지 NovelPia 2행은 cover가 있고 장르/태그 snapshot만 누락됐다.

### 미해결 원인

- Kakao 15건: 저장된 remote ID 상세의 작품명 불일치
- Series 5건: 저장된 remote ID 상세의 작품명 불일치
- Kakao 1건, Series 1건: DNS 이름 해석 실패
- Series 5건: 저장된 상품 상세가 현재 이용 불가

identity 불일치나 상세 이용 불가를 검색 결과로 추측해 교체하지 않았고, 기존 성공 row와 인기 지표를
보존했다.

### 완료 후 운영 검증

- SQLite `PRAGMA user_version=17`, schema validator PASS
- `PRAGMA integrity_check=ok`, foreign key issue 0
- `cover_url IS NOT NULL AND cover_url NOT LIKE 'https://%'` 0건
- `file-metadata-sync --dry-run`: current 18,019, stale 0, missing 0, index missing 0
- Control Server `platform-metadata`: configured/ready, active job 없음
- PM2 `server-control--book_check`: online
- `/health`: version 1.4.24, database ok
