# signal-backup-extract

Two small scripts for working with a **Signal Desktop plaintext backup export**
(`main.jsonl` + `files/`).

| Script | Purpose |
|---|---|
| `signal_backup_extract.py` | Filter messages by chat, date range, author, direction, text, and media — then copy referenced attachments |
| `dump_contacts.py` | Scan all chat/author IDs from the export and produce a `contacts.json` you fill in by hand |

---

## Requirements

Python 3.11+. No third-party runtime dependencies — only the standard library.

```bash
# dev tooling (linting, tests, pre-commit hooks)
pip install -r requirements-dev.txt
pre-commit install
```

---

## Typical workflow

### Step 1 — find your chat IDs

The export contains only numeric IDs, not names. Run `dump_contacts.py` first:

```
python dump_contacts.py C:\Exports\signal-export\
```

This writes `C:\Exports\signal-export\contacts.json`. Open it — it lists every
chat and author with message counts and date ranges:

```json
{
  "chats": {
    "193": { "name": "", "message_count": 412, "first_message": "2024-11-03", "last_message": "2026-04-07" },
    "204": { "name": "", "message_count": 87,  "first_message": "2025-01-10", "last_message": "2026-03-15" }
  },
  "authors": {
    "159": { "name": "", "message_count": 198, "first_message": "2024-11-03", "last_message": "2026-04-07" }
  }
}
```

Fill in the `"name"` fields by hand so you know which ID is which. Re-running
`dump_contacts.py` is safe — your edits are preserved.

### Step 2 — extract a chat

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ --chat-id 193
```

The output folder receives:

```
output\
    filtered.jsonl      one JSON object per matching message
    media\              copies of all referenced attachments
    media_list.txt      per-attachment status report (OK / NOT FOUND)
```

---

## signal_backup_extract.py — all options

### Minimal

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ --chat-id 193
```

Extract all messages from chat 193, copy all media.

### Multiple chat IDs

Pass `--chat-id` more than once, or use comma-separated values:

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --chat-id 204

python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193,204
```

### Date range

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 ^
    --after 2026-01-01 ^
    --before 2026-04-07
```

Only messages sent on or after 2026-01-01 and on or before 2026-04-07 (UTC).
Either bound can be omitted.

### Only received or sent messages

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --incoming
```

Use `--outgoing` for the opposite. The two flags are mutually exclusive.

### Filter by author

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --author-id 159
```

### Filter by message text

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --text-contains "meeting notes"

python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --text-regex "\d{4}-\d{2}-\d{2}"
```

`--text-contains` does a plain substring match. `--text-regex` accepts a Python
regular expression; a bad pattern is rejected at startup. The two flags are
mutually exclusive.

### Filter by attachment presence or MIME type

```
# only messages that have at least one attachment
python signal_backup_extract.py ... --chat-id 193 --has-media

# only messages with no attachments
python signal_backup_extract.py ... --chat-id 193 --no-media

# only messages that contain an image/jpeg attachment
python signal_backup_extract.py ... --chat-id 193 --media-type image/jpeg
```

`--has-media` and `--no-media` are mutually exclusive. `--media-type` can be
combined with either.

### Output format

```
python signal_backup_extract.py ... --chat-id 193 --format json
```

Writes `filtered.json` (a pretty-printed JSON array) instead of the default
`filtered.jsonl` (one object per line). Both contain the same records.

### Control what gets written

```
# write filtered messages only — skip copying media
python signal_backup_extract.py ... --chat-id 193 --skip-media

# copy media only — skip writing filtered messages
python signal_backup_extract.py ... --chat-id 193 --media-only
```

`--skip-media` and `--media-only` are mutually exclusive.

### Rename media files with a timestamp prefix

```
python signal_backup_extract.py ... --chat-id 193 --rename-media timestamp
```

Prefixes each copied filename with `YYYYMMDD_HHMMSS_` (UTC send time), e.g.
`20260101_143022_signal-photo.jpeg`. Useful for chronological ordering in file
browsers.

### Show author names in the media report

```
python signal_backup_extract.py ... --chat-id 193 --contacts C:\Exports\signal-export\contacts.json
```

Loads a `contacts.json` produced by `dump_contacts.py` and resolves author IDs
to names in `media_list.txt`. IDs not found in the file are shown as-is.

### Dry run — check what would be extracted without writing files

```
python signal_backup_extract.py C:\Exports\signal-export\ C:\Exports\output\ ^
    --chat-id 193 --after 2026-01-01 --dry-run
```

Prints match counts and attachment names; nothing is written to disk.

### Verbose / quiet

```
python signal_backup_extract.py ... -v     # debug output
python signal_backup_extract.py ... -q     # suppress all output
```

### Full option reference

```
positional arguments:
  source              export folder (contains main.jsonl and files/)
  target              output folder (created if absent)

required:
  --chat-id ID        chat ID to extract; repeatable, comma-separated values accepted

date filter:
  --after  YYYY-MM-DD   include messages sent on or after this date (UTC)
  --before YYYY-MM-DD   include messages sent on or before this date (UTC)

message filter:
  --author-id ID        only include messages from this author
  --incoming            only include received messages  }  mutually
  --outgoing            only include sent messages      }  exclusive
  --text-contains STR   only include messages whose body contains STR  }  mutually
  --text-regex PATTERN  only include messages whose body matches PATTERN}  exclusive
  --has-media           only include messages with attachments    }  mutually
  --no-media            only include messages without attachments }  exclusive
  --media-type MIME     only include messages with an attachment of this MIME type

output:
  --format jsonl|json   output format for filtered messages (default: jsonl)
  --skip-media          write filtered messages only; skip media copy  }  mutually
  --media-only          copy media only; skip writing filtered messages}  exclusive
  --rename-media SCHEME prefix copied filenames (only scheme: timestamp)
  --contacts FILE       contacts.json for author name resolution in media report
  --dry-run             report matches without writing any files

verbosity:
  -v / --verbose
  -q / --quiet
```

### Media copy

For each attachment the script:

1. Derives the on-disk filename using `SHA-256(plaintextHash || localKey)` (see [Design decisions](#design-decisions))
2. Locates the file under `files/<first-2-hex>/`
3. Copies it to `media\` under its original filename (or a sanitized fallback)
4. Records the outcome in `media_list.txt`

`NOT FOUND` entries were referenced in the JSONL but absent from `files/` —
typically because the attachment was never downloaded before the export was taken.

---

## dump_contacts.py — all options

### Write contacts.json into the export folder (default)

```
python dump_contacts.py C:\Exports\signal-export\
```

### Write to a custom location

```
python dump_contacts.py C:\Exports\signal-export\ --output C:\Users\me\signal-contacts.json
```

### Re-run after receiving new messages (names are preserved)

```
python dump_contacts.py C:\Exports\signal-export-new\
```

Any `"name"` values already in `contacts.json` are kept; new IDs are appended
with empty names.

### Full option reference

```
positional arguments:
  source          export folder (contains main.jsonl)

options:
  --output FILE   output path (default: <source>/contacts.json)
  -v / --verbose
  -q / --quiet
```

---

## Design decisions

### Attachment filename derivation

Signal Desktop's export does not document how attachment filenames are derived.
The scheme was reverse-engineered by matching file sizes between the export
JSON and the on-disk `files/` directory:

```
filename_stem = SHA-256( plaintextHash_bytes || localKey_bytes )
```

Both values come from `chatItem.standardMessage.attachments[].pointer.locatorInfo`
and are base64-encoded in the JSON. The files in `files/` are already decrypted
plaintext — not encrypted blobs.

### IDs, not names

The Signal Desktop export contains only numeric IDs for chats and authors.
Names live in Signal's internal SQLite database (`db.sqlite`), which is
encrypted with a key protected by the OS keychain (Windows DPAPI + AES-GCM).
The export deliberately omits names, presumably for privacy.

**There is no automatic way to map a chat ID to a contact name using the export
alone.** `dump_contacts.py` collects all IDs and produces a template you fill
in manually. Pass the filled-in file to `--contacts` to show names in the
media report.

### No schema documentation

Signal does not publish a schema for `main.jsonl`. The field paths used here
(`chatItem.chatId`, `chatItem.dateSent`, `locatorInfo.plaintextHash`, etc.)
were inferred by inspection and may change across Signal Desktop versions.

### Single-file design

Both scripts are intentionally self-contained single files with no runtime
dependencies beyond the Python standard library. This makes them easy to
copy, audit, and run on any machine with Python 3.11+.

---

## Limitations

- **No name resolution at filter time** — chat and author IDs must be looked up manually via `dump_contacts.py`; `--contacts` resolves names only in the media report.
- **No schema guarantee** — the JSONL format is internal and undocumented; field paths may change in future Signal Desktop versions.
- **Media gaps** — attachments not downloaded before the export was taken will be missing from `files/` and show as `NOT FOUND`.
- **Date filter is on `dateSent`** — server-assigned send time in millisecond epoch UTC. `dateReceived` is not used for filtering.
- **Windows filename sanitization** — illegal characters (`\ / : * ? " < > | +`) in attachment names are replaced with `_`. Duplicate filenames within one run are not deduplicated.

---

## Export format reference

```
<export_folder>/
    main.jsonl          one JSON object per line, each a full message record
    files/
        00/             2-hex-char prefix sharding (256 buckets)
            <hash>.<ext>
        ...
        ff/
    metadata.json       {"version": 1}
```

Minimal message record structure:

```jsonc
{
  "chatItem": {
    "chatId": "193",
    "authorId": "159",
    "dateSent": "1712345678000",
    "incoming": { "dateReceived": "1712345679000", "read": true },
    "standardMessage": {
      "text": { "body": "..." },
      "attachments": [
        {
          "pointer": {
            "contentType": "image/jpeg",
            "fileName": "signal-2026-04-12.jpeg",
            "locatorInfo": {
              "localKey": "<base64>",
              "plaintextHash": "<base64>",
              "size": 1071441,
              "transitCdnKey": "<string>"
            }
          },
          "wasDownloaded": true,
          "clientUuid": "<base64>"
        }
      ]
    }
  }
}
```
