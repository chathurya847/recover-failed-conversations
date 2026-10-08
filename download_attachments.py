#!/usr/bin/env python3
"""Download attachments listed in a FailedConversationsWithAttachments export.

Usage:
    python download_attachments.py [data.json] [output_dir]

Output layout:
    <output_dir>/<conversation_id>/<file name>

Only the Python standard library is used. Files that already exist are skipped,
so the script can be re-run safely. A summary is written to <output_dir>/report.json.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from auth_utils import check_token, clean_token

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "data.json"
OUTPUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE_DIR / "downloads"

import os

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


# Authentication (the file URLs return 401 without it). Put this in .env next to the script:
#   sourceAuthenticationToken=<token>        token of the system the attachment URLs in data.json belong to
#   SOURCE_ACCOUNTS_DOMAIN_URL=<url>         accounts (login) service of that same system
# If sourceAuthenticationToken is empty, authenticationToken / ACCOUNTS_DOMAIN_URL are used instead.
# The token must come from the SAME system as the attachment URLs (a dev token is rejected by production);
# when the accounts URL is set, the script stops early if the token was issued by a different system.
# The token is sent both as "Authorization: Bearer <token>" and as an "authenticationToken" cookie.
# AUTH_TOKEN / AUTH_COOKIE environment variables still work.
_env = load_env(BASE_DIR / ".env")
if _env.get("sourceAuthenticationToken"):
    AUTH_TOKEN, AUTH_TOKEN_NAME = _env["sourceAuthenticationToken"], "sourceAuthenticationToken"
    ACCOUNTS_URL = _env.get("SOURCE_ACCOUNTS_DOMAIN_URL", "")
else:
    AUTH_TOKEN, AUTH_TOKEN_NAME = _env.get("authenticationToken") or os.environ.get("AUTH_TOKEN", ""), "authenticationToken"
    ACCOUNTS_URL = _env.get("ACCOUNTS_DOMAIN_URL", "")
AUTH_TOKEN = clean_token(AUTH_TOKEN)
AUTH_COOKIE = os.environ.get("AUTH_COOKIE", "").strip()

RETRIES = 3
TIMEOUT = 60
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def plain_id(value):
    """Return the bare id string, unwrapping Mongo extended JSON like {"$oid": "..."}."""
    if isinstance(value, dict):
        value = value.get("$oid", next(iter(value.values()), ""))
    return str(value)


def clean_url(raw):
    """Unwrap markdown links such as "[url](url)" that can appear in exports."""
    raw = (raw or "").strip()
    match = MARKDOWN_LINK.search(raw)
    if match:
        return match.group(1)
    return raw.strip("<>")


def safe_name(name, fallback):
    name = INVALID_CHARS.sub("_", (name or "").strip()).strip(". ")
    return name or fallback


def file_name_for(attachment, url, index):
    name = attachment.get("section_name")
    if not name:
        query = parse_qs(urlparse(url).query)
        name = unquote(query.get("file_name", [""])[0]) or Path(urlparse(url).path).name
    return safe_name(name, f"attachment_{index}")


def unique_path(path, used):
    """Avoid overwriting when one session has two attachments with the same name."""
    candidate, counter = path, 1
    while candidate in used:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    used.add(candidate)
    return candidate


def download(url, dest):
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            cookies = []
            if AUTH_TOKEN:
                headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
                cookies.append(f"authenticationToken={AUTH_TOKEN}")
            if AUTH_COOKIE:
                cookies.append(AUTH_COOKIE)
            if cookies:
                headers["Cookie"] = "; ".join(cookies)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = response.read()
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return len(data)
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code} {e.reason}"
            if 400 <= e.code < 500 and e.code != 429:
                break  # retrying will not help
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e)
        if attempt < RETRIES:
            time.sleep(2 * attempt)
    raise RuntimeError(last_error)


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        conversations = json.load(f)
    if isinstance(conversations, dict):
        conversations = [conversations]

    if not (AUTH_TOKEN or AUTH_COOKIE):
        print("Note: no sourceAuthenticationToken or authenticationToken found in .env; requests will be unauthenticated.\n")
    elif AUTH_TOKEN:
        problem = check_token(AUTH_TOKEN, ACCOUNTS_URL, AUTH_TOKEN_NAME)
        if problem:
            print(f"Stopping before any request: {problem}")
            return 2
        if not ACCOUNTS_URL:
            print("Note: no accounts URL set in .env, so the token was not checked against the system.\n")

    results = []
    counts ={"downloaded": 0, "skipped": 0, "failed": 0}

    for conversation in conversations:
        conversation_id = plain_id(conversation.get("_id"))
        folder = OUTPUT_DIR / safe_name(conversation_id, "unknown_conversation")
        used = set()  # shared by all sessions, since they now write into the same folder
        for session in conversation.get("sessions") or []:
            session_id = plain_id(session.get("_id"))

            for index, attachment in enumerate(session.get("session_attachment_list") or [], 1):
                url = clean_url(attachment.get("url"))
                record = {"conversation_id": conversation_id, "session_id": session_id, "url": url}

                if not url:
                    record.update(status="failed", error="missing url")
                    counts["failed"] += 1
                    results.append(record)
                    print(f"[FAIL] {conversation_id}/{session_id}: missing url")
                    continue

                dest = unique_path(folder / file_name_for(attachment, url, index), used)
                record["file"] = str(dest)

                if dest.exists() and dest.stat().st_size > 0:
                    record["status"] = "skipped"
                    counts["skipped"] += 1
                    print(f"[SKIP] {dest.relative_to(OUTPUT_DIR)} (already exists)")
                else:
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                        size = download(url, dest)
                        record.update(status="downloaded", bytes=size)
                        counts["downloaded"] += 1
                        print(f"[ OK ] {dest.relative_to(OUTPUT_DIR)} ({size:,} bytes)")
                    except Exception as e:
                        record.update(status="failed", error=str(e))
                        counts["failed"] += 1
                        print(f"[FAIL] {dest.relative_to(OUTPUT_DIR)}: {e}")
                results.append(record)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nDone. {counts['downloaded']} downloaded, {counts['skipped']} skipped, {counts['failed']} failed.")
    print(f"Output: {OUTPUT_DIR}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
