import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from typing import Optional

from exotel_client import initiate_call, get_call_details
from sheets import get_pending_numbers, mark_dialed

app = FastAPI(title="Exotel Dialer")


# ── Health ───────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "exotel-dialer"}


# ── Dial all pending numbers from Google Sheet ───────────────────────
class DialResponse(BaseModel):
    total: int
    dialed: int
    errors: int
    results: list


@app.api_route("/dial", methods=["GET", "POST"], response_model=DialResponse)
def dial_numbers(delay: float = 1.0):
    """Read Google Sheet, dial every pending number via Exotel."""
    pending = get_pending_numbers()
    results = []
    dialed = 0
    errors = 0

    for row_idx, phone in pending:
        try:
            resp = initiate_call(phone)
            call_sid = resp.get("Call", {}).get("Sid", "unknown")
            mark_dialed(row_idx, call_sid)
            results.append({"phone": phone, "status": "dialed", "call_sid": call_sid})
            dialed += 1
        except Exception as e:
            results.append({"phone": phone, "status": "error", "error": str(e)})
            errors += 1
        time.sleep(delay)

    return DialResponse(total=len(pending), dialed=dialed, errors=errors, results=results)


# ── Dial a single number ─────────────────────────────────────────────
@app.post("/dial-one")
def dial_one(phone: str):
    try:
        resp = initiate_call(phone)
        call_sid = resp.get("Call", {}).get("Sid", "unknown")
        return {"phone": phone, "status": "dialed", "call_sid": call_sid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Exotel Passthru webhook ─────────────────────────────────────────
@app.post("/exotel/passthru")
async def passthru(request: Request):
    """Exotel Passthru applet hits this endpoint.

    Receives form data with call details:
      - CallSid, CallFrom, CallTo, CallStatus,
      - Direction, digits (if gather was used), etc.

    Return plain text or JSON — Exotel Passthru expects an HTTP 200
    with optional response body to control the next applet.
    """
    content_type = request.headers.get("content-type", "")
    if "form" in content_type:
        data = dict(await request.form())
    else:
        data = await request.json() if await request.body() else {}

    call_sid = data.get("CallSid", "")
    call_from = data.get("CallFrom", "")
    call_to = data.get("CallTo", "")
    direction = data.get("Direction", "")
    digits = data.get("digits", "")
    call_status = data.get("CallStatus", "")

    print(f"[passthru] sid={call_sid} from={call_from} to={call_to} "
          f"dir={direction} digits={digits} status={call_status}")

    # Return 200 — Exotel continues the flow.
    # If you need to branch based on digits or status, add logic here.
    return JSONResponse({"status": "ok"})


# ── Call status callback (optional — set in Exotel flow) ─────────────
@app.post("/exotel/status")
async def status_callback(request: Request):
    content_type = request.headers.get("content-type", "")
    if "form" in content_type:
        data = dict(await request.form())
    else:
        data = await request.json() if await request.body() else {}

    print(f"[status] {data}")
    return {"status": "received"}


# ── Lookup a call ────────────────────────────────────────────────────
@app.get("/call/{call_sid}")
def call_detail(call_sid: str):
    try:
        return get_call_details(call_sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
