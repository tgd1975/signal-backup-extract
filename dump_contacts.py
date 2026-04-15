#!/usr/bin/env python3
"""
Scan a Signal Desktop plaintext export and produce a contacts.json mapping
of chat/author IDs → human-readable names.

The export does not include names — only numeric IDs. This script collects
every ID that appears in main.jsonl, annotates each with message count and
date range, and writes a contacts.json you can fill in by hand.

Re-running is safe: existing names in contacts.json are preserved.

Usage:
    python dump_contacts.py <source_folder> [--output contacts.json]
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ms_to_iso(ms: int) -> str:
    """Convert a millisecond epoch to a UTC ISO-8601 date string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")  # noqa: UP017


def _load_jsonl(path: Path) -> list[dict]:  # type: ignore[type-arg]
    records: list[dict] = []  # type: ignore[type-arg]
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                log.warning("Skipping line %d: %s", lineno, exc)
    return records


# ── Core ──────────────────────────────────────────────────────────────────────


def scan_records(
    records: list[dict],  # type: ignore[type-arg]
) -> tuple[dict[str, dict], dict[str, dict]]:  # type: ignore[type-arg]
    """
    Return (chats, authors) dicts keyed by ID.

    Each value has:
        message_count   int
        first_message   ISO date string
        last_message    ISO date string
        name            str (empty — to be filled in by hand)
    """
    chats: dict[str, dict] = {}  # type: ignore[type-arg]
    authors: dict[str, dict] = {}  # type: ignore[type-arg]

    def _update(
        mapping: dict[str, dict],  # type: ignore[type-arg]
        id_: str,
        date_ms: int,
    ) -> None:
        if id_ not in mapping:
            mapping[id_] = {
                "name": "",
                "message_count": 0,
                "first_message_ms": date_ms,
                "last_message_ms": date_ms,
            }
        entry = mapping[id_]
        entry["message_count"] += 1
        entry["first_message_ms"] = min(entry["first_message_ms"], date_ms)
        entry["last_message_ms"] = max(entry["last_message_ms"], date_ms)

    for record in records:
        item = record.get("chatItem", {})
        chat_id = item.get("chatId", "")
        author_id = item.get("authorId", "")
        date_ms = int(item.get("dateSent", 0))

        if chat_id:
            _update(chats, chat_id, date_ms)
        if author_id:
            _update(authors, author_id, date_ms)

    # Replace raw ms timestamps with human-readable dates for the output
    for mapping in (chats, authors):
        for entry in mapping.values():
            entry["first_message"] = _ms_to_iso(entry.pop("first_message_ms"))
            entry["last_message"] = _ms_to_iso(entry.pop("last_message_ms"))

    return chats, authors


def merge_with_existing(
    new: dict[str, dict],  # type: ignore[type-arg]
    existing: dict[str, dict],  # type: ignore[type-arg]
) -> dict[str, dict]:  # type: ignore[type-arg]
    """
    Merge freshly-scanned entries with an existing mapping.
    Existing names are preserved; stats are updated from the new scan.
    """
    merged: dict[str, dict] = {}  # type: ignore[type-arg]
    all_ids = set(new) | set(existing)
    for id_ in sorted(all_ids, key=lambda x: int(x) if x.isdigit() else 0):
        entry = dict(new.get(id_, existing.get(id_, {})))
        # Keep the hand-edited name if one exists
        if id_ in existing and existing[id_].get("name"):
            entry["name"] = existing[id_]["name"]
        merged[id_] = entry
    return merged


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a Signal export and produce a contacts.json ID→name mapping."
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Source export folder (contains main.jsonl)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Output file (default: <source>/contacts.json)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.CRITICAL)
    elif args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    src_jsonl = args.source / "main.jsonl"
    if not src_jsonl.exists():
        log.error("main.jsonl not found at %s", src_jsonl)
        sys.exit(1)

    output_path = args.output or (args.source / "contacts.json")

    log.info("Reading %s ...", src_jsonl)
    records = _load_jsonl(src_jsonl)
    log.info("  Loaded %s records.", f"{len(records):,}")

    chats, authors = scan_records(records)
    log.info("  Found %d unique chat IDs, %d unique author IDs.", len(chats), len(authors))

    # Load existing contacts.json to preserve hand-edited names
    existing_chats: dict[str, dict] = {}  # type: ignore[type-arg]
    existing_authors: dict[str, dict] = {}  # type: ignore[type-arg]
    if output_path.exists():
        with open(output_path, encoding="utf-8") as fh:
            existing = json.load(fh)
        existing_chats = existing.get("chats", {})
        existing_authors = existing.get("authors", {})
        log.info("  Merging with existing %s (names preserved).", output_path.name)

    output = {
        "_instructions": (
            "Fill in the 'name' field for each ID. "
            "Re-running dump_contacts.py will preserve your edits."
        ),
        "chats": merge_with_existing(chats, existing_chats),
        "authors": merge_with_existing(authors, existing_authors),
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    log.info("  Contacts written to %s", output_path)
    log.info("")
    log.info("Next steps:")
    log.info("  1. Open %s", output_path)
    log.info('  2. Fill in the "name" field for each chat/author ID you care about')
    log.info("  3. Use --chat-id with signal_backup_extract.py and --contacts %s", output_path)


if __name__ == "__main__":
    main()
