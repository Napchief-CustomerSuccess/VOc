# pyrefly: ignore [missing-import]
import gspread
# pyrefly: ignore [missing-import]
from google.oauth2.service_account import Credentials
from config import GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON, GOOGLE_CREDS_JSON_STR
import json
from datetime import datetime, timedelta
import pytz

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

IST = pytz.timezone("Asia/Kolkata")

HEADERS = ["Phone", "Status", "Call SID", "Call Status", "Date", "Time", "Month", "Attempts", "Last Attempt"]

# Retry intervals in minutes: after attempt 1 wait 30m, after 2 wait 60m, after 3 wait 90m, after 4 wait 120m
RETRY_INTERVALS = {1: 30, 2: 60, 3: 90, 4: 120}
MAX_ATTEMPTS = 5


def get_sheet():
    if GOOGLE_CREDS_JSON_STR:
        creds_info = json.loads(GOOGLE_CREDS_JSON_STR)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1


def ensure_headers():
    sheet = get_sheet()
    first_row = sheet.row_values(1)
    if first_row != HEADERS:
        sheet.update("A1:I1", [HEADERS])


def get_pending_numbers():
    """Return list of (row_index, phone_number) where status column is empty."""
    sheet = get_sheet()
    records = sheet.get_all_values()
    pending = []
    for i, row in enumerate(records[1:], start=2):
        phone = row[0].strip() if len(row) > 0 else ""
        status = row[1].strip() if len(row) > 1 else ""
        if phone and not status:
            pending.append((i, phone))
    return pending


def get_retry_numbers():
    """Return list of (row_index, phone_number) eligible for retry today."""
    sheet = get_sheet()
    records = sheet.get_all_values()
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    retry_list = []

    for i, row in enumerate(records[1:], start=2):
        phone = row[0].strip() if len(row) > 0 else ""
        status = row[1].strip() if len(row) > 1 else ""
        call_status = row[3].strip() if len(row) > 3 else ""
        date = row[4].strip() if len(row) > 4 else ""
        attempts_str = row[7].strip() if len(row) > 7 else ""
        last_attempt_str = row[8].strip() if len(row) > 8 else ""

        if not phone or status != "dialed":
            continue
        if call_status == "completed":
            continue
        if date != today:
            continue

        attempts = int(attempts_str) if attempts_str else 1
        if attempts >= MAX_ATTEMPTS:
            continue

        retry_wait = RETRY_INTERVALS.get(attempts, 120)

        if last_attempt_str:
            try:
                last_attempt = datetime.strptime(f"{today} {last_attempt_str}", "%Y-%m-%d %H:%M:%S")
                last_attempt = IST.localize(last_attempt)
                if now < last_attempt + timedelta(minutes=retry_wait):
                    continue
            except ValueError:
                continue

        retry_list.append((i, phone))

    return retry_list


def mark_dialed(row_index: int, call_sid: str):
    now = datetime.now(IST)
    sheet = get_sheet()
    current_row = sheet.row_values(row_index)
    attempts_str = current_row[7].strip() if len(current_row) > 7 else ""
    attempts = int(attempts_str) if attempts_str else 0
    attempts += 1

    sheet.update(f"B{row_index}:I{row_index}", [[
        "dialed",
        call_sid,
        "",
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        now.strftime("%B %Y"),
        str(attempts),
        now.strftime("%H:%M:%S"),
    ]])


def mark_call_result(row_index: int, call_status: str):
    sheet = get_sheet()
    sheet.update_cell(row_index, 4, call_status)
