#!/usr/bin/env python3
"""Human review CLI for the 1.2.1 dedup decision store."""

import argparse
import json
import sqlite3
import sys

import decision_store
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR


DEFAULT_STATE_DB = str(STATE_DB)
DEFAULT_HOUSE_DIR = str(HOUSE_DIR)
DEFAULT_TEMP_DIR = str(TEMP_DIR)


def build_parser():
    parser = argparse.ArgumentParser(description="중복 검토 pair와 사람 판정을 관리합니다.")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--house", default=DEFAULT_HOUSE_DIR)
    parser.add_argument("--temp", default=DEFAULT_TEMP_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="빈 최신 schema DB를 생성합니다.")
    init.add_argument("--run", action="store_true")

    listing = subparsers.add_parser("list", help="검토 항목을 조회합니다.")
    listing.add_argument("--state", choices=decision_store.REVIEW_STATES)
    listing.add_argument("--classification")
    listing.add_argument("--file-id")

    review = subparsers.add_parser("review", help="수동 pair를 pending review로 등록합니다.")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_add = review_sub.add_parser("add")
    review_add.add_argument("--candidate-id", required=True)
    review_add.add_argument("--reference-id", required=True)
    review_add.add_argument("--classification", default="manual_review")
    review_add.add_argument("--queue-path")
    review_add.add_argument("--run", action="store_true")

    decide = subparsers.add_parser("decide", help="review에 최종 verdict를 적용합니다.")
    decide.add_argument("--review-id", required=True, type=int)
    decide.add_argument("--candidate-id", required=True)
    decide.add_argument("--reference-id", required=True)
    decide.add_argument("--verdict", required=True, choices=decision_store.FINAL_VERDICTS)
    decide.add_argument(
        "--variant-kind",
        default="other",
        choices=("base", "revision", "adult", "translation", "other"),
    )
    decide.add_argument("--note")
    decide.add_argument("--run", action="store_true")

    discard = subparsers.add_parser(
        "discard", help="same_content의 비대표 파일을 managed quarantine으로 보냅니다."
    )
    discard.add_argument("--review-id", required=True, type=int)
    discard.add_argument("--run", action="store_true")

    cancel = subparsers.add_parser("cancel", help="고립된 최초 판정을 취소하고 review를 재개합니다.")
    cancel.add_argument("--decision-id", required=True, type=int)
    cancel.add_argument("--run", action="store_true")

    correct = subparsers.add_parser("correct", help="고립된 최초 판정을 원자적으로 정정합니다.")
    correct.add_argument("--decision-id", required=True, type=int)
    correct.add_argument("--verdict", required=True, choices=decision_store.FINAL_VERDICTS)
    correct.add_argument(
        "--variant-kind", default="other",
        choices=("base", "revision", "adult", "translation", "other"),
    )
    correct.add_argument("--note")
    correct.add_argument("--run", action="store_true")

    for command in ("defer", "reopen"):
        state_parser = subparsers.add_parser(command)
        state_parser.add_argument("--review-id", required=True, type=int)
        state_parser.add_argument("--run", action="store_true")

    protect = subparsers.add_parser("protect")
    protect.add_argument("--file-id", required=True)
    protect.add_argument("--value", required=True, choices=("on", "off"))
    protect.add_argument("--run", action="store_true")

    representative = subparsers.add_parser("representative")
    representative.add_argument("--variant-id", required=True, type=int)
    representative.add_argument("--file-id", required=True)
    representative.add_argument("--run", action="store_true")
    return parser


def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _open_existing(path):
    conn = decision_store.connect_state_db(path)
    decision_store.validate_schema(conn)
    return conn


def _run_locked(args, action, callback):
    from mutation_io import mutation_lock_for_roots
    with mutation_lock_for_roots(args.house, args.temp, f"decision-{action}"):
        return callback()


def run(args):
    if args.command == "init":
        if not args.run:
            _print_json({"dry_run": True, "action": "init", "state_db": args.state_db})
            return 0
        conn = decision_store.initialize_state_db(args.state_db)
        conn.close()
        _print_json({"dry_run": False, "action": "init", "state_db": args.state_db})
        return 0

    conn = _open_existing(args.state_db)
    try:
        if args.command == "list":
            rows = decision_store.list_review_items(
                conn,
                args.state,
                classification=args.classification,
                file_id=args.file_id,
            )
            _print_json([dict(row) for row in rows])
            return 0

        if args.command == "review":
            preview = decision_store.preview_review_pair(
                conn, args.candidate_id, args.reference_id
            )
            preview.update({
                "action": "review_add",
                "classification": args.classification,
                "dry_run": not args.run,
            })
            if args.run:
                preview["review_id"] = _run_locked(
                    args, "review-add", lambda: decision_store.add_review_item(
                        conn,
                        candidate_file_id=args.candidate_id,
                        reference_file_id=args.reference_id,
                        classification=args.classification,
                        queue_path=args.queue_path,
                    ),
                )
            _print_json(preview)
            return 0

        if args.command == "decide":
            preview = decision_store.preview_decision(
                conn,
                review_id=args.review_id,
                candidate_file_id=args.candidate_id,
                reference_file_id=args.reference_id,
                verdict=args.verdict,
            )
            preview["dry_run"] = not args.run
            if args.run:
                preview["decision_id"] = _run_locked(
                    args, "decide", lambda: decision_store.apply_decision(
                        conn,
                        review_id=args.review_id,
                        candidate_file_id=args.candidate_id,
                        reference_file_id=args.reference_id,
                        verdict=args.verdict,
                        variant_kind=args.variant_kind,
                        note=args.note,
                    ),
                )
            _print_json(preview)
            return 0

        if args.command == "discard":
            payload = decision_store.preview_decided_review_disposition(
                conn, args.review_id
            )
            payload["dry_run"] = not args.run
            if args.run:
                payload.update(_run_locked(
                    args,
                    "discard",
                    lambda: decision_store.quarantine_decided_review(
                        conn, args.review_id
                    ),
                ))
            _print_json(payload)
            return 0

        if args.command == "cancel":
            payload = {"action": "cancel", "decision_id": args.decision_id, "dry_run": not args.run}
            if args.run:
                payload["review_id"] = _run_locked(
                    args, "cancel",
                    lambda: decision_store.cancel_decision(conn, args.decision_id),
                )
            _print_json(payload)
            return 0

        if args.command == "correct":
            payload = {
                "action": "correct", "decision_id": args.decision_id,
                "verdict": args.verdict, "dry_run": not args.run,
            }
            if args.run:
                payload["new_decision_id"] = _run_locked(
                    args, "correct", lambda: decision_store.correct_decision(
                        conn,
                        decision_id=args.decision_id,
                        verdict=args.verdict,
                        variant_kind=args.variant_kind,
                        note=args.note,
                    ),
                )
            _print_json(payload)
            return 0

        if args.command in {"defer", "reopen"}:
            target = "deferred" if args.command == "defer" else "pending"
            if args.run:
                _run_locked(
                    args, args.command,
                    lambda: decision_store.set_review_state(conn, args.review_id, target),
                )
            _print_json({
                "action": args.command,
                "review_id": args.review_id,
                "target_state": target,
                "dry_run": not args.run,
            })
            return 0

        if args.command == "protect":
            value = args.value == "on"
            if args.run:
                _run_locked(
                    args, "protect",
                    lambda: decision_store.set_file_protected(conn, args.file_id, value),
                )
            _print_json({
                "action": "protect",
                "file_id": args.file_id,
                "protected": value,
                "dry_run": not args.run,
            })
            return 0

        if args.command == "representative":
            if args.run:
                _run_locked(
                    args, "representative",
                    lambda: decision_store.replace_representative(
                        conn, args.variant_id, args.file_id
                    ),
                )
            _print_json({
                "action": "representative",
                "variant_id": args.variant_id,
                "file_id": args.file_id,
                "dry_run": not args.run,
            })
            return 0
        raise RuntimeError(f"unsupported command: {args.command}")
    finally:
        conn.close()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (FileNotFoundError, sqlite3.Error, ValueError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
