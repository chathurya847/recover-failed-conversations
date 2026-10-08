#!/usr/bin/env python3
"""Download the attachments of email-scrubbing emails from the Gmail inbox.

Mirrors how layernext-cms (EmailScrubbingService) reads mail: IMAP over imap.gmail.com using
GMAIL_USER_EMAIL / GMAIL_APP_PASSWORD. Processed mails may have been moved out of the INBOX to
EMAIL_CLEANUP_MAILBOX (the record has `archivedAt`), and an IMAP UID (the record's `emailId`) is only
valid inside the mailbox it was read from, so the lookup order is:

    1. messageId  -> searched in "All Mail" (finds the mail wherever it was moved), then in
                     INBOX and EMAIL_CLEANUP_MAILBOX as a fallback
    2. emailId    -> UID lookup in INBOX (only works while the mail was not archived)

Usage:
    python download_email_attachments.py <input> [output_dir]

<input> is a .csv or .json file. Each row needs a conversationId plus emailId and/or messageId:

    conversationId,emailId,messageId
    6abeb717a9ad0f1e727a4a7b,22144,<20261001193933.1532EC181DB@mail1.triadinet.net>

JSON may be a list of such objects, or the raw email-scrubbing records exported from MongoDB
(extended JSON like {"$oid": "..."} / {"$numberInt": "..."} is unwrapped).

Output layout:   <output_dir>/<conversationId>/<attachment file name>
A summary is written to <output_dir>/email_report.json. Existing identical files are skipped, so re-running is safe.
Mails are fetched with BODY.PEEK, so they are not marked as read.

.env (next to this script, same names as layernext-cms):
    GMAIL_USER_EMAIL=support@layernext.ai      (optional, same default as cms)
    GMAIL_APP_PASSWORD=<app password>
    EMAIL_CLEANUP_MAILBOX=Processed            (optional, same default as cms)
"""

import csv
import hashlib
import imaplib
import json
import os
import re
import sys
from email import message_from_bytes, policy
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def load_env(path):
    """Minimal .env reader: KEY=VALUE lines, optional quotes, '#' comments."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def plain(value):
    """Unwrap Mongo extended JSON ({"$oid": ..}, {"$numberInt": ..}) and shell wrappers to a string."""
    if isinstance(value, dict):
        value = next(iter(value.values()), "")
    value = str(value if value is not None else "").strip()
    match = re.fullmatch(r"(?:ObjectId|NumberInt|NumberLong)\(\s*['\"]?([^'\")]*)['\"]?\s*\)", value)
    return match.group(1) if match else value


def safe_name(name, fallback):
    name = INVALID_CHARS.sub("_", (name or "").strip()).strip(". ")
    return name or fallback


def normalize_message_id(value):
    return plain(value).strip().strip("<>").strip()


def read_rows(path):
    """Return a list of {conversationId, emailId, messageId} dicts from a CSV or JSON file."""
    raw = []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        raw = data if isinstance(data, list) else [data]
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f))

    rows = []
    for item in raw:
        lowered = {str(k).strip().lower(): v for k, v in item.items()}
        rows.append({
            "conversationId": plain(lowered.get("conversationid") or lowered.get("conversation_id")),
            "emailId": plain(lowered.get("emailid") or lowered.get("email_id")),
            "messageId": normalize_message_id(lowered.get("messageid") or lowered.get("message_id")),
        })
    return [r for r in rows if r["emailId"] or r["messageId"]]


def connect(env):
    user = env.get("GMAIL_USER_EMAIL") or os.environ.get("GMAIL_USER_EMAIL") or "support@layernext.ai"
    password = env.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        sys.exit("GMAIL_APP_PASSWORD is not set (put it in .env next to this script).")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(user, password.replace(" ", ""))
    print(f"Connected to {IMAP_HOST} as {user}\n")
    return conn


def find_all_mail_folder(conn):
    """Gmail's 'All Mail' folder has a locale-dependent name; locate it by its \\All flag."""
    status, lines = conn.list()
    if status == "OK":
        for line in lines:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\All" in text:
                match = re.search(r'"([^"]+)"\s*$', text)
                if match:
                    return match.group(1)
    return "[Gmail]/All Mail"


def quote(mailbox):
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def select(conn, mailbox):
    status, _ = conn.select(quote(mailbox), readonly=True)
    return status == "OK"


def fetch_uid(conn, uid):
    status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        return None
    for part in data:
        if isinstance(part, tuple):
            return part[1]
    return None


def find_by_message_id(conn, message_id, mailboxes):
    """Return (raw_bytes, mailbox) for the first mailbox containing the Message-ID."""
    for mailbox in mailboxes:
        if not select(conn, mailbox):
            continue
        searches = [("X-GM-RAW", f'"rfc822msgid:{message_id}"'), ("HEADER", "Message-ID", f'"<{message_id}>"')]
        for criteria in searches:
            try:
                status, data = conn.uid("SEARCH", *criteria)
            except imaplib.IMAP4.error:
                continue
            if status == "OK" and data and data[0]:
                raw = fetch_uid(conn, data[0].split()[0].decode())
                if raw:
                    return raw, mailbox
    return None, None


def find_by_uid(conn, uid):
    if not select(conn, "INBOX"):
        return None, None
    raw = fetch_uid(conn, uid)
    return (raw, "INBOX") if raw else (None, None)


def attachments_of(message):
    """Yield (filename, bytes) for each attached file, including attached .eml messages."""
    for index, part in enumerate(message.walk(), 1):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and part.get_content_type() == "message/rfc822":
            filename = f"attached_message_{index}.eml"
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:  # message/rfc822 and similar nested parts
            inner = part.get_payload()
            payload = inner[0].as_bytes() if inner else b""
        yield filename, payload


def save_attachment(folder, filename, payload):
    """Write the file, never overwriting a different file. Returns (path, status)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(safe_name(filename, "attachment")).stem, Path(safe_name(filename, "attachment")).suffix
    candidate, counter = folder / f"{stem}{suffix}", 1
    digest = hashlib.sha256(payload).hexdigest()
    while candidate.exists():
        if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            return candidate, "skipped"
        candidate = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    candidate.write_bytes(payload)
    return candidate, "downloaded"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    input_file = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE_DIR / "downloads"

    env = load_env(BASE_DIR / ".env")
    rows = read_rows(input_file)
    if not rows:
        print("No rows with emailId or messageId found in the input.")
        return 2

    conn = connect(env)
    all_mail = find_all_mail_folder(conn)
    cleanup_mailbox = (env.get("EMAIL_CLEANUP_MAILBOX") or os.environ.get("EMAIL_CLEANUP_MAILBOX") or "Processed").strip()
    mailboxes = [all_mail, "INBOX", cleanup_mailbox]

    report, counts = [], {"downloaded": 0, "skipped": 0, "no_attachments": 0, "not_found": 0}
    for row in rows:
        label = f"emailId={row['emailId'] or '-'} messageId=<{row['messageId'] or '-'}>"
        raw, mailbox = (None, None)
        if row["messageId"]:
            raw, mailbox = find_by_message_id(conn, row["messageId"], mailboxes)
        if raw is None and row["emailId"]:
            raw, mailbox = find_by_uid(conn, row["emailId"])
            if raw and row["messageId"]:
                found = message_from_bytes(raw, policy=policy.default).get("Message-ID", "")
                if normalize_message_id(found) != row["messageId"]:
                    raw = None  # UIDs are per-mailbox, so make sure it is really the same mail
        if raw is None:
            counts["not_found"] += 1
            report.append({**row, "status": "not_found"})
            print(f"[MISS] {label}: mail not found in {', '.join(mailboxes)}")
            continue

        message = message_from_bytes(raw, policy=policy.default)
        folder = output_dir / safe_name(row["conversationId"], "no_conversation_id")
        files = []
        for filename, payload in attachments_of(message):
            path, status = save_attachment(folder, filename, payload)
            counts[status] += 1
            files.append({"file": str(path), "status": status, "bytes": len(payload)})
            print(f"[{'OK' if status == 'downloaded' else 'SKIP':>4}] {path.relative_to(output_dir)} ({len(payload):,} bytes)")
        if not files:
            counts["no_attachments"] += 1
            print(f"[NONE] {label}: mail found in '{mailbox}' but has no attachments")
        report.append({**row, "mailbox": mailbox, "status": "ok" if files else "no_attachments", "files": files})

    try:
        conn.logout()
    except Exception:
        pass

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "email_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone. {counts['downloaded']} downloaded, {counts['skipped']} skipped, "
          f"{counts['no_attachments']} mails without attachments, {counts['not_found']} not found.")
    print(f"Output: {output_dir}")
    return 1 if counts["not_found"] else 0


if __name__ == "__main__":
    sys.exit(main())
