import os
import time
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from typing import Optional

from exotel_client import initiate_call, get_call_details, hangup_call
from sheets import get_pending_numbers, get_retry_numbers, mark_dialed, mark_call_result, ensure_headers

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WATCHDOG_MAX_CALL_MINUTES = int(os.getenv("WATCHDOG_MAX_CALL_MINUTES", "12"))

dialer_state = {
    "running": False,
    "current_phone": None,
    "current_started_at": None,
    "progress": [],
    "auto_poll": True,
}

# Track what digit each caller pressed: call_sid -> "1", "2", etc.
call_actions = {}


def auto_poll_loop():
    """Background thread: check the Sheet every POLL_INTERVAL seconds and dial new numbers."""
    try:
        ensure_headers()
    except Exception as e:
        print(f"[auto-poll] ensure_headers failed: {e}")
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


def watchdog_loop():
    """Background thread: force-reset dialer state if a single call runs too long.

    Belt-and-suspenders safety net. wait_for_call_to_finish already caps each
    call at 10 min, but if the thread ever hangs elsewhere (network stall,
    Sheets API hang, unexpected exception), this ensures the queue never
    stays locked for more than WATCHDOG_MAX_CALL_MINUTES on one number.
    """
    print(f"[watchdog] started, max per-call = {WATCHDOG_MAX_CALL_MINUTES} min")
    while dialer_state["auto_poll"]:
        time.sleep(60)
        try:
            started_at = dialer_state.get("current_started_at")
            phone = dialer_state.get("current_phone")
            if dialer_state["running"] and started_at and phone:
                elapsed_min = (time.time() - started_at) / 60
                if elapsed_min > WATCHDOG_MAX_CALL_MINUTES:
                    print(f"[watchdog] {phone} has been current for {elapsed_min:.1f} min — force-releasing state")
                    dialer_state["running"] = False
                    dialer_state["current_phone"] = None
                    dialer_state["current_started_at"] = None
        except Exception as e:
            print(f"[watchdog] error: {e}")


@asynccontextmanager
async def lifespan(app):
    poll_thread = threading.Thread(target=auto_poll_loop, daemon=True)
    poll_thread.start()
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()
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


def wait_for_call_to_finish(call_sid: str, timeout: int = 600):
    """Poll Exotel until the call reaches a terminal state, max 10 minutes.

    If stuck in-progress > 8 min (voicemail, fax line, silent line that
    never hangs up), actively tell Exotel to hang up so the queue moves on.
    """
    terminal_statuses = {"completed", "failed", "busy", "no-answer", "canceled"}
    unknown_count = 0
    in_progress_start = None
    start = time.time()
    while time.time() - start < timeout:
        try:
            details = get_call_details(call_sid)
            status = details.get("Call", {}).get("Status", "").lower()
            if status in terminal_statuses:
                return status
            if status == "in-progress":
                if in_progress_start is None:
                    in_progress_start = time.time()
                elif time.time() - in_progress_start > 480:
                    print(f"[wait] {call_sid} stuck in-progress for 8 min, hanging up")
                    try:
                        hangup_call(call_sid)
                    except Exception as e:
                        print(f"[wait] hangup failed: {e}")
                    return "failed"
                unknown_count = 0
            elif status in ("", "unknown") or not status:
                unknown_count += 1
                if unknown_count >= 30:
                    print(f"[wait] {call_sid} stuck with unknown status for 60s, marking as failed")
                    return "failed"
            else:
                unknown_count = 0
        except Exception:
            unknown_count += 1
            if unknown_count >= 30:
                return "failed"
        time.sleep(2)
    print(f"[wait] {call_sid} hit 10-min hard timeout, hanging up")
    try:
        hangup_call(call_sid)
    except Exception as e:
        print(f"[wait] hangup failed: {e}")
    return "timeout"


def dial_sequentially(pending):
    """Background worker: dial one number at a time, wait for each call to finish."""
    dialer_state["running"] = True
    dialer_state["progress"] = []

    try:
        for row_idx, phone in pending:
            dialer_state["current_phone"] = phone
            dialer_state["current_started_at"] = time.time()

            clean = phone.replace("+", "").replace(" ", "").replace("-", "")
            if not clean.isdigit() or len(clean) < 10 or len(clean) > 13 or "E" in phone or "e" in phone:
                print(f"[dial] skipping invalid number: {phone}")
                try:
                    mark_call_result(row_idx, "failed")
                except Exception as e:
                    print(f"[dial] mark_call_result failed for invalid {phone}: {e}")
                dialer_state["progress"].append({"phone": phone, "status": "invalid", "call_sid": "none"})
                continue

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
                try:
                    mark_call_result(row_idx, display_status)
                except Exception as e:
                    print(f"[dial] mark_call_result failed for {phone}: {e}")
                dialer_state["progress"].append({"phone": phone, "status": display_status, "call_sid": call_sid})
            except Exception as e:
                print(f"[dial] {phone} error: {e}")
                try:
                    mark_call_result(row_idx, f"error: {e}")
                except Exception as inner:
                    print(f"[dial] mark_call_result failed on error path for {phone}: {inner}")
                dialer_state["progress"].append({"phone": phone, "status": "error", "error": str(e)})

            time.sleep(5)
    finally:
        dialer_state["running"] = False
        dialer_state["current_phone"] = None
        dialer_state["current_started_at"] = None
        print(f"[dial] batch done. {len(dialer_state['progress'])} calls processed.")


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
    started_at = dialer_state.get("current_started_at")
    elapsed_sec = int(time.time() - started_at) if started_at else None
    return {
        "running": dialer_state["running"],
        "current_phone": dialer_state["current_phone"],
        "current_elapsed_sec": elapsed_sec,
        "completed": len(dialer_state["progress"]),
        "results": dialer_state["progress"],
    }


@app.api_route("/reset", methods=["GET", "POST"])
def reset_dialer():
    """Force-clear the dialer state. Use if a call is wedged and you don't want to redeploy."""
    was_running = dialer_state["running"]
    was_phone = dialer_state["current_phone"]
    dialer_state["running"] = False
    dialer_state["current_phone"] = None
    dialer_state["current_started_at"] = None
    print(f"[reset] cleared state (was running={was_running}, phone={was_phone})")
    return {"status": "reset", "was_running": was_running, "was_phone": was_phone}


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
@app.api_route("/exotel/passthru", methods=["GET", "POST"])
async def passthru(request: Request):
    """Exotel Passthru applet hits this endpoint.

    Exotel sends GET by default (params in query string) but form/JSON
    POST is also supported.
    """
    content_type = request.headers.get("content-type", "")
    if request.method == "POST":
        if "form" in content_type:
            data = dict(await request.form())
        elif await request.body():
            data = await request.json()
        else:
            data = {}
    else:
        data = dict(request.query_params)

    call_sid = data.get("CallSid", "")
    call_from = data.get("CallFrom", "")
    call_to = data.get("CallTo", "")
    direction = data.get("Direction", "")
    digits = data.get("digits", "")
    call_status = data.get("CallStatus", "")

    print(f"[passthru] sid={call_sid} from={call_from} to={call_to} "
          f"dir={direction} digits={digits} status={call_status}")

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
@app.api_route("/exotel/status", methods=["GET", "POST"])
async def status_callback(request: Request):
    content_type = request.headers.get("content-type", "")
    if request.method == "POST":
        if "form" in content_type:
            data = dict(await request.form())
        elif await request.body():
            data = await request.json()
        else:
            data = {}
    else:
        data = dict(request.query_params)

    print(f"[status] {data}")
    return {"status": "received"}


# ── Lookup a call ────────────────────────────────────────────────────
@app.get("/call/{call_sid}")
def call_detail(call_sid: str):
    try:
        return get_call_details(call_sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
