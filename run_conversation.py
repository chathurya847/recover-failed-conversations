#!/usr/bin/env python3
"""Re-run failed sessions one by one through the chat API and keep a record of every request.

Usage:
    python run_conversation.py <id> [<id> ...]             each id is a session id or a conversation id
    python run_conversation.py conversation_id.csv         a .csv file listing the ids (session or conversation ids)
    python run_conversation.py --csv [session_ids.csv]     same, with the default file name session_ids.csv
    python run_conversation.py --all                       run every session in the data file
    python run_conversation.py <...> --dry-run             only show what would be sent (no network, no records)

The data file is the JSON export named by DATA_FILE_PATH in .env (default: data.json next to this script). Each
entry is a conversation with a "sessions" list or a single "session" object, or (session-data.json) a session
document itself with "conversation_id" and "user_input". For that flat shape the attachments are the files in
downloads/<conversation_id>/, and the "Attachments:" list at the end of the user_input says how many are expected.

For every id the script finds the session in the data file:
  - a SESSION id        -> that session
  - a CONVERSATION id   -> the LAST session of that conversation (a warning is printed if it has several), and it
                           must be failed, otherwise it is recorded as Failed and nothing is sent
Then it checks whether the session has attachments (a non-empty "session_attachment_list"):
  - with attachments    -> they are taken from downloads/<conversation_id>/ (run
                           download_attachments.py first; if they are missing the session is recorded as Failed and
                           nothing is sent), uploaded, and sent together with the user_input
  - without attachments -> only the user_input is sent

What is called on the server:
  0. GET  /api/conversation/{id}/title        -> checks that the conversation exists on the server
  1. POST /api/upload/session/initialize       -> creates an upload session          (only with attachments)
  2. POST /api/upload/session/{id}/files       -> uploads the downloaded attachments (only with attachments)
  3. POST /api/conversation                    -> sends the user_input (with uploads=[{upload_id, files}] if any)
Items are sent one after another, in the order given, DELAY_TIME seconds apart (from .env, default 60,
counted from the moment the previous message was sent). The script does not wait for the server to finish
processing: as soon as the server accepts a request (HTTP 200) it is recorded as "Send" and the stream is closed;
the processing continues on the server. Use --wait to read every stream until the server has finished instead.

The message is sent into the EXISTING conversation (same id as in the data file). If that conversation does not
exist on the server (for example a dev server that does not have production data) the script stops for that
item, unless you pass --create-missing: then a new conversation is created with POST /api/conversation/create
(sessions with attachments only). The old id -> new id mapping is kept per server in conversation_map.<host>.json,
and later sessions of the same conversation (for example follow-up messages without attachments) are sent into
that new conversation. Run a conversation's first session first.

records.csv (next to this script) is created on the first run and updated after every request. It holds one row
per session, with the latest result of that session:
    Session_id, Conversation_id, Status (Send / Failed), Requested_Time, Requested_Date
Status is "Send" once the server accepted the request, and "Failed" for any error before that (authentication,
conversation not found, attachments missing, upload error, HTTP error, ...). The reason is printed on the console.
A session that is already "Send" in records.csv is skipped, so it is never sent twice by accident; use --force to
send it again.

CSV files: one id per row (a header row such as "Session_id" or "Conversation_id" is ignored; ids may also be
separated by commas).

Options:
    --csv [PATH]          read the ids from a CSV file (default: session_ids.csv)
    --dry-run             print what would be sent for every id and stop; no network request, no records written
    --force               send again even if records.csv already says "Send"
    --wait                read each response stream until the server has finished processing before moving on
    --session-type TYPE   override the session type (default: the session's own "type", e.g. complex_analysis)
    --conversation ID     send the session into this specific existing conversation on the server instead
                          (for example one created in the UI)
    --create-missing      create a new conversation when the conversation does not exist on the server

Settings are read from .env next to this script (nothing is hard-coded for a specific system):
    BASE_URL=https://mercurydev.chat.layernext.ai
    authenticationToken=<token>
    ACCOUNTS_DOMAIN_URL=https://accounts-dev.layernext.ai
                                 accounts (login) service of that same system; the script stops early if the
                                 token was issued by a different one (for example a dev token on production)
    DATA_FILE_PATH=D:\\path\\to\\data.json
                                 optional, the JSON export to use (default: data.json next to this script)
    DELAY_TIME=60                optional, seconds to wait between requests (default 60)
    SESSION_TYPE=                optional, forces one session type (default: each session's own type)
    READ_TIMEOUT_SECONDS=900     optional, how long to wait for the server to send data

The server's answer is saved to runs/<session_id>.log (the full stream only with --wait).
Requires: pip install requests
"""

import argparse
import csv
import json
import mimetypes
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from auth_utils import check_token, clean_token

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
RUNS_DIR = BASE_DIR / "runs"
SESSION_IDS_FILE = BASE_DIR / "session_ids.csv"
RECORDS_FILE = BASE_DIR / "records.csv"
RECORD_HEADERS = ["Session_id", "Conversation_id", "Status", "Requested_Time", "Requested_Date"]
DEFAULT_DELAY_SECONDS = 60


def load_env(path):
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


ENV = load_env(BASE_DIR / ".env")
ROLE = "user"  # required by the request model but only logged by the backend; identity and role come from the token
SESSION_TYPE = ENV.get("SESSION_TYPE") or ""  # empty = use each session's own type from the data file
READ_TIMEOUT = int(ENV.get("READ_TIMEOUT_SECONDS") or 900)  # seconds without any data from the server before giving up

DATA_FILE = Path(ENV.get("DATA_FILE_PATH") or "data.json")
if not DATA_FILE.is_absolute():
    DATA_FILE = BASE_DIR / DATA_FILE


def get_delay():
    raw = (ENV.get("DELAY_TIME") or "").strip()
    if not raw:
        return DEFAULT_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"Note: DELAY_TIME '{raw}' in .env is not a number, using {DEFAULT_DELAY_SECONDS} seconds.")
        return DEFAULT_DELAY_SECONDS


def plain_id(value):
    if isinstance(value, dict):
        value = value.get("$oid", next(iter(value.values()), ""))
    return str(value)


def listed_attachment_names(text):
    """File names from the "Attachments:" block that email-created user_input ends with, e.g. "- invoice.pdf"."""
    names, in_block = [], False
    for line in (text or "").splitlines():
        if line.strip() == "Attachments:":
            in_block = True
        elif in_block and line.startswith("- "):
            names.append(line[2:].strip())
        elif in_block:
            break
    return names


def read_entry(entry):
    """Return (conversation_id, [sessions], listed_from_text) for one entry of the data file.

    Every JSON shape the data file can have is handled here, so a new shape means a new branch:
      - flat session (session-data.json): the entry is a session with its own "conversation_id" and
        "user_input". It has no "session_attachment_list", so the expected attachment count is read from the
        "Attachments:" list in the user_input (listed_from_text=True).
      - conversation with "sessions" (data.json) or a single "session" (reply-data.json): the original shapes,
        unchanged; the expected count comes from "session_attachment_list".
    """
    if "sessions" not in entry and "session" not in entry and "conversation_id" in entry and "user_input" in entry:
        return plain_id(entry["conversation_id"]), [entry], True
    session_list = entry.get("sessions") or ([entry["session"]] if entry.get("session") else [])
    return plain_id(entry.get("_id")), session_list, False


def load_sessions():
    """Return {session_id: item} in data file order.

    An item has the session's user_input and, when the session lists attachments, the files found for it in
    downloads/<conversation_id>/. If attachments are listed but not all are found, the item gets an
    "error" so that nothing is sent without them.
    """
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    sessions = {}
    for entry in data if isinstance(data, list) else [data]:
        conversation_id, session_list, listed_from_text = read_entry(entry)
        for session in session_list:
            session_id = plain_id(session.get("_id"))
            if listed_from_text:
                listed = len(listed_attachment_names(session.get("user_input")))
            else:
                listed = len(session.get("session_attachment_list") or [])
            folder = DOWNLOADS_DIR / conversation_id
            files = []
            if listed and folder.is_dir():
                files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".part"))
            item = {
                "session_id": session_id,
                "conversation_id": conversation_id,
                "content": session.get("user_input") or "",
                "type": session.get("type") or "analysis",
                "status": session.get("status") or "failed",
                "files": files,
            }
            if listed and len(files) < listed:
                item["error"] = (
                    f"{listed} attachment(s) are listed for this session but {len(files)} found in {folder}; "
                    "run download_attachments.py first"
                )
            elif not listed and not item["content"].strip():
                item["error"] = "the session has no attachments and no user_input, so there is nothing to send"
            sessions[session_id] = item
    return sessions


def resolve_item(run_id, sessions, sessions_by_conversation):
    """A session id gives that session; a conversation id gives its last session (which must be failed)."""
    if run_id in sessions:
        return sessions[run_id]
    if run_id in sessions_by_conversation:
        found = sessions_by_conversation[run_id]
        item = found[-1]
        if len(found) > 1:
            print(f"Note: conversation {run_id} has {len(found)} sessions in {DATA_FILE.name}; only the last is used.")
        if item["status"] != "failed" and not item.get("error"):
            return dict(item, error=f"the last session of this conversation is '{item['status']}', not failed")
        return item
    return {
        "session_id": run_id,
        "conversation_id": "",
        "content": "",
        "type": "",
        "files": [],
        "error": f"this id is not a session or conversation in {DATA_FILE.name}",
    }


def find_csv(value):
    """A CSV path as given, or next to this script."""
    for candidate in (Path(value), BASE_DIR / value):
        if candidate.is_file():
            return candidate
    return None


def read_session_ids(path):
    """Read ids from a CSV: one per row or comma separated; header cells like 'Session_id' are ignored."""
    headers = ("sessionid", "sessionids", "conversationid", "conversationids", "id", "ids")
    ids = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            for cell in row:
                value = cell.strip().strip("'\"")
                if value and value.lower().replace("_", "").replace(" ", "") not in headers:
                    ids.append(value)
    return list(dict.fromkeys(ids))  # drop duplicates, keep order


def load_records():
    """records.csv as {session_id: row}, in file order."""
    if not RECORDS_FILE.exists():
        return {}
    with open(RECORDS_FILE, newline="", encoding="utf-8-sig") as handle:
        return {row["Session_id"]: row for row in csv.DictReader(handle) if row.get("Session_id")}


def save_records(records):
    temp = RECORDS_FILE.with_name(RECORDS_FILE.name + ".tmp")
    with open(temp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECORD_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records.values())
    temp.replace(RECORDS_FILE)


def update_record(records, item, status):
    """Set the session's row in records.csv (created on the first call) and write the file."""
    requested = item["requested_at"]
    records[item["session_id"]] = {
        "Session_id": item["session_id"],
        "Conversation_id": item.get("target_id") or item["conversation_id"],
        "Status": status,
        "Requested_Time": requested.strftime("%H:%M:%S"),
        "Requested_Date": requested.strftime("%Y-%m-%d"),
    }
    try:
        save_records(records)
    except OSError as error:
        print(f"  Warning: could not update {RECORDS_FILE.name} ({error}). Close it if it is open in another program.")


def map_file(base_url):
    """One mapping file per server, so ids from one environment are never used on another."""
    host = urlparse(base_url).netloc.replace(":", "_")
    return BASE_DIR / f"conversation_map.{host}.json"


def load_map(base_url):
    path = map_file(base_url)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_map(base_url, mapping):
    map_file(base_url).write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def post_json(base_url, headers, path, body):
    response = requests.post(f"{base_url}{path}", headers=headers, json=body, timeout=120)
    if not response.ok:
        raise RuntimeError(f"{path} -> HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def conversation_exists(base_url, headers, conversation_id):
    """True if the conversation id exists on the server (the title endpoint answers 406 for unknown ids)."""
    response = requests.get(f"{base_url}/api/conversation/{conversation_id}/title", headers=headers, timeout=60)
    if response.status_code == 200:
        return True
    if response.status_code == 406:
        return False
    raise RuntimeError(f"checking conversation {conversation_id} -> HTTP {response.status_code}: {response.text[:300]}")


def create_conversation(base_url, headers):
    """An empty parent_conversation_id makes the backend create a brand-new conversation."""
    result = post_json(base_url, headers, "/api/conversation/create", {"parent_conversation_id": ""})
    return result["conversationId"]


def upload_files(base_url, headers, files, description):
    """Create an upload session and upload the files into it. Returns the 'uploads' payload."""
    session = post_json(base_url, headers, "/api/upload/session/initialize", {"description": description})
    upload_id = session["upload_id"]

    handles = [open(path, "rb") for path in files]
    try:
        multipart = [
            ("files", (path.name, handle, mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
            for path, handle in zip(files, handles)
        ]
        response = requests.post(f"{base_url}/api/upload/session/{upload_id}/files", headers=headers, files=multipart, timeout=300)
    finally:
        for handle in handles:
            handle.close()
    if not response.ok:
        raise RuntimeError(f"/api/upload/session/{upload_id}/files -> HTTP {response.status_code}: {response.text[:300]}")

    uploaded = (response.json().get("upload_info") or {}).get("files") or [p.name for p in files]
    print(f"  uploaded {len(uploaded)} file(s) to upload session {upload_id}: {', '.join(uploaded)}")
    return [{"upload_id": upload_id, "files": uploaded}]


def run_session(base_url, headers, item, session_type, mapping, create_missing, on_sent, not_before, wait_for_stream):
    """Send one session. Calls on_sent() as soon as the server has accepted the request.

    The message request is not sent before the time.monotonic() value not_before (this is how DELAY_TIME spaces the
    requests). Unless wait_for_stream is set, the response stream is closed right after the server accepted the
    request; the processing continues on the server.
    """
    old_id = item["conversation_id"]
    target_id = mapping.get(old_id, "")

    if not target_id:
        if conversation_exists(base_url, headers, old_id):
            target_id = old_id
            print(f"  using the existing conversation {old_id}")
        elif not create_missing:
            raise RuntimeError(
                f"conversation {old_id} does not exist on this server. Pass --create-missing to create a new "
                "conversation for it instead"
            )
        elif item["files"]:
            target_id = create_conversation(base_url, headers)
            mapping[old_id] = target_id
            save_map(base_url, mapping)
            print(f"  created new conversation {target_id} for {old_id}")
        else:
            raise RuntimeError(
                f"no conversation exists yet on the server for {old_id}; run the first session of that conversation "
                "(the one with attachments) first"
            )
    item["target_id"] = target_id

    uploads = upload_files(base_url, headers, item["files"], f"Recovered attachments for conversation {target_id}") if item["files"] else []

    # The backend expects a JSON body here (a multipart form returns 422).
    body = {
        "id": target_id,
        "content": item["content"],
        "role": ROLE,
        "session_type": session_type or SESSION_TYPE or item["type"],
        "isFileUpload": bool(uploads),
        "uploads": uploads,
    }

    RUNS_DIR.mkdir(exist_ok=True)
    log_path = RUNS_DIR / f"{item['session_id']}.log"

    remaining = not_before - time.monotonic()
    if remaining > 0:
        print(f"  waiting {remaining:.0f}s (DELAY_TIME) before sending...")
        time.sleep(remaining)
    item["requested_at"] = datetime.now()  # the time the request is actually sent
    started = time.time()
    print(f"  sending message to conversation {target_id}")

    with requests.post(
        f"{base_url}/api/conversation", headers=headers, json=body, stream=True, timeout=(30, READ_TIMEOUT)
    ) as response:
        print(f"  HTTP {response.status_code} {response.reason}")
        ok = response.ok
        if ok:
            item["sent"] = True
            item["sent_at"] = time.monotonic()
            on_sent()
        with open(log_path, "w", encoding="utf-8") as log:
            if ok and not wait_for_stream:
                note = "accepted by the server; the stream was closed here and the processing continues on the server"
                log.write(f"HTTP {response.status_code}: {note}\n")
                print(f"  Accepted: {note}. Log: {log_path}")
                return True
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                log.write(line + "\n")
                log.flush()
                print("    " + (line if len(line) <= 300 else line[:300] + "..."))

    print(f"  {'Finished' if ok else 'Failed'} in {time.time() - started:.1f}s. Full output: {log_path}")
    return ok


def describe(item):
    return f"{len(item['files'])} attachment(s)" if item["files"] else "text only"


def dry_run(queue, session_type):
    """Show what would be sent. Makes no network request and writes nothing."""
    records = load_records()
    print(f"Dry run: nothing is sent and nothing is written. {len(queue)} item(s).\n")
    for number, item in enumerate(queue, 1):
        print(f"[{number}/{len(queue)}] session {item['session_id']} (conversation {item['conversation_id'] or '?'}, {describe(item)})")
        previous = records.get(item["session_id"])
        if item.get("error"):
            print(f"  would be recorded as Failed: {item['error']}")
        elif previous and previous.get("Status") == "Send":
            print(f"  would be skipped: already sent on {previous.get('Requested_Date')} {previous.get('Requested_Time')}")
        else:
            for path in item["files"]:
                print(f"  attachment: {path.name}")
            text = " ".join(item["content"].split())
            shown = text if len(text) <= 150 else text[:150] + "..."
            print(f"  would send ({session_type or SESSION_TYPE or item['type']}): {shown!r}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Re-run failed sessions one by one.")
    parser.add_argument("ids", nargs="*", help="session ids or conversation ids, or .csv files that list them")
    parser.add_argument("--all", action="store_true", help="run every session in the data file")
    parser.add_argument(
        "--csv",
        nargs="?",
        const=str(SESSION_IDS_FILE),
        metavar="PATH",
        help="read the ids from a CSV file (default: session_ids.csv next to this script)",
    )
    parser.add_argument("--dry-run", action="store_true", help="only print what would be sent; no network request, nothing written")
    parser.add_argument("--force", action="store_true", help='send again even if records.csv already says "Send"')
    parser.add_argument(
        "--wait",
        action="store_true",
        help="read each response stream until the server has finished processing before moving on "
        "(default: close the stream as soon as the server accepted the request)",
    )
    parser.add_argument("--session-type", help="override the session type")
    parser.add_argument(
        "--conversation",
        help="send the session into this EXISTING conversation id on the server (for example one created in the UI) "
        "instead of creating a new conversation",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="when a conversation id from the data file does not exist on the server (for example a dev server), "
        "create a new conversation instead of stopping",
    )
    args = parser.parse_args()
    if not (args.ids or args.all or args.csv):
        parser.print_help()
        return 2

    if not DATA_FILE.is_file():
        print(f"Data file not found: {DATA_FILE}. Set DATA_FILE_PATH in .env.")
        return 2
    sessions = load_sessions()
    sessions_by_conversation = {}
    for session_item in sessions.values():
        sessions_by_conversation.setdefault(session_item["conversation_id"], []).append(session_item)

    if args.csv:
        csv_path = find_csv(args.csv)
        if not csv_path:
            print(f"CSV file not found: {args.csv}")
            return 2
        ids = read_session_ids(csv_path)
        print(f"Read {len(ids)} id(s) from {csv_path.name}")
    elif args.all:
        ids = list(sessions)
    else:
        ids = []
        for value in args.ids:
            if value.lower().endswith(".csv"):
                csv_path = find_csv(value)
                if not csv_path:
                    print(f"CSV file not found: {value}")
                    return 2
                found = read_session_ids(csv_path)
                print(f"Read {len(found)} id(s) from {csv_path.name}")
                ids.extend(found)
            else:
                ids.append(value)
        ids = list(dict.fromkeys(ids))  # drop duplicates, keep order

    resolved = [resolve_item(run_id, sessions, sessions_by_conversation) for run_id in ids]
    queue = list({id(item): item for item in resolved}.values())  # an id and its conversation may point to one item

    if args.dry_run:
        return dry_run(queue, args.session_type)

    base_url = (ENV.get("BASE_URL") or "").rstrip("/")
    token = clean_token(ENV.get("authenticationToken", ""))
    if not base_url or not token:
        print("Missing BASE_URL or authenticationToken in .env")
        return 2
    delay = get_delay()

    mapping = load_map(base_url)
    if args.conversation:
        old_ids = {item["conversation_id"] for item in queue if not item.get("error")}
        if len(old_ids) != 1 or any(item.get("error") for item in queue):
            print("--conversation needs ids that all belong to one conversation in the data file")
            return 2
        mapping[old_ids.pop()] = args.conversation
        save_map(base_url, mapping)

    records = load_records()
    failures = skipped = sent = 0

    problem = check_token(token, ENV.get("ACCOUNTS_DOMAIN_URL", ""), "authenticationToken")
    if problem:
        print(f"Stopping before any request: {problem}")
        # Nothing was sent, so record every requested session as Failed (authentication problem).
        for item in queue:
            previous = records.get(item["session_id"])
            if previous and previous.get("Status") == "Send" and not args.force:
                continue  # keep the earlier successful record
            item["requested_at"] = datetime.now()
            update_record(records, item, "Failed")
        print(f"Recorded the requested session(s) as Failed in {RECORDS_FILE.name}.")
        return 2
    if not ENV.get("ACCOUNTS_DOMAIN_URL"):
        print("Note: ACCOUNTS_DOMAIN_URL is not set in .env, so the token was not checked against the system.")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {token}",
        "Cookie": f"authenticationToken={token}",
    }

    not_before = 0.0  # time.monotonic() value before which the next message must not be sent
    for number, item in enumerate(queue, 1):
        print(
            f"\n[{number}/{len(queue)}] session {item['session_id']} (conversation {item['conversation_id'] or '?'}, "
            f"{describe(item)}, content {len(item['content'])} chars)"
        )

        previous = records.get(item["session_id"])
        if previous and previous.get("Status") == "Send" and not args.force:
            print(
                f"  Skipped: already sent on {previous.get('Requested_Date')} {previous.get('Requested_Time')} "
                "(use --force to send it again)"
            )
            skipped += 1
            continue

        if item.get("error"):
            item["requested_at"] = datetime.now()
            update_record(records, item, "Failed")
            print(f"  Failed: {item['error']}")
            failures += 1
            continue

        item["requested_at"] = datetime.now()  # replaced by the real send time once the request goes out
        try:
            ok = run_session(
                base_url,
                headers,
                item,
                args.session_type,
                mapping,
                args.create_missing,
                on_sent=lambda item=item: update_record(records, item, "Send"),
                not_before=not_before,
                wait_for_stream=args.wait,
            )
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
            ok = False
            print(f"  Failed: {e}")

        if not item.get("sent"):
            update_record(records, item, "Failed")
        else:
            sent += 1
            not_before = item["sent_at"] + delay  # DELAY_TIME counts from the moment this request was sent
        if not ok:
            failures += 1

    print(f"\nDone. {sent} sent, {skipped} skipped, {failures} failed. Records: {RECORDS_FILE}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
