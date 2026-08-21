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

- `file-metadata-sync`로 stale/unindexed active file을 먼저 정상 수렴
- PM2의 기존 NovelPia 자격 증명을 사용하는 Control Server `platform-metadata` 전체 작업으로 실행
- 완료 후 플랫폼별 `ok / cover 있음 / cover NULL`과 `failure_reasons`를 기록
