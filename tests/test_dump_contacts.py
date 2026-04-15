"""Unit tests for dump_contacts."""

from dump_contacts import _ms_to_iso, merge_with_existing, scan_records

# ── _ms_to_iso ────────────────────────────────────────────────────────────────


class TestMsToIso:
    def test_known_timestamp(self) -> None:
        # 2026-01-15 00:00:00 UTC = 1768435200000 ms
        assert _ms_to_iso(1_768_435_200_000) == "2026-01-15"

    def test_epoch_zero(self) -> None:
        assert _ms_to_iso(0) == "1970-01-01"

    def test_returns_string(self) -> None:
        result = _ms_to_iso(1_000_000_000_000)
        assert isinstance(result, str)
        assert len(result) == 10  # YYYY-MM-DD


# ── scan_records ──────────────────────────────────────────────────────────────


def _make_record(
    chat_id: str = "1",
    author_id: str = "10",
    date_sent: int = 1_000_000,
) -> dict:  # type: ignore[type-arg]
    return {
        "chatItem": {
            "chatId": chat_id,
            "authorId": author_id,
            "dateSent": str(date_sent),
        }
    }


class TestScanRecords:
    def test_collects_unique_chat_ids(self) -> None:
        records = [_make_record(chat_id="1"), _make_record(chat_id="2"), _make_record(chat_id="1")]
        chats, _ = scan_records(records)
        assert set(chats.keys()) == {"1", "2"}

    def test_collects_unique_author_ids(self) -> None:
        records = [_make_record(author_id="10"), _make_record(author_id="20")]
        _, authors = scan_records(records)
        assert set(authors.keys()) == {"10", "20"}

    def test_message_count(self) -> None:
        records = [_make_record(chat_id="1")] * 3
        chats, _ = scan_records(records)
        assert chats["1"]["message_count"] == 3

    def test_first_and_last_message_dates(self) -> None:
        records = [
            _make_record(date_sent=1_000_000_000),
            _make_record(date_sent=2_000_000_000),
            _make_record(date_sent=1_500_000_000),
        ]
        chats, _ = scan_records(records)
        assert chats["1"]["first_message"] == _ms_to_iso(1_000_000_000)
        assert chats["1"]["last_message"] == _ms_to_iso(2_000_000_000)

    def test_empty_chat_id_skipped(self) -> None:
        records = [{"chatItem": {"chatId": "", "authorId": "10", "dateSent": "1000"}}]
        chats, _ = scan_records(records)
        assert chats == {}

    def test_empty_author_id_skipped(self) -> None:
        records = [{"chatItem": {"chatId": "1", "authorId": "", "dateSent": "1000"}}]
        _, authors = scan_records(records)
        assert authors == {}

    def test_empty_records(self) -> None:
        chats, authors = scan_records([])
        assert chats == {}
        assert authors == {}

    def test_output_has_no_raw_ms_fields(self) -> None:
        records = [_make_record()]
        chats, authors = scan_records(records)
        for mapping in (chats, authors):
            for entry in mapping.values():
                assert "first_message_ms" not in entry
                assert "last_message_ms" not in entry
                assert "first_message" in entry
                assert "last_message" in entry

    def test_record_without_chatItem_ignored(self) -> None:
        records: list[dict] = [{"something_else": {}}]  # type: ignore[type-arg]
        chats, authors = scan_records(records)
        assert chats == {}
        assert authors == {}


# ── merge_with_existing ───────────────────────────────────────────────────────


class TestMergeWithExisting:
    def test_new_ids_added(self) -> None:
        new = {
            "1": {
                "name": "",
                "message_count": 5,
                "first_message": "2026-01-01",
                "last_message": "2026-01-10",
            }
        }
        result = merge_with_existing(new, {})
        assert "1" in result

    def test_existing_name_preserved(self) -> None:
        new = {
            "1": {
                "name": "",
                "message_count": 10,
                "first_message": "2026-01-01",
                "last_message": "2026-01-10",
            }
        }
        existing = {
            "1": {
                "name": "Alice",
                "message_count": 5,
                "first_message": "2026-01-01",
                "last_message": "2026-01-05",
            }
        }
        result = merge_with_existing(new, existing)
        assert result["1"]["name"] == "Alice"

    def test_empty_existing_name_not_overwritten_by_empty(self) -> None:
        new = {
            "1": {
                "name": "",
                "message_count": 1,
                "first_message": "2026-01-01",
                "last_message": "2026-01-01",
            }
        }
        existing = {
            "1": {
                "name": "",
                "message_count": 1,
                "first_message": "2026-01-01",
                "last_message": "2026-01-01",
            }
        }
        result = merge_with_existing(new, existing)
        assert result["1"]["name"] == ""

    def test_id_only_in_existing_is_kept(self) -> None:
        existing = {
            "99": {
                "name": "Old Chat",
                "message_count": 1,
                "first_message": "2025-01-01",
                "last_message": "2025-01-01",
            }
        }
        result = merge_with_existing({}, existing)
        assert "99" in result

    def test_numeric_ids_sorted(self) -> None:
        new: dict[str, dict] = {"10": {}, "2": {}, "1": {}}  # type: ignore[type-arg]
        result = merge_with_existing(new, {})
        assert list(result.keys()) == ["1", "2", "10"]

    def test_stats_come_from_new_scan(self) -> None:
        new = {
            "1": {
                "name": "",
                "message_count": 99,
                "first_message": "2026-01-01",
                "last_message": "2026-04-01",
            }
        }
        existing = {
            "1": {
                "name": "Bob",
                "message_count": 10,
                "first_message": "2025-01-01",
                "last_message": "2025-12-31",
            }
        }
        result = merge_with_existing(new, existing)
        assert result["1"]["message_count"] == 99
        assert result["1"]["name"] == "Bob"
