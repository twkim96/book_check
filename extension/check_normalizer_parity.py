#!/usr/bin/env python3
"""Python/Chrome core_title parity gate for the tracked extension source."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from normalizer import NORMALIZER_VERSION, extract_core_title  # noqa: E402


CASES = [
    "완벽한세계 1-200 완.txt",
    "19호실의 비밀 1-50화 완결.txt",
    "인생 2회차 아이돌의 럭키플랜 1-1099 완.txt",
    "죽는 연기의 제왕 1－243 완 ［현판, 고별］.txt",
    "톱스타 그 자체 외전 1-71 完 [서홍].txt",
    "Dr. 신선한 미래를 보는 의사 1-2부 483 외포 完",
    "헌터세계 재벌가주 1 325화 完.epub",
    "팽가신화@김도훈(북큐브) -379(완).txt",
    "東廠 完.txt",
    "검황 이계정벌하다 1 10 [완] - 한가.txt",
    "로판 l 시한부 악녀가 복수하는 방법.txt",
    "멸망한 세계의 전승자 총206화 완.epub",
    "무인도 표류일지 R 307(완).txt",
    "밀푸색마 1 100 완.txt",
    "서랍 속 청개구리完⓳ [디키탈리스].epub",
    "시장통 맛침명의 재벌처가 씹어먹다 256＋24 完.txt",
    "가끔씩 툭하고 러시아어로 부끄러워하는 옆자리의 아랴 양 04.5권.epub",
    "[[19금]] 떡타지의 주인공 친구가 되었다 0-631 완 [스투피르].txt",
    "구조 작품 {{R 307}}.epub",
    "[갱신 19禁완) 마조 수녀와 음마 신부 0-134 완 [ txt + epub ].txt",
    "신규 19禁완) 사랑을 먹고 자라는 마법소녀 0-83 완 [ txt + epub ]",
    "신작) 일러스트로 일인군단",
    "[재업로드] 마조 수녀와 음마 신부 0-134 완.txt",
    "나는 매달 치트키를 갱신할 수 있다 1-100 완.txt",
    "Lv2부터 치트였던 전직 용사 후보의 유유자적 이세계 라이프 01권.epub",
    "재벌은 1968부터 1-250 완.txt",
    "k200 장갑차.txt",
    "24／7 1권 [이내리].txt",
    "글밈 -2회차-드래곤은-유희를-즐긴다-1-201-완.txt",
    "어게인1997@삽자루(19N) -134(완).txt",
    "좀비묵시록 82-08 001-449 完.txt",
    "꾸롶 [밤오렌지] 백작과 하녀 2권완.epub",
    "CSS [백덕수] 데뷔 못 하면 죽는 병 걸림 1-644 完.epub",
    "판 [백덕수] 괴담에 떨어져도 출근을 해야 하는구나 1-371 1,2부 완결.epub",
    "CSS 완벽 가이드 1권.epub",
    "[19금] 튜토리얼로 회귀한 이유는 무엇일까(19N) 1-416 완",
    "[19금] 야설(근친) 작가로 살아남기 1-100 완.txt",
]

READABLE_EXPECTATIONS = {
    "[19금] 튜토리얼로 회귀한 이유는 무엇일까(19N) 1-416 완": (
        "튜토리얼로 회귀한 이유는 무엇일까"
    ),
    "[19금] 야설(근친) 작가로 살아남기 1-100 완.txt": (
        "야설(근친) 작가로 살아남기"
    ),
}


script = """
import { NORMALIZER_VERSION, extractCoreTitle, extractReadableTitle } from './extension/normalizer.js';
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const names = JSON.parse(input);
  console.log(JSON.stringify({
    version: NORMALIZER_VERSION,
    cores: names.map(name => extractCoreTitle(name)),
    readables: names.map(name => extractReadableTitle(name)),
  }));
});
"""
result = subprocess.run(
    ["node", "--input-type=module", "-e", script],
    cwd=ROOT,
    input=json.dumps(CASES, ensure_ascii=False),
    text=True,
    capture_output=True,
    check=False,
)
if result.returncode:
    raise SystemExit(result.stderr or f"node failed: {result.returncode}")
payload = json.loads(result.stdout)
if payload["version"] != NORMALIZER_VERSION:
    raise SystemExit(
        f"normalizer version mismatch: python={NORMALIZER_VERSION} js={payload['version']}"
    )
mismatches = [
    (name, extract_core_title(name), js_core)
    for name, js_core in zip(CASES, payload["cores"])
    if extract_core_title(name) != js_core
]
if mismatches:
    for name, python_core, js_core in mismatches:
        print(f"MISMATCH {name!r}\n  python={python_core!r}\n  chrome={js_core!r}")
    raise SystemExit(1)
readable_mismatches = [
    (name, READABLE_EXPECTATIONS[name], js_readable)
    for name, js_readable in zip(CASES, payload["readables"])
    if name in READABLE_EXPECTATIONS and READABLE_EXPECTATIONS[name] != js_readable
]
if readable_mismatches:
    for name, expected, actual in readable_mismatches:
        print(f"READABLE MISMATCH {name!r}\n  expected={expected!r}\n  chrome={actual!r}")
    raise SystemExit(1)
print(f"normalizer parity ok: version={NORMALIZER_VERSION} cases={len(CASES)}")
