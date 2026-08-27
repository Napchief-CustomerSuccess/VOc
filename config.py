import os

EXOTEL_SID = os.getenv("EXOTEL_SID")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_CALLER_ID = os.getenv("EXOTEL_CALLER_ID")  # your exophone e.g. 02246181279
EXOTEL_SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "credentials.json")
GOOGLE_CREDS_JSON_STR = os.getenv("GOOGLE_CREDS_JSON_STR")  # Useful for Railway

PASSTHRU_FLOW_APP_ID = os.getenv("PASSTHRU_FLOW_APP_ID")
