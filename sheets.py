# pyrefly: ignore [missing-import]
import gspread
# pyrefly: ignore [missing-import]
from google.oauth2.service_account import Credentials
from config import GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON, GOOGLE_CREDS_JSON_STR
import json

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_sheet():
    if GOOGLE_CREDS_JSON_STR:
        creds_info = json.loads(GOOGLE_CREDS_JSON_STR)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1


def get_pending_numbers():
    """Return list of (row_index, phone_number) where status column is empty."""
    sheet = get_sheet()
    records = sheet.get_all_values()
    pending = []
    for i, row in enumerate(records[1:], start=2):  # skip header, 1-indexed in sheets
        phone = row[0].strip() if len(row) > 0 else ""
        status = row[1].strip() if len(row) > 1 else ""
        if phone and not status:
            pending.append((i, phone))
    return pending


def mark_dialed(row_index: int, call_sid: str):
    sheet = get_sheet()
    sheet.update_cell(row_index, 2, "dialed")
    sheet.update_cell(row_index, 3, call_sid)
