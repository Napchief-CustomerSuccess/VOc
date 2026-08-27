import time
import threading
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from typing import Optional

from exotel_client import initiate_call, get_call_details
from sheets import get_pending_numbers, mark_dialed

app = FastAPI(title="Exotel Dialer")

dialer_state = {"running": False, "current_phone": None, "progress": []}


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


def wait_for_call_to_finish(call_sid: str, timeout: int = 300):
    """Poll Exotel until the call is completed/failed/no-answer."""
    terminal_statuses = {"completed", "failed", "busy", "no-answer", "canceled"}
    start = time.time()
    while time.time() - start < timeout:
        try:
            details = get_call_details(call_sid)
            status = details.get("Call", {}).get("Status", "").lower()
            if status in terminal_statuses:
                return status
        except Exception:
            pass
        time.sleep(5)
    return "timeout"


def dial_sequentially(pending):
    """Background worker: dial one number at a time, wait for each call to finish."""
    dialer_state["running"] = True
    dialer_state["progress"] = []

    for row_idx, phone in pending:
        dialer_state["current_phone"] = phone
        try:
            resp = initiate_call(phone)
            call_sid = resp.get("Call", {}).get("Sid", "unknown")
            mark_dialed(row_idx, call_sid)
            print(f"[dial] calling {phone}, call_sid={call_sid} — waiting for call to finish...")
            final_status = wait_for_call_to_finish(call_sid)
            print(f"[dial] {phone} finished with status: {final_status}")
            dialer_state["progress"].append({"phone": phone, "status": final_status, "call_sid": call_sid})
        except Exception as e:
            print(f"[dial] {phone} error: {e}")
            dialer_state["progress"].append({"phone": phone, "status": "error", "error": str(e)})
        time.sleep(2)

    dialer_state["running"] = False
    dialer_state["current_phone"] = None
    print(f"[dial] all done. {len(dialer_state['progress'])} calls processed.")


@app.api_route("/dial", methods=["GET", "POST"])
def dial_numbers(background_tasks: BackgroundTasks):
    """Read Google Sheet, dial numbers one at a time — each call waits for the previous to finish."""
    if dialer_state["running"]:
        return {
            "status": "already_running",
            "current_phone": dialer_state["current_phone"],
            "completed": len(dialer_state["progress"]),
        }

    try:
        pending = get_pending_numbers()
        if not pending:
            return {"status": "no_pending_numbers", "total": 0}

        background_tasks.add_task(dial_sequentially, pending)
        return {"status": "started", "total": len(pending), "message": "Dialing one at a time. Check /dial-status for progress."}
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}\n\nTraceback:\n{error_details}")


@app.get("/dial-status")
def dial_status():
    return {
        "running": dialer_state["running"],
        "current_phone": dialer_state["current_phone"],
        "completed": len(dialer_state["progress"]),
        "results": dialer_state["progress"],
    }


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
