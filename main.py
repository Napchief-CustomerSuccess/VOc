import os
import time
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from typing import Optional

from exotel_client import initiate_call, get_call_details
from sheets import get_pending_numbers, get_retry_numbers, mark_dialed, mark_call_result, ensure_headers

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

dialer_state = {"running": False, "current_phone": None, "progress": [], "auto_poll": True}

# Track what digit each caller pressed: call_sid -> "1", "2", etc.
call_actions = {}


def auto_poll_loop():
    """Background thread: check the Sheet every POLL_INTERVAL seconds and dial new numbers."""
    try:
        ensure_headers()
    except Exception:
        pass
    print(f"[auto-poll] started, checking every {POLL_INTERVAL}s")
    while dialer_state["auto_poll"]:
        try:
            if not dialer_state["running"]:
                pending = get_pending_numbers()
                if pending:
                    print(f"[auto-poll] found {len(pending)} new numbers, starting dialer...")
                    dial_sequentially(pending)
                elif not dialer_state["running"]:
                    retries = get_retry_numbers()
                    if retries:
                        print(f"[auto-poll] found {len(retries)} numbers to retry...")
                        dial_sequentially(retries)
        except Exception as e:
            print(f"[auto-poll] error: {e}")
        time.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app):
    poll_thread = threading.Thread(target=auto_poll_loop, daemon=True)
    poll_thread.start()
    yield
    dialer_state["auto_poll"] = False


app = FastAPI(title="Exotel Dialer", lifespan=lifespan)


# ── Health ───────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "exotel-dialer", "auto_poll": dialer_state["auto_poll"]}


# ── Dial all pending numbers from Google Sheet ───────────────────────
class DialResponse(BaseModel):
    total: int
    dialed: int
    errors: int
    results: list


def wait_for_call_to_finish(call_sid: str, timeout: int = 7200):
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
        time.sleep(2)
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
            action = call_actions.pop(call_sid, None)
            if action == "1":
                display_status = "completed"
            elif action == "2":
                display_status = "rescheduled"
            elif final_status == "no-answer":
                display_status = "no-answer"
            elif final_status == "busy":
                display_status = "busy"
            elif final_status == "failed":
                display_status = "failed"
            elif final_status == "completed" and action is None:
                display_status = "no-response"
            else:
                display_status = final_status
            print(f"[dial] {phone} finished: exotel={final_status}, action={action}, sheet={display_status}")
            mark_call_result(row_idx, display_status)
            dialer_state["progress"].append({"phone": phone, "status": display_status, "call_sid": call_sid})
        except Exception as e:
            print(f"[dial] {phone} error: {e}")
            mark_call_result(row_idx, f"error: {e}")
            dialer_state["progress"].append({"phone": phone, "status": "error", "error": str(e)})

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
        retries = get_retry_numbers()
        all_numbers = pending + retries
        if not all_numbers:
            return {"status": "no_pending_numbers", "total": 0, "new": 0, "retries": 0}

        background_tasks.add_task(dial_sequentially, all_numbers)
        return {"status": "started", "total": len(all_numbers), "new": len(pending), "retries": len(retries), "message": "Dialing one at a time. Check /dial-status for progress."}
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
    return JSONResponse({"status": "ok"})


# ── Track customer action (press 1 = connected, press 2 = rescheduled) ──
@app.api_route("/exotel/pressed1", methods=["GET", "POST"])
async def pressed1(request: Request):
    """Exotel hits this when customer presses 1 (Connect)."""
    data = dict(await request.form()) if "form" in request.headers.get("content-type", "") else {}
    call_sid = data.get("CallSid", request.query_params.get("CallSid", ""))
    if call_sid:
        call_actions[call_sid] = "1"
        print(f"[pressed1] {call_sid} — customer pressed 1 (connect)")
    return JSONResponse({"status": "ok"})


@app.api_route("/exotel/pressed2", methods=["GET", "POST"])
async def pressed2(request: Request):
    """Exotel hits this when customer presses 2 (Rescheduled)."""
    data = dict(await request.form()) if "form" in request.headers.get("content-type", "") else {}
    call_sid = data.get("CallSid", request.query_params.get("CallSid", ""))
    if call_sid:
        call_actions[call_sid] = "2"
        print(f"[pressed2] {call_sid} — customer pressed 2 (reschedule)")
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
