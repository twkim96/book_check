"""앞마커(〔P〕…/〔Dn〕…) 파일명을 접미사 마커(…〔P〕.ext/…〔Dn〕.ext)로 일괄 이전.

1.1.1에서 마커 위치를 제목 앞 → 확장자 앞으로 바꾸면서, 기존에 앞마커로 입고된
파일명을 새 규칙으로 맞춘다. 자모 폴더 위치는 그대로 둔다(파일명만 변경).

사용:
    python3 migrate_marker_position.py            # dry-run(미리보기)
    python3 migrate_marker_position.py --run      # 실제 이름 변경
    python3 migrate_marker_position.py --house <경로>

`_최근` 심볼릭 링크도 새 대상으로 다시 건다. 인덱스 재생성은 scanner.py로 별도 수행.
"""
import os
import re
import sys

from normalizer import (
    PASS_MARKER,
    add_disambig_marker,
    add_pass_marker,
    normalize_nfc,
)
from project_paths import HOUSE_DIR

DEFAULT_HOUSE_DIR = str(HOUSE_DIR)
RECENT_DIR_NAME = "_최근"

# 앞마커 추출: 맨 앞의 〔P〕와 〔Dn〕(순서 무관, 둘 다 가능)
_PREFIX_RE = re.compile(r"^\s*(?:(〔P〕)|(〔D(\d+)〕))+")
_PREFIX_TOKEN_RE = re.compile(r"〔P〕|〔D(\d+)〕")


def _convert_name(name):
    """앞마커가 있으면 접미사 마커로 변환한 새 이름을 반환. 없으면 None."""
    norm = normalize_nfc(name)
    m = _PREFIX_RE.match(norm)
    if not m:
        return None
    prefix = m.group(0)
    rest = norm[m.end():].lstrip()
    has_pass = PASS_MARKER in prefix
    dis = None
    for tok in _PREFIX_TOKEN_RE.finditer(prefix):
        if tok.group(1):
            dis = int(tok.group(1))
    new = rest
    if has_pass:
        new = add_pass_marker(new)
    if dis and dis > 1:
        new = add_disambig_marker(new, dis)
    return new


def parse_args(argv):
    args = {"house_dir": DEFAULT_HOUSE_DIR, "dry_run": True}
    for i, a in enumerate(argv):
        if a == "--run":
            args["dry_run"] = False
        elif a == "--house" and i + 1 < len(argv):
            args["house_dir"] = argv[i + 1]
    return args


def _relink_recent(recent_dir, old_name, new_name, new_target, dry_run):
    """_최근 폴더에서 old_name 링크를 new_name → new_target으로 재연결."""
    if not os.path.isdir(recent_dir):
        return
    old_link = os.path.join(recent_dir, old_name)
    new_link = os.path.join(recent_dir, new_name)
    if not (os.path.islink(old_link) or os.path.exists(old_link)):
        return
    if dry_run:
        print(f"    [링크] {old_name} → {new_name}")
        return
    try:
        os.remove(old_link)
    except OSError:
        pass
    try:
        if os.path.islink(new_link) or os.path.exists(new_link):
            os.remove(new_link)
        os.symlink(new_target, new_link)
    except OSError as e:
        print(f"    ⚠️ 링크 재연결 실패 ({new_name}): {e}")


def migrate(house_dir, dry_run):
    recent_dir = os.path.join(house_dir, RECENT_DIR_NAME)
    converted = 0
    skipped = 0
    for root, dirs, files in os.walk(house_dir):
        # _최근은 링크 폴더라 본문 변경 대상에서 제외
        if os.path.basename(root) == RECENT_DIR_NAME:
            continue
        for fn in files:
            new_name = _convert_name(fn)
            if not new_name or new_name == fn:
                continue
            src = os.path.join(root, fn)
            dst = os.path.join(root, new_name)
            if os.path.exists(dst):
                print(f"⚠️ 충돌로 건너뜀: {os.path.relpath(dst, house_dir)}")
                skipped += 1
                continue
            print(f"{'[미리보기] ' if dry_run else ''}{os.path.relpath(src, house_dir)}  →  {new_name}")
            if not dry_run:
                os.rename(src, dst)
            _relink_recent(recent_dir, fn, new_name, os.path.abspath(dst), dry_run)
            converted += 1
    label = "미리보기" if dry_run else "실행"
    print(f"\n{label} 완료: 변환 {converted}개, 건너뜀 {skipped}개")
    if dry_run:
        print("실제 적용: python3 migrate_marker_position.py --run  (이후 python3 scanner.py)")
    return converted


if __name__ == "__main__":
    opts = parse_args(sys.argv[1:])
    migrate(opts["house_dir"], opts["dry_run"])
