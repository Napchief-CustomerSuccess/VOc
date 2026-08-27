import requests
from config import (
    EXOTEL_SID,
    EXOTEL_API_KEY,
    EXOTEL_API_TOKEN,
    EXOTEL_CALLER_ID,
    EXOTEL_SUBDOMAIN,
    PASSTHRU_FLOW_APP_ID,
)


def initiate_call(to_number: str) -> dict:
    """Place an outbound call via Exotel REST API.

    Connects the callee to the flow defined by PASSTHRU_FLOW_APP_ID.
    """
    url = f"https://{EXOTEL_SUBDOMAIN}/v1/Accounts/{EXOTEL_SID}/Calls/connect.json"

    payload = {
        "From": to_number,
        "CallerId": EXOTEL_CALLER_ID,
        "Url": f"http://my.exotel.com/Exotel/exoml/start_voice/{PASSTHRU_FLOW_APP_ID}",
    }

    resp = requests.post(
        url,
        data=payload,
        auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_call_details(call_sid: str) -> dict:
    url = f"https://{EXOTEL_SUBDOMAIN}/v1/Accounts/{EXOTEL_SID}/Calls/{call_sid}.json"
    resp = requests.get(
        url,
        auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
