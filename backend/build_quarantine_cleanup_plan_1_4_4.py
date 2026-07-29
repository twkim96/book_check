#!/usr/bin/env python3
"""Build the immutable data plan from the completed 2026-07-29 audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import decision_store
from cleanup_quarantine_1_4_4 import _atomic_json
from mutation_io import inspect_regular_file
from project_paths import STATE_DB


PRESERVE = {
    518: ("same_work_distinct_variant", "longer 1-875 plus external chapters; replace 1-846"),
    532: ("distinct_work", "내 눈에 스카우트 vs 스카우트; body match 0.04%"),
    534: ("same_work_distinct_variant", "도시의 지배자 OV; word-shingle containment 74.6%"),
    544: ("same_work_distinct_variant", "M사 후기 only supplement not present in main text"),
    547: ("distinct_work", "사류라 스카우트 vs other 스카우트; body match 0.07%"),
    549: ("distinct_work", "시간의 지배자 vs 도시의 지배자; body match 0%"),
    555: ("same_work_distinct_variant", "월야환담 창월야 01-10 aggregate; containment 22.6%"),
    557: ("same_work_distinct_variant", "월야환담 창월야 상; containment 30.1%"),
    558: ("same_work_distinct_variant", "월야환담 창월야 하; containment 18.3%"),
    559: ("same_work_distinct_variant", "월야환담 채월야 01-07; containment 21.5%"),
    572: ("same_work_distinct_variant", "케스핀의 대군주 variant; containment 65.1%"),
    573: ("same_work_distinct_variant", "크루세이더 glossary and side-story supplement"),
    574: ("same_work_distinct_variant", "크루세이더 side story; main-body containment 0%"),
    589: ("same_work_distinct_variant", "SSS급 랭커 회귀하다 variant; containment 56.3%"),
    706: ("same_work_distinct_variant", "MLB 메이저리그 variant; containment 57.4%"),
    714: ("same_work_distinct_variant", "대항해시대VIII variant; containment 81.6%"),
    715: ("same_work_distinct_variant", "던전앤시티 variant; containment 61.2%"),
    717: ("same_work_distinct_variant", "캔슬러 variant; containment 59.5%"),
    721: ("same_work_distinct_variant", "머더러시티 variant; containment 55.9%"),
    736: ("same_work_distinct_variant", "영웅의 주인 variant; containment 61.3%"),
}


RESTORE_DESTINATIONS = {
    518: "숫자/1217 고려3군단 1-875화 完 외전 完-판250925.txt",
    532: "ㄴ/내 눈에 스카우트ⓒ라이즈리얼 1-241 完.txt",
    534: "ㄷ/도시의 지배자  ov(완).txt",
    544: "ㅂ/비트타는 수양대군 M사 후기.txt",
    547: "ㅅ/스카우트 1-177(完, 에필 포함)@사류라〔D2〕.txt",
    549: "ㅅ/시간의 지배자%40데자뷰%28훈%29%2819N%29 -151%28완%29.txt",
    555: "ㅇ/월야환담 2부 창월야 01-10 (완).txt",
    557: "ㅇ/월야환담 창월야 상.txt",
    558: "ㅇ/월야환담 창월야 하.txt",
    559: "ㅇ/월야환담 채월야 01권-07권(완).txt",
    572: "ㅋ/케스핀의 대군주@박제후(19N) -194(완).txt",
    573: "ㅋ/크루세이더 용어 사전&외전.txt",
    574: "ㅋ/크루세이더외전.txt",
    589: "영어/SSS급 랭커, 회귀하다 1-500 완[]갈드.txt",
    706: "영어/MLB 메이저리그@말리브의해적(19N) -306(완).txt",
    714: "ㄷ/대항해시대VIII@리그너스(19N) -511(완).txt",
    715: "ㄷ/던전앤시티%40채병일%2819N%29 -311%28완%29.txt",
    717: "ㄷ/뒤로 걷는자. 캔슬러(Canceler)@바라밀경(19N) -291(완).txt",
    721: "ㅁ/머더러시티@채병일(19N) -215(완).txt",
    736: "ㅇ/영웅의 주인%40무간진%2819N%29 -255%28완%29.txt",
}


# Old representatives that were superseded, plus action-inbox files that had
# no keep recorded by the original one-click discard operation.
KEEP_OVERRIDES = {
    520: "a140171a-0a71-4c10-b8ad-263800c14ac6",
    528: "630fed44-7f7c-4d3c-a83e-660f0b3258de",
    551: "5fe825c0-416e-4648-84b5-77520e151290",
    563: "ae61a6c0-fbc6-4595-81fa-554f900708ec",
    568: "f3643122-4d85-41e0-9d13-fad93f99b4ce",
    569: "b7107eb6-7ca8-454e-8d22-2fa61773e1b9",
    599: "c077013b-0f41-4ccf-94ef-253ad1ff802f",
    706: "5aed57d3-7f55-440f-bf3c-3225433f8fc8",
    707: "3b779dee-ff8c-4e07-8409-38b5329c8cfe",
    712: "fe9eb0aa-cf41-4d91-ab0e-3fa2382c51f5",
    713: "523a8113-f2c4-4d9c-ba50-c4bd19e46c74",
    714: "adc2d154-bb1d-40de-9045-a5a1e85f7850",
    715: "ab3816ed-7ccb-431f-a62d-29765155dbd0",
    717: "82c4737c-8860-41e6-b7be-11618149fa3d",
    718: "a8a5e583-cfe6-41bc-b5b6-af9e64f72ca2",
    719: "4ad116c1-6a26-4115-9d3c-d14b3dce298b",
    720: "1bd2fb5a-a179-4f2e-aa47-5f79f05609e5",
    721: "4332647c-a691-400a-a955-39e4bf5880f2",
    722: "76094f6f-2ffe-4eb4-ae2d-1d426da3e15d",
    723: "3aaf003b-f738-4908-a5e6-ea86fc2d722b",
    725: "7b81e9f0-9352-4b40-a89a-3239ba6fb999",
    727: "cd8ae856-18a2-43b7-b7ca-af2708726f55",
    730: "8717a91b-cbb8-435d-867a-a6bb8b0b3391",
    733: "a339a7aa-abef-4116-a3e7-fcbc521df623",
    734: "a3b89926-828c-4a24-92ec-649a57068a5c",
    736: "a15939ac-17fd-4d41-b3d8-12985310a43f",
    737: "f216c663-a116-4d9a-96a6-2c1c783ca79a",
    738: "1ebe2caf-0189-458c-b2c6-451d4590997d",
    739: "0d0c526a-a86f-418a-9751-7166f47cfbfb",
    740: "0d0c526a-a86f-418a-9751-7166f47cfbfb",
    741: "158928b0-c5ca-46f8-8349-0f13df8d3a58",
    742: "71d9e313-43af-446c-831e-a8f7234bd6d1",
    743: "138a2f22-4828-4a9a-9d1d-d7bb6ab1233e",
    744: "76890264-7415-4b90-b4cb-0c8e40182a07",
    745: "5816b478-7a92-488b-b275-a1ba3e24a583",
    746: "c40a0992-bd49-4332-8aca-2f70a71369e5",
    747: "3cf3b00e-8df9-43f1-b536-83afcbfc52b8",
    748: "0de4bb6e-c090-4a6c-8aa8-95ad7b2d3646",
    749: "5348a991-bd2d-4c88-9986-7a32d71afb8a",
    750: "83451dca-a78a-4a90-9551-57c6781ba0cc",
    751: "3779ee5d-e3b4-49a4-961a-82c9a7944cb5",
    752: "2aaedd0f-4c3c-42ee-8580-711ec384a4a6",
    753: "ff2b5eb9-986a-4068-a6d2-114380f736cf",
    754: "1ee25ccf-7ec3-40bc-ad62-b141e7c1a799",
    755: "40fcb831-961f-47fd-9d39-64da2d159ec5",
    756: "75c00de4-8a98-4c2d-a164-295565f0392f",
    757: "79a71dad-f8ed-4680-87b7-134dafc912b6",
    758: "138f862e-ab2a-4a59-b7c2-7cc5660d82da",
    759: "8a9d8d54-9e29-4380-8d25-088e36850e54",
    760: "508af701-0772-49d9-bd71-ac9e05c0a157",
    761: "62ec3120-b4de-4254-85cc-b7c7f7d7a8af",
    762: "b7107eb6-7ca8-454e-8d22-2fa61773e1b9",
    765: "3eb66b3a-7cf8-4e95-a82e-ee2d31c8b5d4",
}


QUEUE_UPGRADES = {
    "461872bd-4cea-42ed-b783-095d74aacd17": {
        "keep_file_id": "961f008a-18c8-4ef3-9075-d602da621381",
        "destination_rel": "ㅅ/[티그리드] 스페셜 메이지(完).txt",
        "basis": "queue contains 97.27% of old keep and is longer",
    },
    "4a82f6c2-99a5-43ca-82f2-a002b07363ff": {
        "keep_file_id": "59788bfb-1d29-467f-89c8-bb1c62191567",
        "destination_rel": "ㅂ/백작가 도련님이 미쳐날뜀 [ⓒ더블킥] 1-225 (완).txt",
        "basis": "old keep is 99.86% contained in the slightly longer queue copy",
    },
}


QUEUE_BASIS = {
    "사일록신전": "ordered body 99.25%",
    "신화의 땅": "ordered body 99.29%",
    "흑영기병대": "ordered body 99.36%",
    "환관의 요리사": "word-shingle/ordered audit 94.3%, same title and span",
    "회귀로 재벌 참교육": "ordered audit 91%, current keep is longer",
    "약사의 혼잣말 15권": "same reading text; house copy retains larger images",
    "위대한 가문의 검술천재": "1-385 is contained by current same-author 1-407 keep",
}


MISSING_QUARANTINE = {
    570: "quarantine bytes were already absent before the 1.4.4 audit; active 1-178 house edition remains",
    594: "quarantine bytes were already absent before the 1.4.4 audit; DB acknowledgement only",
}


def build(state_db, untracked_path):
    conn = decision_store.connect_state_db_readonly(state_db)
    sha_cache = {}

    def sha(path):
        path = str(path)
        if path not in sha_cache:
            sha_cache[path] = inspect_regular_file(path).sha256
        return sha_cache[path]

    def file_row(file_id):
        row = conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown file id: {file_id}")
        return row

    try:
        user_rows = conn.execute(
            """
            SELECT o.*, f.canonical_path AS current_path, f.active, f.source
            FROM operations AS o JOIN files AS f ON f.file_id = o.file_id
            WHERE o.action = 'user_quarantine' AND o.state = 'committed'
              AND o.purged_at IS NULL AND f.active = 0
              AND f.source = 'quarantine'
            ORDER BY o.operation_id
            """
        ).fetchall()
        by_operation = {row["operation_id"]: row for row in user_rows}
        if not set(PRESERVE).issubset(by_operation):
            raise RuntimeError("preserve operation set changed")

        restore = []
        for operation_id in sorted(PRESERVE):
            row = by_operation[operation_id]
            keep_id = KEEP_OVERRIDES.get(operation_id) or row["keep_file_id"]
            keep = file_row(keep_id)
            if not keep["active"] or keep["source"] != "house":
                raise RuntimeError(f"restore reference is not active: {operation_id}")
            verdict, basis = PRESERVE[operation_id]
            restore.append({
                "operation_id": operation_id,
                "file_id": row["file_id"],
                "reference_file_id": keep_id,
                "verdict": verdict,
                "destination_rel": RESTORE_DESTINATIONS[operation_id],
                "expected_source_sha256": sha(row["current_path"]),
                "expected_reference_sha256": sha(keep["canonical_path"]),
                "note": f"1.4.4 full quarantine audit: {basis}",
                "basis": basis,
            })

        safe_user = [
            row for row in user_rows
            if row["operation_id"] not in PRESERVE
            and row["operation_id"] not in MISSING_QUARANTINE
        ]
        revalidation = []
        for row in safe_user:
            keep_id = KEEP_OVERRIDES.get(row["operation_id"]) or row["keep_file_id"]
            if not keep_id:
                raise RuntimeError(
                    f"safe discard has no current keep: {row['operation_id']}"
                )
            keep = file_row(keep_id)
            if not keep["active"] or keep["source"] != "house":
                raise RuntimeError(
                    f"safe discard keep is stale: {row['operation_id']}"
                )
            revalidation.append({
                "operation_id": row["operation_id"],
                "keep_file_id": keep_id,
                "expected_source_sha256": sha(row["current_path"]),
                "expected_keep_sha256": sha(keep["canonical_path"]),
                "basis": "full audit: normalized/ordered/word-shingle containment at least 90%, exact reading payload, or explicit prior strong decision",
            })

        exact_rows = conn.execute(
            """
            SELECT o.*, f.canonical_path AS current_path
            FROM operations AS o JOIN files AS f ON f.file_id = o.file_id
            WHERE o.action = 'exact_quarantine' AND o.state = 'committed'
              AND o.purged_at IS NULL AND f.active = 0
              AND f.source = 'quarantine'
            ORDER BY o.operation_id
            """
        ).fetchall()
        exceptional_exact = next(
            row for row in exact_rows if row["operation_id"] == 3118
        )
        exceptional_keep = file_row("f4731e6e-4d08-4f8a-88e8-f0fa6b10dd37")
        revalidation.append({
            "operation_id": 3118,
            "keep_file_id": exceptional_keep["file_id"],
            "expected_source_sha256": sha(exceptional_exact["current_path"]),
            "expected_keep_sha256": sha(exceptional_keep["canonical_path"]),
            "basis": "original exact keep was later quarantined; current active edition has 99.07% ordered-body agreement",
        })

        queue_rows = conn.execute(
            """
            SELECT * FROM files
            WHERE active = 1 AND source = 'queue'
              AND canonical_path NOT LIKE '%/volume_conflicts/%'
              AND canonical_path NOT LIKE '%/.DS_Store'
            ORDER BY canonical_path
            """
        ).fetchall()
        queue_discard = []
        queue_upgrade = []
        for queue in queue_rows:
            upgrade = QUEUE_UPGRADES.get(queue["file_id"])
            if upgrade:
                keep = file_row(upgrade["keep_file_id"])
                queue_upgrade.append({
                    "file_id": queue["file_id"],
                    "keep_file_id": keep["file_id"],
                    "destination_rel": upgrade["destination_rel"],
                    "expected_source_sha256": sha(queue["canonical_path"]),
                    "expected_keep_sha256": sha(keep["canonical_path"]),
                    "basis": upgrade["basis"],
                })
                continue
            review = conn.execute(
                """
                SELECT ri.review_id, ri.classification,
                       other.file_id AS keep_file_id,
                       other.canonical_path AS keep_path
                FROM review_items AS ri
                JOIN files AS other
                  ON other.file_id = CASE
                    WHEN ri.candidate_file_id = ? THEN ri.reference_file_id
                    ELSE ri.candidate_file_id END
                WHERE (ri.candidate_file_id = ? OR ri.reference_file_id = ?)
                  AND ri.state IN ('pending', 'deferred')
                  AND other.active = 1 AND other.source = 'house'
                ORDER BY CASE ri.classification
                  WHEN 'text_equivalent' THEN 0
                  WHEN 'epub_equivalent' THEN 0
                  WHEN 'contained_exact' THEN 1
                  WHEN 'contained_version' THEN 1
                  WHEN 'longer_unresolved' THEN 2
                  WHEN 'decode_lossy' THEN 3
                  WHEN 'metadata_only' THEN 4 ELSE 5 END,
                  ri.review_id DESC
                LIMIT 1
                """,
                (queue["file_id"], queue["file_id"], queue["file_id"]),
            ).fetchone()
            if review is None:
                raise RuntimeError(
                    f"tracked queue item has no active house relation: {queue['canonical_path']}"
                )
            name = Path(queue["canonical_path"]).name
            basis = next(
                (value for key, value in QUEUE_BASIS.items() if key in name),
                f"current persisted review {review['review_id']} {review['classification']}",
            )
            queue_discard.append({
                "file_id": queue["file_id"],
                "keep_file_id": review["keep_file_id"],
                "expected_source_sha256": sha(queue["canonical_path"]),
                "expected_keep_sha256": sha(review["keep_path"]),
                "review_id": review["review_id"],
                "classification": review["classification"],
                "basis": basis,
            })

        untracked_path = Path(untracked_path).resolve()
        untracked_keep = file_row("38447b13-e104-4dc1-87a6-b4abd53eaaf2")
        untracked = [{
            "path": str(untracked_path),
            "keep_file_id": untracked_keep["file_id"],
            "expected_source_sha256": sha(untracked_path),
            "expected_keep_sha256": sha(untracked_keep["canonical_path"]),
            "basis": "same title/author; 2073 vs 2076 episodes; word-shingle containment 93.90%/94.61%",
        }]
        trash_root = Path(untracked_path).resolve().parents[1]
        metadata_cleanup = [
            {"path": str(path), "expected_sha256": sha(path)}
            for path in sorted(trash_root.rglob(".DS_Store"))
            if path.is_file() and not path.is_symlink()
        ]

        purge_ids = sorted(
            [row["operation_id"] for row in exact_rows]
            + [row["operation_id"] for row in safe_user]
        )
        restored_upgrade = by_operation[518]
        missing_ack = []
        for operation_id, basis in sorted(MISSING_QUARANTINE.items()):
            row = by_operation[operation_id]
            if Path(row["current_path"]).exists():
                raise RuntimeError(
                    f"missing quarantine unexpectedly reappeared: {operation_id}"
                )
            if not row["destination_sha256"]:
                raise RuntimeError(
                    f"missing quarantine lacks historical ownership: {operation_id}"
                )
            missing_ack.append({
                "operation_id": operation_id,
                "file_id": row["file_id"],
                "expected_missing_path": row["current_path"],
                "historical_destination_sha256": row["destination_sha256"],
                "basis": basis,
            })
        payload = {
            "schema_version": 1,
            "kind": "quarantine_cleanup_1_4_4",
            "generated_at": "2026-07-29",
            "policy": {
                "permanent_discard": "byte exact, strong TXT/EPUB equivalence, or audited same-work containment at least 90% with a current retained reference",
                "preserve": "distinct work, unique supplement, or same-work variant below 90% containment",
                "queue": "all 51 meaningful legacy queue files resolved; two longer copies become representatives",
                "purge": "irreversible only after current endpoint SHA-256 revalidation and a recoverable DB backup",
            },
            "restore": restore,
            "upgrade_restored": [{
                "restore_operation_id": 518,
                "restored_file_id": restored_upgrade["file_id"],
                "old_keep_file_id": restored_upgrade["keep_file_id"],
                "expected_old_keep_sha256": sha(
                    file_row(restored_upgrade["keep_file_id"])["canonical_path"]
                ),
                "basis": "restored 1-875+외전 supersedes active 1-846 edition",
            }],
            "queue_discard": queue_discard,
            "queue_upgrade": queue_upgrade,
            "untracked_queue_discard": untracked,
            "missing_quarantine_ack": missing_ack,
            "metadata_cleanup": metadata_cleanup,
            "purge_revalidation": sorted(
                revalidation, key=lambda item: item["operation_id"]
            ),
            "purge_operation_ids": purge_ids,
            "inventory": {
                "exact_quarantine_to_purge": len(exact_rows),
                "old_user_quarantine_to_purge": len(safe_user),
                "missing_quarantine_to_ack": len(missing_ack),
                "restores": len(restore),
                "tracked_queue_discard": len(queue_discard),
                "tracked_queue_upgrade": len(queue_upgrade),
                "untracked_queue_discard": len(untracked),
                "metadata_cleanup": len(metadata_cleanup),
            },
        }
        return payload
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--untracked-path",
        default=str(
            Path.home() / "Documents" / "txt_temp" / "trash_bin" /
            "author_conflicts" / "공포수선세계恐怖修仙世界 2073 完 [룡사지龙蛇枝].txt"
        ),
    )
    args = parser.parse_args()
    payload = build(args.state_db, args.untracked_path)
    _atomic_json(args.output, payload)
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "inventory": payload["inventory"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
