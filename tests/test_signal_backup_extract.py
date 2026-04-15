"""Unit tests for signal_backup_extract."""

import argparse
import base64
import hashlib
import json
import re
from datetime import UTC
from pathlib import Path

import pytest

from signal_backup_extract import (
    MediaRef,
    _compute_hex_hash,
    _find_source_file,
    _load_contacts,
    _load_jsonl,
    _make_target_name,
    _parse_date,
    _safe_filename,
    _write_json_array,
    _write_jsonl,
    copy_media,
    filter_records,
    resolve_media_names,
    write_media_report,
)

# ── _compute_hex_hash ─────────────────────────────────────────────────────────


class TestComputeHexHash:
    def test_known_value(self) -> None:
        # Build expected hash from raw bytes, then base64-encode the inputs.
        ph_bytes = b"plaintext_hash_bytes"
        lk_bytes = b"local_key_bytes"
        expected = hashlib.sha256(ph_bytes + lk_bytes).hexdigest()

        ph_b64 = base64.b64encode(ph_bytes).decode()
        lk_b64 = base64.b64encode(lk_bytes).decode()

        assert _compute_hex_hash(ph_b64, lk_b64) == expected

    def test_returns_64_hex_chars(self) -> None:
        ph_b64 = base64.b64encode(b"a").decode()
        lk_b64 = base64.b64encode(b"b").decode()
        result = _compute_hex_hash(ph_b64, lk_b64)
        assert result is not None
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_invalid_base64_returns_none(self) -> None:
        assert _compute_hex_hash("not-valid-base64!!!", "also-bad!!!") is None

    def test_empty_strings_return_none(self) -> None:
        # Empty string is valid base64 (decodes to b""), so hash is computed —
        # we just check the function doesn't crash and returns a string.
        result = _compute_hex_hash("", "")
        assert result is not None
        assert len(result) == 64


# ── _safe_filename ────────────────────────────────────────────────────────────


class TestSafeFilename:
    @pytest.mark.parametrize(  # type: ignore[misc]
        "inp, expected",
        [
            ("normal_name.jpg", "normal_name.jpg"),
            ("file/with/slashes", "file_with_slashes"),
            ('has"quotes"', "has_quotes_"),
            ("has<angle>brackets", "has_angle_brackets"),
            ("col:on", "col_on"),
            ("star*name", "star_name"),
        ],
    )
    def test_replaces_unsafe_chars(self, inp: str, expected: str) -> None:
        assert _safe_filename(inp) == expected


# ── _make_target_name ─────────────────────────────────────────────────────────


class TestMakeTargetName:
    def test_prefers_filename_from_pointer(self) -> None:
        pointer = {"fileName": "signal-photo.jpeg", "contentType": "image/jpeg"}
        assert _make_target_name(pointer, {}, None) == "signal-photo.jpeg"

    def test_falls_back_to_cdn_key(self) -> None:
        pointer = {"fileName": "", "transitCdnKey": "cdn/key/value", "contentType": "image/png"}
        src = Path("fake.png")
        # src_file.suffix → ".png"
        result = _make_target_name(pointer, {}, src)
        assert result == "cdn_key_value.png"

    def test_falls_back_to_client_uuid(self) -> None:
        pointer = {"fileName": "", "contentType": "video/mp4"}
        att = {"clientUuid": "some-uuid-1234"}
        result = _make_target_name(pointer, att, None)
        assert result == "some-uuid-1234.mp4"

    def test_unknown_type_and_no_src_gives_no_ext(self) -> None:
        pointer = {"fileName": "", "contentType": "application/octet-stream"}
        att = {"clientUuid": "uid"}
        result = _make_target_name(pointer, att, None)
        assert result == "uid"

    def test_cdn_key_sanitized(self) -> None:
        pointer = {"fileName": "", "transitCdnKey": "key:with*bad?chars", "contentType": ""}
        result = _make_target_name(pointer, {}, None)
        assert result == "key_with_bad_chars"


# ── filter_records ────────────────────────────────────────────────────────────


def _make_record(
    chat_id: str = "1",
    author_id: str = "10",
    date_sent: int = 1_000_000,
    direction: str = "incoming",
    attachments: list[dict] | None = None,  # type: ignore[type-arg]
    body: str | None = None,
) -> dict:  # type: ignore[type-arg]
    std_msg: dict = {"attachments": attachments or []}  # type: ignore[type-arg]
    if body is not None:
        std_msg["text"] = {"body": body}
    item: dict = {  # type: ignore[type-arg]
        "chatId": chat_id,
        "authorId": author_id,
        "dateSent": str(date_sent),
        direction: {"dateReceived": str(date_sent + 100)},
        "standardMessage": std_msg,
    }
    return {"chatItem": item}


class TestFilterRecords:
    def test_filters_by_chat_id(self) -> None:
        records = [_make_record(chat_id="1"), _make_record(chat_id="2")]
        filtered, _ = filter_records(records, chat_ids={"1"})
        assert len(filtered) == 1

    def test_filters_by_multiple_chat_ids(self) -> None:
        records = [_make_record(chat_id="1"), _make_record(chat_id="2"), _make_record(chat_id="3")]
        filtered, _ = filter_records(records, chat_ids={"1", "2"})
        assert len(filtered) == 2

    def test_after_ms_excludes_older(self) -> None:
        records = [_make_record(date_sent=500), _make_record(date_sent=1500)]
        filtered, _ = filter_records(records, chat_ids={"1"}, after_ms=1000)
        assert len(filtered) == 1

    def test_before_ms_excludes_newer(self) -> None:
        records = [_make_record(date_sent=500), _make_record(date_sent=1500)]
        filtered, _ = filter_records(records, chat_ids={"1"}, before_ms=1000)
        assert len(filtered) == 1

    def test_date_range_both_bounds(self) -> None:
        records = [
            _make_record(date_sent=100),
            _make_record(date_sent=500),
            _make_record(date_sent=900),
        ]
        filtered, _ = filter_records(records, chat_ids={"1"}, after_ms=200, before_ms=800)
        assert len(filtered) == 1

    def test_author_id_filter(self) -> None:
        records = [_make_record(author_id="10"), _make_record(author_id="99")]
        filtered, _ = filter_records(records, chat_ids={"1"}, author_id="10")
        assert len(filtered) == 1

    def test_incoming_only(self) -> None:
        records = [_make_record(direction="incoming"), _make_record(direction="outgoing")]
        filtered, _ = filter_records(records, chat_ids={"1"}, incoming_only=True)
        assert len(filtered) == 1

    def test_outgoing_only(self) -> None:
        records = [_make_record(direction="incoming"), _make_record(direction="outgoing")]
        filtered, _ = filter_records(records, chat_ids={"1"}, outgoing_only=True)
        assert len(filtered) == 1

    def test_no_match_returns_empty(self) -> None:
        records = [_make_record(chat_id="1")]
        filtered, media = filter_records(records, chat_ids={"99"})
        assert filtered == []
        assert media == []

    def test_attachment_extracted(self) -> None:
        ph_b64 = base64.b64encode(b"ph").decode()
        lk_b64 = base64.b64encode(b"lk").decode()
        att = {
            "pointer": {
                "contentType": "image/jpeg",
                "fileName": "photo.jpg",
                "locatorInfo": {"plaintextHash": ph_b64, "localKey": lk_b64},
            },
            "clientUuid": "uuid-1",
        }
        records = [_make_record(attachments=[att])]
        _, media_refs = filter_records(records, chat_ids={"1"})
        assert len(media_refs) == 1
        ref = media_refs[0]
        assert ref.content_type == "image/jpeg"
        assert ref.hex_hash is not None
        assert len(ref.hex_hash) == 64

    def test_attachment_without_locator_has_no_hex_hash(self) -> None:
        att = {"pointer": {"contentType": "image/jpeg", "fileName": "x.jpg"}, "clientUuid": "u"}
        records = [_make_record(attachments=[att])]
        _, media_refs = filter_records(records, chat_ids={"1"})
        assert media_refs[0].hex_hash is None

    def test_empty_records(self) -> None:
        filtered, media = filter_records([], chat_ids={"1"})
        assert filtered == []
        assert media == []

    def test_media_ref_date_sent_and_author_id_populated(self) -> None:
        ph_b64 = base64.b64encode(b"ph").decode()
        lk_b64 = base64.b64encode(b"lk").decode()
        att = {
            "pointer": {
                "contentType": "image/jpeg",
                "fileName": "photo.jpg",
                "locatorInfo": {"plaintextHash": ph_b64, "localKey": lk_b64},
            },
            "clientUuid": "uuid-1",
        }
        records = [_make_record(author_id="42", date_sent=999_000, attachments=[att])]
        _, media_refs = filter_records(records, chat_ids={"1"})
        assert media_refs[0].date_sent == 999_000
        assert media_refs[0].author_id == "42"

    # text filters

    def test_text_contains_hit(self) -> None:
        records = [_make_record(body="hello world"), _make_record(body="goodbye")]
        filtered, _ = filter_records(records, chat_ids={"1"}, text_contains="hello")
        assert len(filtered) == 1

    def test_text_contains_miss(self) -> None:
        records = [_make_record(body="goodbye")]
        filtered, _ = filter_records(records, chat_ids={"1"}, text_contains="hello")
        assert filtered == []

    def test_text_regex_hit(self) -> None:
        records = [_make_record(body="order 12345"), _make_record(body="no digits here")]
        filtered, _ = filter_records(records, chat_ids={"1"}, text_regex=re.compile(r"\d+"))
        assert len(filtered) == 1

    def test_text_regex_miss(self) -> None:
        records = [_make_record(body="no digits here")]
        filtered, _ = filter_records(records, chat_ids={"1"}, text_regex=re.compile(r"\d+"))
        assert filtered == []

    def test_text_filter_missing_body_does_not_crash(self) -> None:
        # record with no text field at all
        records = [_make_record()]
        filtered, _ = filter_records(records, chat_ids={"1"}, text_contains="hello")
        assert filtered == []

    # media presence filters

    def _att(self, mime: str = "image/jpeg") -> dict:  # type: ignore[type-arg]
        return {"pointer": {"contentType": mime}, "clientUuid": "u"}

    def test_has_media_includes_only_messages_with_attachments(self) -> None:
        records = [_make_record(attachments=[self._att()]), _make_record()]
        filtered, _ = filter_records(records, chat_ids={"1"}, has_media=True)
        assert len(filtered) == 1

    def test_no_media_includes_only_messages_without_attachments(self) -> None:
        records = [_make_record(attachments=[self._att()]), _make_record()]
        filtered, _ = filter_records(records, chat_ids={"1"}, no_media=True)
        assert len(filtered) == 1

    def test_media_type_filter_hit(self) -> None:
        records = [
            _make_record(attachments=[self._att("image/png")]),
            _make_record(attachments=[self._att("video/mp4")]),
        ]
        filtered, _ = filter_records(records, chat_ids={"1"}, media_type="image/png")
        assert len(filtered) == 1

    def test_media_type_filter_miss(self) -> None:
        records = [_make_record(attachments=[self._att("video/mp4")])]
        filtered, _ = filter_records(records, chat_ids={"1"}, media_type="image/jpeg")
        assert filtered == []

    def test_media_type_no_attachments_excluded(self) -> None:
        records = [_make_record()]
        filtered, _ = filter_records(records, chat_ids={"1"}, media_type="image/jpeg")
        assert filtered == []


# ── _find_source_file ─────────────────────────────────────────────────────────


class TestFindSourceFile:
    def test_finds_file_by_hash_prefix(self, tmp_path: Path) -> None:
        hex_hash = "abcdef1234567890" * 4  # 64 chars
        shard = tmp_path / hex_hash[:2]
        shard.mkdir()
        expected = shard / f"{hex_hash}.jpg"
        expected.write_bytes(b"data")

        result = _find_source_file(tmp_path, hex_hash)
        assert result == expected

    def test_returns_none_when_shard_missing(self, tmp_path: Path) -> None:
        assert _find_source_file(tmp_path, "ab" + "0" * 62) is None

    def test_returns_none_when_no_matching_file(self, tmp_path: Path) -> None:
        hex_hash = "abcdef1234567890" * 4
        shard = tmp_path / hex_hash[:2]
        shard.mkdir()
        (shard / "different_file.jpg").write_bytes(b"x")

        assert _find_source_file(tmp_path, hex_hash) is None


# ── _load_jsonl / _write_jsonl ────────────────────────────────────────────────


class TestLoadWriteJsonl:
    def test_roundtrip(self, tmp_path: Path) -> None:
        records: list[dict] = [{"a": 1}, {"b": "hello"}, {"c": [1, 2, 3]}]  # type: ignore[type-arg]
        path = tmp_path / "test.jsonl"
        _write_jsonl(records, path)
        assert _load_jsonl(path) == records

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
        assert _load_jsonl(path) == [{"a": 1}, {"b": 2}]

    def test_skips_invalid_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")
        assert _load_jsonl(path) == [{"a": 1}, {"b": 2}]

    def test_preserves_unicode(self, tmp_path: Path) -> None:
        records = [{"text": "héllo wörld 日本語"}]
        path = tmp_path / "test.jsonl"
        _write_jsonl(records, path)
        assert _load_jsonl(path) == records

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert _load_jsonl(path) == []


# ── _write_json_array ─────────────────────────────────────────────────────────


class TestWriteJsonArray:
    def test_roundtrip(self, tmp_path: Path) -> None:
        records: list[dict] = [{"a": 1}, {"b": "hello"}]  # type: ignore[type-arg]
        path = tmp_path / "out.json"
        _write_json_array(records, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == records

    def test_unicode(self, tmp_path: Path) -> None:
        records = [{"text": "héllo 日本語"}]
        path = tmp_path / "out.json"
        _write_json_array(records, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == records

    def test_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        _write_json_array([], path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == []


# ── resolve_media_names ───────────────────────────────────────────────────────


class TestResolveMediaNames:
    def test_sets_status_to_dry_run(self) -> None:
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={"fileName": "photo.jpg"},
            att={},
        )
        resolve_media_names([ref])
        assert ref.status == "DRY RUN"
        assert ref.target_name == "photo.jpg"

    def test_empty_list(self) -> None:
        resolve_media_names([])  # should not raise

    def test_rename_timestamp_prefixes_name(self) -> None:
        # 2000-01-02 03:04:05 UTC in ms
        ts_ms = 946782245000
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={"fileName": "photo.jpg"},
            att={},
            date_sent=ts_ms,
        )
        resolve_media_names([ref], rename_timestamp=True)
        assert ref.target_name.startswith("20000102_030405_")
        assert ref.target_name.endswith("photo.jpg")

    def test_rename_timestamp_skipped_when_date_sent_zero(self) -> None:
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={"fileName": "photo.jpg"},
            att={},
            date_sent=0,
        )
        resolve_media_names([ref], rename_timestamp=True)
        assert ref.target_name == "photo.jpg"


# ── copy_media ────────────────────────────────────────────────────────────────


class TestCopyMedia:
    def test_copies_found_file(self, tmp_path: Path) -> None:
        hex_hash = "aa" + "b" * 62
        src_files = tmp_path / "files"
        shard = src_files / hex_hash[:2]
        shard.mkdir(parents=True)
        src = shard / f"{hex_hash}.jpg"
        src.write_bytes(b"image data")

        dst_media = tmp_path / "media"
        dst_media.mkdir()

        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=hex_hash,
            pointer={"fileName": "photo.jpg"},
            att={},
        )
        copied = copy_media([ref], src_files, dst_media)

        assert copied == 1
        assert ref.status == "OK"
        assert (dst_media / "photo.jpg").read_bytes() == b"image data"

    def test_marks_missing_file_not_found(self, tmp_path: Path) -> None:
        src_files = tmp_path / "files"
        src_files.mkdir()
        dst_media = tmp_path / "media"
        dst_media.mkdir()

        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash="aa" + "0" * 62,
            pointer={"fileName": "missing.jpg"},
            att={},
        )
        copied = copy_media([ref], src_files, dst_media)

        assert copied == 0
        assert ref.status == "NOT FOUND"

    def test_ref_without_hex_hash_is_not_found(self, tmp_path: Path) -> None:
        src_files = tmp_path / "files"
        src_files.mkdir()
        dst_media = tmp_path / "media"
        dst_media.mkdir()

        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={"fileName": "x.jpg"},
            att={},
        )
        copied = copy_media([ref], src_files, dst_media)
        assert copied == 0
        assert ref.status == "NOT FOUND"

    def test_rename_timestamp_prefixes_name(self, tmp_path: Path) -> None:
        hex_hash = "cc" + "d" * 62
        src_files = tmp_path / "files"
        shard = src_files / hex_hash[:2]
        shard.mkdir(parents=True)
        src = shard / f"{hex_hash}.jpg"
        src.write_bytes(b"data")

        dst_media = tmp_path / "media"
        dst_media.mkdir()

        ts_ms = 946782245000  # 2000-01-02 03:04:05 UTC
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=hex_hash,
            pointer={"fileName": "photo.jpg"},
            att={},
            date_sent=ts_ms,
        )
        copy_media([ref], src_files, dst_media, rename_timestamp=True)
        assert ref.target_name.startswith("20000102_030405_")
        assert (dst_media / ref.target_name).exists()


# ── write_media_report ────────────────────────────────────────────────────────


class TestWriteMediaReport:
    def test_writes_header_and_rows(self, tmp_path: Path) -> None:
        refs = [
            MediaRef(
                content_type="image/jpeg",
                hex_hash=None,
                pointer={},
                att={},
                target_name="photo.jpg",
                status="OK",
            ),
            MediaRef(
                content_type="video/mp4",
                hex_hash=None,
                pointer={},
                att={},
                target_name="clip.mp4",
                status="NOT FOUND",
            ),
        ]
        path = tmp_path / "report.txt"
        write_media_report(refs, path)
        text = path.read_text(encoding="utf-8")

        assert "fileName" in text
        assert "contentType" in text
        assert "photo.jpg" in text
        assert "OK" in text
        assert "clip.mp4" in text
        assert "NOT FOUND" in text

    def test_empty_refs_writes_header_only(self, tmp_path: Path) -> None:
        path = tmp_path / "report.txt"
        write_media_report([], path)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # header + separator

    def test_with_contacts_shows_resolved_name(self, tmp_path: Path) -> None:
        contacts = {"42": "Alice Smith"}
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={},
            att={},
            target_name="photo.jpg",
            status="OK",
            author_id="42",
        )
        path = tmp_path / "report.txt"
        write_media_report([ref], path, contacts=contacts)
        text = path.read_text(encoding="utf-8")
        assert "Alice Smith" in text
        assert "author" in text

    def test_with_contacts_fallback_to_raw_id(self, tmp_path: Path) -> None:
        contacts: dict[str, str] = {}
        ref = MediaRef(
            content_type="image/jpeg",
            hex_hash=None,
            pointer={},
            att={},
            target_name="photo.jpg",
            status="OK",
            author_id="99",
        )
        path = tmp_path / "report.txt"
        write_media_report([ref], path, contacts=contacts)
        text = path.read_text(encoding="utf-8")
        assert "99" in text


# ── _parse_date ───────────────────────────────────────────────────────────────


class TestParseDate:
    def test_valid_date(self) -> None:
        dt = _parse_date("2026-01-15")
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo == UTC

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_date("15-01-2026")

    def test_non_date_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_date("not-a-date")


# ── _load_contacts ────────────────────────────────────────────────────────────


class TestLoadContacts:
    def _write_contacts(self, tmp_path: Path, data: dict) -> Path:  # type: ignore[type-arg]
        path = tmp_path / "contacts.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_extracts_names_from_chats_and_authors(self, tmp_path: Path) -> None:
        # Format matches dump_contacts.py output: dict keyed by ID
        data = {
            "chats": {"1": {"name": "Alice", "message_count": 10}},
            "authors": {"2": {"name": "Bob", "message_count": 5}},
        }
        contacts = _load_contacts(self._write_contacts(tmp_path, data))
        assert contacts == {"1": "Alice", "2": "Bob"}

    def test_skips_empty_names(self, tmp_path: Path) -> None:
        data = {
            "chats": {"1": {"name": ""}, "2": {"name": "Carol"}},
            "authors": {},
        }
        contacts = _load_contacts(self._write_contacts(tmp_path, data))
        assert "1" not in contacts
        assert contacts["2"] == "Carol"

    def test_missing_sections_tolerated(self, tmp_path: Path) -> None:
        data: dict = {}  # type: ignore[type-arg]
        contacts = _load_contacts(self._write_contacts(tmp_path, data))
        assert contacts == {}

    def test_only_authors_section(self, tmp_path: Path) -> None:
        data = {"authors": {"5": {"name": "Dave"}}}
        contacts = _load_contacts(self._write_contacts(tmp_path, data))
        assert contacts == {"5": "Dave"}
