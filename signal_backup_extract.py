#!/usr/bin/env python3
"""
Filter a Signal Desktop plaintext export for a specific chat and date range,
then copy all referenced media files to the target folder.

Usage:
    python signal_backup_extract.py <source_folder> <target_folder>
        --chat-id <id> [--chat-id <id> ...]
        [--after YYYY-MM-DD] [--before YYYY-MM-DD]
        [--author-id <id>]
        [--incoming | --outgoing]
        [--text-contains STR | --text-regex PATTERN]
        [--has-media | --no-media] [--media-type MIME]
        [--format jsonl|json]
        [--skip-media | --media-only]
        [--rename-media timestamp]
        [--contacts FILE]
        [--dry-run]

    source_folder   must contain main.jsonl and a files/ subdirectory
    target_folder   is created if absent; receives:
                        filtered.jsonl/json  — matching messages
                        media/               — copies of referenced attachments
                        media_list.txt       — per-attachment status report
"""

import argparse
import base64
import hashlib
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Map Signal content-type → file extension (fallback when no fileName present)
CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "audio/aac": ".aac",
    "application/pdf": ".pdf",
}

# Characters that are illegal in Windows filenames
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|+]')


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class MediaRef:
    """Everything needed to locate, copy, and report one attachment."""

    content_type: str
    hex_hash: str | None  # SHA-256(plaintextHash || localKey) → sharded filename
    pointer: dict  # type: ignore[type-arg]  # raw JSON pointer block
    att: dict  # type: ignore[type-arg]  # raw JSON attachment block
    # filled in during the copy phase:
    target_name: str = field(default="")
    status: str = field(default="PENDING")
    date_sent: int = field(default=0)  # ms epoch from chatItem.dateSent
    author_id: str = field(default="")  # from chatItem.authorId


# ── Signal filename resolution ─────────────────────────────────────────────────


def _compute_hex_hash(plaintext_hash_b64: str, local_key_b64: str) -> str | None:
    """
    Derive the on-disk filename stem used by Signal Desktop's backup export.

    Signal stores each attachment as:
        files/<first-2-hex>/<SHA-256(plaintextHash || localKey)>.<ext>

    Both inputs come from locatorInfo and are base64-encoded.
    """
    try:
        ph = base64.b64decode(plaintext_hash_b64)
        lk = base64.b64decode(local_key_b64)
        return hashlib.sha256(ph + lk).hexdigest()
    except Exception:
        return None


def _find_source_file(files_root: Path, hex_hash: str) -> Path | None:
    """
    Locate an attachment in the hash-sharded files/ directory.
    Signal appends the original extension to the hash stem, so we match by prefix.
    """
    folder = files_root / hex_hash[:2]
    if not folder.is_dir():
        return None
    for candidate in folder.iterdir():
        if candidate.name.startswith(hex_hash):
            return candidate
    return None


def _safe_filename(name: str) -> str:
    """Replace characters that are illegal in Windows filenames."""
    return _UNSAFE_CHARS.sub("_", name)


def _make_target_name(pointer: dict, att: dict, src_file: Path | None) -> str:  # type: ignore[type-arg]
    """
    Choose a human-readable target filename for the copied attachment.

    Priority:
        1. Original fileName from the JSON (already has a good name)
        2. transitCdnKey + extension (sanitized)
        3. clientUuid + extension (sanitized, last resort)

    Extension preference: taken from the source file on disk (authoritative),
    falling back to a MIME-type lookup.
    """
    fname = str(pointer.get("fileName", "")).strip()
    if fname:
        return fname

    ext = src_file.suffix if src_file else CONTENT_TYPE_EXT.get(pointer.get("contentType", ""), "")

    cdn_key = pointer.get("transitCdnKey", "")
    if cdn_key:
        return _safe_filename(cdn_key) + ext

    uuid = att.get("clientUuid", "unknown")
    return _safe_filename(uuid) + ext


# ── JSONL helpers ─────────────────────────────────────────────────────────────


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


def _write_jsonl(records: list[dict], path: Path) -> None:  # type: ignore[type-arg]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_json_array(records: list[dict], path: Path) -> None:  # type: ignore[type-arg]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)


# ── Contacts ──────────────────────────────────────────────────────────────────


def _load_contacts(path: Path) -> dict[str, str]:
    """
    Load a contacts.json produced by dump_contacts.py.
    Returns a flat id → name dict (both chats and authors sections).
    dump_contacts writes both sections as dict[id, {name, ...}].
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    contacts: dict[str, str] = {}
    for section in ("chats", "authors"):
        for cid, entry in data.get(section, {}).items():
            name = str(entry.get("name", "")).strip()
            if name:
                contacts[str(cid)] = name
    return contacts


# ── Core logic ────────────────────────────────────────────────────────────────


def filter_records(
    records: list[dict],  # type: ignore[type-arg]
    *,
    chat_ids: set[str],
    after_ms: int | None = None,
    before_ms: int | None = None,
    author_id: str | None = None,
    incoming_only: bool = False,
    outgoing_only: bool = False,
    text_contains: str | None = None,
    text_regex: re.Pattern | None = None,  # type: ignore[type-arg]
    media_type: str | None = None,
    has_media: bool = False,
    no_media: bool = False,
) -> tuple[list[dict], list[MediaRef]]:  # type: ignore[type-arg]
    """
    Return (matching_records, media_refs) for records that pass all active filters.
    """
    filtered: list[dict] = []  # type: ignore[type-arg]
    media_refs: list[MediaRef] = []

    for record in records:
        item = record.get("chatItem", {})

        if item.get("chatId") not in chat_ids:
            continue

        date_sent = int(item.get("dateSent", 0))
        if after_ms is not None and date_sent < after_ms:
            continue
        if before_ms is not None and date_sent > before_ms:
            continue

        if author_id is not None and item.get("authorId") != author_id:
            continue

        if incoming_only and "incoming" not in item:
            continue
        if outgoing_only and "outgoing" not in item:
            continue

        if text_contains is not None or text_regex is not None:
            body = item.get("standardMessage", {}).get("text", {}).get("body", "")
            if text_contains is not None and text_contains not in body:
                continue
            if text_regex is not None and not text_regex.search(body):
                continue

        attachments = item.get("standardMessage", {}).get("attachments", [])

        if has_media and not attachments:
            continue
        if no_media and attachments:
            continue
        if media_type is not None and not any(
            a.get("pointer", {}).get("contentType") == media_type for a in attachments
        ):
            continue

        filtered.append(record)

        item_author_id = item.get("authorId", "")
        for att in attachments:
            pointer = att.get("pointer", {})
            locator = pointer.get("locatorInfo", {})
            ph_b64 = locator.get("plaintextHash")
            lk_b64 = locator.get("localKey")
            media_refs.append(
                MediaRef(
                    content_type=pointer.get("contentType", ""),
                    hex_hash=_compute_hex_hash(ph_b64, lk_b64) if (ph_b64 and lk_b64) else None,
                    pointer=pointer,
                    att=att,
                    date_sent=date_sent,
                    author_id=item_author_id,
                )
            )

    return filtered, media_refs


def copy_media(
    media_refs: list[MediaRef],
    src_files: Path,
    dst_media: Path,
    rename_timestamp: bool = False,
) -> int:
    """
    Locate and copy each attachment. Updates each MediaRef in-place with
    target_name and status. Returns the number of successfully copied files.
    """
    copied = 0
    for ref in media_refs:
        src_file = _find_source_file(src_files, ref.hex_hash) if ref.hex_hash else None
        ref.target_name = _make_target_name(ref.pointer, ref.att, src_file)

        if rename_timestamp and ref.date_sent:
            prefix = datetime.fromtimestamp(ref.date_sent / 1000, tz=UTC).strftime("%Y%m%d_%H%M%S")
            ref.target_name = f"{prefix}_{ref.target_name}"

        if src_file:
            shutil.copy2(src_file, dst_media / ref.target_name)
            ref.status = "OK"
            copied += 1
        else:
            ref.status = "NOT FOUND"

    return copied


def resolve_media_names(media_refs: list[MediaRef], rename_timestamp: bool = False) -> None:
    """Populate target_name on each ref without copying (used in dry-run mode)."""
    for ref in media_refs:
        ref.target_name = _make_target_name(ref.pointer, ref.att, None)
        if rename_timestamp and ref.date_sent:
            prefix = datetime.fromtimestamp(ref.date_sent / 1000, tz=UTC).strftime("%Y%m%d_%H%M%S")
            ref.target_name = f"{prefix}_{ref.target_name}"
        ref.status = "DRY RUN"


def write_media_report(
    media_refs: list[MediaRef],
    path: Path,
    contacts: dict[str, str] | None = None,
) -> None:
    col_name = 50
    col_ct = 25
    col_author = 30
    if contacts is not None:
        header = (
            f"{'fileName':<{col_name}}  {'contentType':<{col_ct}}  {'author':<{col_author}}  status"
        )
    else:
        header = f"{'fileName':<{col_name}}  {'contentType':<{col_ct}}  status"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        fh.write("-" * len(header) + "\n")
        for ref in media_refs:
            if contacts is not None:
                author_display = contacts.get(ref.author_id, ref.author_id) if ref.author_id else ""
                fh.write(
                    f"{ref.target_name:<{col_name}}  {ref.content_type:<{col_ct}}  {author_display:<{col_author}}  {ref.status}\n"
                )
            else:
                fh.write(
                    f"{ref.target_name:<{col_name}}  {ref.content_type:<{col_ct}}  {ref.status}\n"
                )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_date(value: str) -> datetime:
    """Parse an ISO date string (YYYY-MM-DD) into an aware UTC datetime."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)  # noqa: UP017
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}', expected YYYY-MM-DD") from exc


def _compile_regex(pattern: str) -> re.Pattern:  # type: ignore[type-arg]
    """Compile a regex pattern, raising ArgumentTypeError on bad syntax."""
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"Invalid regex '{pattern}': {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter a Signal Desktop export and copy referenced media."
    )
    parser.add_argument(
        "source", type=Path, help="Source export folder (contains main.jsonl and files/)"
    )
    parser.add_argument("target", type=Path, help="Target folder (created if absent)")

    parser.add_argument(
        "--chat-id",
        action="append",
        dest="chat_ids",
        required=True,
        metavar="ID",
        help="Chat ID to extract (repeatable; comma-separated values also accepted)",
    )
    parser.add_argument(
        "--after",
        metavar="YYYY-MM-DD",
        type=_parse_date,
        help="Include messages sent on or after this date (UTC)",
    )
    parser.add_argument(
        "--before",
        metavar="YYYY-MM-DD",
        type=_parse_date,
        help="Include messages sent on or before this date (UTC)",
    )
    parser.add_argument("--author-id", metavar="ID", help="Only include messages from this author")

    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--incoming", action="store_true", help="Only include received messages")
    direction.add_argument("--outgoing", action="store_true", help="Only include sent messages")

    text_filter = parser.add_mutually_exclusive_group()
    text_filter.add_argument(
        "--text-contains", metavar="STR", help="Only include messages whose body contains STR"
    )
    text_filter.add_argument(
        "--text-regex",
        metavar="PATTERN",
        type=_compile_regex,
        help="Only include messages whose body matches PATTERN",
    )

    media_presence = parser.add_mutually_exclusive_group()
    media_presence.add_argument(
        "--has-media", action="store_true", help="Only include messages with attachments"
    )
    media_presence.add_argument(
        "--no-media", action="store_true", help="Only include messages without attachments"
    )
    parser.add_argument(
        "--media-type",
        metavar="MIME",
        help="Only include messages with an attachment of this MIME type",
    )

    parser.add_argument(
        "--format",
        choices=["jsonl", "json"],
        default="jsonl",
        help="Output format for filtered messages (default: jsonl)",
    )

    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--skip-media", action="store_true", help="Write filtered messages only; skip media copy"
    )
    output_mode.add_argument(
        "--media-only", action="store_true", help="Copy media only; skip writing filtered messages"
    )

    parser.add_argument(
        "--rename-media",
        choices=["timestamp"],
        metavar="SCHEME",
        help="Rename copied media files (timestamp: prefix with YYYYMMDD_HHMMSS_)",
    )

    parser.add_argument(
        "--contacts", metavar="FILE", type=Path, help="contacts.json from dump_contacts.py"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Filter and report without copying any files",
    )

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) output"
    )
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Suppress all output")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Configure logging
    if args.quiet:
        logging.basicConfig(level=logging.CRITICAL)
    elif args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    src, dst = args.source, args.target

    src_jsonl = src / "main.jsonl"
    src_files = src / "files"
    dst_media = dst / "media"

    for p, label in [(src_jsonl, "main.jsonl"), (src_files, "files/")]:
        if not p.exists():
            log.error("Error: %s not found at %s", label, p)
            sys.exit(1)

    after_ms = int(args.after.timestamp() * 1000) if args.after else None
    before_ms = int(args.before.timestamp() * 1000) if args.before else None

    chat_ids = {cid.strip() for val in args.chat_ids for cid in val.split(",") if cid.strip()}

    contacts = _load_contacts(args.contacts) if args.contacts else None

    rename_timestamp = args.rename_media == "timestamp"

    log.info("Reading %s ...", src_jsonl)
    records = _load_jsonl(src_jsonl)
    log.info("  Loaded %s records.", f"{len(records):,}")

    filtered, media_refs = filter_records(
        records,
        chat_ids=chat_ids,
        after_ms=after_ms,
        before_ms=before_ms,
        author_id=args.author_id,
        incoming_only=args.incoming,
        outgoing_only=args.outgoing,
        text_contains=args.text_contains,
        text_regex=args.text_regex,
        media_type=args.media_type,
        has_media=args.has_media,
        no_media=args.no_media,
    )
    log.info("  Matched %s messages, %s attachments.", f"{len(filtered):,}", f"{len(media_refs):,}")

    if args.dry_run:
        log.info("  Dry run — no files will be written.")
        resolve_media_names(media_refs, rename_timestamp=rename_timestamp)
    else:
        dst.mkdir(parents=True, exist_ok=True)
        dst_media.mkdir(exist_ok=True)

        if not args.media_only:
            if args.format == "json":
                out_path = dst / "filtered.json"
                _write_json_array(filtered, out_path)
            else:
                out_path = dst / "filtered.jsonl"
                _write_jsonl(filtered, out_path)
            log.info("  Filtered %s -> %s", args.format.upper(), out_path)

        if not args.skip_media:
            copied = copy_media(media_refs, src_files, dst_media, rename_timestamp=rename_timestamp)
            missing = [r for r in media_refs if r.status == "NOT FOUND"]
            log.info("  Media copied: %d, not found: %d", copied, len(missing))

            report_path = dst / "media_list.txt"
            write_media_report(media_refs, report_path, contacts=contacts)
            log.info("  Media report  -> %s", report_path)

            if missing:
                log.warning("\nMissing files:")
                for ref in missing:
                    log.warning("  %s", ref.target_name)


if __name__ == "__main__":
    main()
