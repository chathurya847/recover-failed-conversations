# Recover Failed Conversations

Scripts to re-run chat conversations that failed. The attachments of the failed sessions are downloaded first
(from the session export, or from the Gmail inbox for email-created conversations), then each failed session is
sent again to the chat API with its original `user_input` and attachments.

```
 MongoDB export ──► download_attachments.py ───────┐
 (failed sessions)                                 ├──► downloads/<conversation_id>/ ──► run_conversation.py ──► chat API
 email records ───► Download-Email_Content/ ───────┘                                          │
                    download_email_attachments.py                                              └──► records.csv, runs/*.log
```

## Contents

| Path | Purpose |
|---|---|
| `mongo-querys/` | MongoDB aggregations that build the list of failed conversations to export. |
| `download_attachments.py` | Downloads the attachment URLs listed in a conversation export (`session_attachment_list`). |
| `Download-Email_Content/download_email_attachments.py` | Downloads attachments of email-scrubbing emails from Gmail over IMAP. |
| `run_conversation.py` | Re-sends failed sessions to the chat API and records the result of every request. |
| `auth_utils.py` | Shared token helpers (cleaning a copied token, checking it belongs to the right system). |
| `data/` | JSON exports used as input (not committed). |
| `downloads/` | Downloaded attachments, one folder per conversation (not committed). |
| `runs/` | The server's answer for each session, `runs/<session_id>.log` (not committed). |
| `records.csv` | Result of the latest request for each session (not committed). |

## Requirements

- Python 3.10+
- `pip install requests` (only `run_conversation.py` needs it; the download scripts use the standard library only)

## Configuration

Create a `.env` file next to the scripts. It is git-ignored, never commit it.
`.env.example` files show the keys.

**`recover-failed-conversations/.env`**

| Key | Used by | Meaning |
|---|---|---|
| `authenticationToken` | `run_conversation.py`, `download_attachments.py` | Login token of the target system (copying it from a browser cookie, URL-encoded and quoted, is fine). |
| `ACCOUNTS_DOMAIN_URL` | both | Accounts (login) service of the same system. When set, the script stops early if the token was issued by a different system (for example a dev token against production). |
| `BASE_URL` | `run_conversation.py` | Chat API to send to, for example `https://<tenant>.chat.layernext.ai`. |
| `DATA_FILE_PATH` | `run_conversation.py` | The JSON export to read sessions from, for example `data/session-data.json`. Default: `data.json` next to the script. |
| `DELAY_TIME` | `run_conversation.py` | Seconds between requests. Default 60. |
| `SESSION_TYPE` | `run_conversation.py` | Optional. Forces one session type instead of each session's own type. |
| `READ_TIMEOUT_SECONDS` | `run_conversation.py` | Optional. How long to wait for data from the server. Default 900. |
| `sourceAuthenticationToken`, `SOURCE_ACCOUNTS_DOMAIN_URL` | `download_attachments.py` | Optional. Use these when the attachment URLs belong to a different system than the one you send to. |

**`Download-Email_Content/.env`** (same names as layernext-cms)

| Key | Meaning |
|---|---|
| `GMAIL_APP_PASSWORD` | Gmail app password of the scrubbing mailbox. On the server, look in the `.env` of the folder shown by `pm2 describe layernext-cms-nodejs-backend` (`exec cwd`). |
| `GMAIL_USER_EMAIL` | Optional. Default `support@layernext.ai`. |
| `EMAIL_CLEANUP_MAILBOX` | Optional. Mailbox processed mails are moved to. Default `Processed`. |

## Workflow

### 1. Find the failed conversations

Run the queries in `mongo-querys/` in the MongoDB shell. They write the matching conversations, with their
sessions, to a collection that you export as JSON into `data/`:

- `db-one-session-withAttachments.sh`: failed conversations with exactly one session, and that session has attachments.
- `multiple_session_last_failed_no_attachments.sh`: failed conversations with two or more sessions, where the last session failed and has no attachments.

### 2. Download the attachments

All downloads end up in `downloads/<conversation_id>/`, with no session folder.

**From the session export** (attachments are URLs):

```powershell
python download_attachments.py data\data.json
python download_attachments.py data\data.json D:\some\other\folder   # optional output folder
```

**From email** (conversations created by the email scrubber, attachments live in Gmail):

```powershell
cd Download-Email_Content
python download_email_attachments.py email-data.csv ..\downloads
```

`email-data.csv` needs the columns `conversationId,emailId,messageId` (see `emails.example.csv`). Export all three
from the email-scrubbing records in MongoDB.

- Mails are found by `messageId` first, searching Gmail's All Mail, then INBOX and the cleanup mailbox. This finds a mail even after it was archived.
- `emailId` is the IMAP UID. It only works while the mail is still in the INBOX, and is accepted only if its Message-ID matches.
- Mails are fetched without marking them read.
- A summary goes to `email_report.json` (`report.json` for `download_attachments.py`) in the output folder.

Both download scripts can be re-run safely: files that already exist are skipped, and a different file with the same name gets a `_1` suffix.

### 3. Re-run the sessions

```powershell
python run_conversation.py conversation_id.csv --dry-run   # check first: nothing is sent
python run_conversation.py conversation_id.csv             # send for real
```

Ways to choose what runs:

```powershell
python run_conversation.py <session_id>                          # one session
python run_conversation.py <id1> <id2>                           # several, one after another
python run_conversation.py conversation_id.csv                   # ids from a CSV (one per row, header ignored)
python run_conversation.py --csv                                 # same, default file session_ids.csv
python run_conversation.py --all                                 # every session in the data file
```

An id can be a session id, or a conversation id. A conversation id selects its **last** session, which must have
status `failed`.

For each session the script:

1. checks the conversation exists on the server (`GET /api/conversation/{id}/title`);
2. if the session has attachments, uploads the files from `downloads/<conversation_id>/`
   (`POST /api/upload/session/initialize`, then `/api/upload/session/{id}/files`);
3. sends the `user_input` into the existing conversation (`POST /api/conversation`).

If a session has attachments but fewer files are found than expected, it is recorded as Failed and nothing is sent.

Options:

| Option | Effect |
|---|---|
| `--dry-run` | Print what would be sent. No network requests, nothing written. |
| `--force` | Send again even if `records.csv` already says `Send`. |
| `--wait` | Read every response stream until the server has finished, instead of closing it once the request is accepted. |
| `--session-type TYPE` | Override the session type (default: the session's own, for example `complex_analysis`). |
| `--conversation ID` | Send into this specific existing conversation on the server instead (for example one created in the UI). |
| `--create-missing` | Create a new conversation when it does not exist on the server (dev servers without production data). The old to new id mapping is kept in `conversation_map.<host>.json`. |

Requests are sent one at a time, `DELAY_TIME` seconds apart. The script does not wait for the server to finish
processing: HTTP 200 is recorded as `Send`.

### 4. Check the result

- `records.csv`: one row per session with `Session_id, Conversation_id, Status (Send/Failed), Requested_Time, Requested_Date`. A session already `Send` is skipped on the next run.
- `runs/<session_id>.log`: the server's answer (the full stream only with `--wait`).
- The reason for any `Failed` row is printed on the console.

## Supported data file formats

`run_conversation.py` detects the shape of each entry in the JSON file (`read_entry()` in the script):

| Shape | Example file | Attachments |
|---|---|---|
| Conversation with a `sessions` list | `data/data.json` | Count taken from each session's `session_attachment_list`. |
| Conversation with a single `session` | `data/reply-data.json` | Same. |
| Flat session (has `conversation_id` and `user_input`) | `data/session-data.json` | Count taken from the `Attachments:` list at the end of `user_input`. |

Mongo extended JSON (`{"$oid": "..."}`) is unwrapped. To support a new shape, add a branch in `read_entry()`.

## Troubleshooting

| Message | Cause |
|---|---|
| `Data file not found` | `DATA_FILE_PATH` in `.env` points to a file that does not exist. |
| `N attachment(s) are listed ... but M found` | The attachments were not downloaded into `downloads/<conversation_id>/` yet (step 2). |
| `conversation ... does not exist on this server` | Wrong `BASE_URL`, or a dev server. Use `--create-missing` only on dev. |
| Token rejected / stops before any request | `authenticationToken` expired, or issued by a different system than `ACCOUNTS_DOMAIN_URL`. |
| `GMAIL_APP_PASSWORD is not set` / `AUTHENTICATIONFAILED` | Missing or wrong Gmail app password in `Download-Email_Content/.env`. |
| `[MISS] mail not found` | The mail is not in All Mail, INBOX or the cleanup mailbox, or the `messageId` is wrong. |
| `ModuleNotFoundError: requests` | Run `pip install requests`. |

## Safety notes

- `BASE_URL` may be production. Always run `--dry-run` first.
- `.env`, `downloads/`, `data/`, `runs/` and the CSV files contain tokens or customer data and are git-ignored.
- If a token or password was ever committed, rotate it. Deleting the commit does not remove it from GitHub's history.
