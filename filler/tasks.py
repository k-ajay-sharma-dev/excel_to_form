"""
Background work. Playwright runs in a plain thread so the page stays responsive; a single lock
means only one browser runs at a time, and jobs queue up behind it.

Long runs on a small host: check runs are retried after a browser error, rows are spaced out a
little, and resume_after_restart() picks up unfinished work when the app starts again. A real
submission is never repeated automatically (it may already have reached Kobo).
"""
import logging
import os
import threading
import time
import traceback

from django.db import close_old_connections, connections, transaction
from django.utils import timezone

from engine import browser, excel

from .models import AppSettings, Field, Job, Row

log = logging.getLogger("filler")
LOCK = threading.Lock()

ROW_PAUSE = float(os.environ.get("ROW_PAUSE_SECONDS", "2"))  # between rows of a batch
RETRY_WAITS = [10, 30]  # seconds before the 2nd and 3rd attempt of a check run

# row.message markers that tell a queued submit from one that had started (see resume_after_restart)
SUBMIT_QUEUED = "Queued for submission…"
SUBMIT_STARTED = "Submitting to Kobo…"


def start(fn, *args):
    def run():
        close_old_connections()
        try:
            with LOCK:
                fn(*args)
        except Exception:
            log.exception("background task failed")
        finally:
            connections.close_all()  # this thread ends: release its DB session (Oracle has few)

    threading.Thread(target=run, daemon=True).start()


def process_job(job_id):
    """Read + realign the sheet, store every row, then check-fill each row (never submits)."""
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        log.info(f"Job {job_id} deleted before processing started.")
        return
    job.status = "processing"
    job.save(update_fields=["status"])
    try:
        df = excel.read_table(job.data, job.name)
        job.total_rows = len(df)
        job.save(update_fields=["total_rows"])
        mapping = excel.build_mapping(df)
        stored = set(job.rows.values_list("row_no", flat=True))  # rows saved before a restart
        for r in range(len(df)):
            if r in stored:
                continue
            if not Job.objects.filter(pk=job_id).exists():
                log.info(f"Job {job_id} deleted during row creation.")
                return
            fields = excel.row_fields(df, mapping, r)
            if not fields:
                continue
            with transaction.atomic():  # a row is stored with all its fields or not at all
                _store_row(job, r, fields)
        rows = list(job.rows.filter(status__in=("pending", "filling")).order_by("row_no")
                    .values_list("pk", flat=True))  # rows not checked yet (all of them, unless resuming)
        job.message = f"{len(df)} rows in file, {job.rows.count()} with data."
        job.save(update_fields=["message"])
        for k, pk in enumerate(rows):
            try:
                job.refresh_from_db()
                if job.status == "error":  # User stopped the job
                    log.info(f"Job {job_id} was stopped by user.")
                    return
            except Job.DoesNotExist:
                log.info(f"Job {job_id} was deleted by user.")
                return
            if k:
                time.sleep(ROW_PAUSE)
            fill_row(pk, submit=False)
        job.status = "done"
    except Exception as e:
        if not Job.objects.filter(pk=job_id).exists():
            return
        job.status = "error"
        job.message = traceback.format_exc()[-2000:]
    try:
        job.finished = timezone.now()
        job.save(update_fields=["status", "message", "finished"])
    except Job.DoesNotExist:
        pass


def _store_row(job, r, fields):
    row = Row.objects.create(job=job, row_no=r, title=excel.row_title(fields, r)[:255])
    Field.objects.bulk_create([
        Field(row=row, order=k, header=f["header"], question=f["question"], instance=f["instance"],
              group=f.get("group", ""),
              value=f["value"], excel_value=f["excel_value"], suggestion=f["suggestion"],
              flags=f["flags"], note=f["note"], kind=f["kind"], options=f["options"])
        for k, f in enumerate(fields)
    ])


def _submit_still_enabled():
    """
    Re-read the Settings switch. Called from inside Playwright's sync API, which runs an event loop
    in this thread, so Django refuses ORM calls here (SynchronousOnlyOperation); read it in a
    separate thread instead. Any error counts as "no".
    """
    out = {"ok": False}

    def read():
        close_old_connections()
        try:
            out["ok"] = AppSettings.get().submit_enabled
        except Exception:
            log.exception("could not re-check the submit setting")
        finally:
            connections.close_all()

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(10)
    return out["ok"]


def fill_row(row_id, submit=False):
    """Fill one row in the browser. submit=True submits only if Settings allow it and nothing is wrong."""
    try:
        row = Row.objects.get(pk=row_id)
    except Row.DoesNotExist:
        log.info(f"Row {row_id} deleted; skipping fill_row.")
        return
    cfg = AppSettings.get()
    fields = list(row.fields.all())
    unreviewed = any(f.note and not f.reviewed for f in fields)
    real_submit = submit and cfg.submit_enabled and not unreviewed

    row.status = "submitting" if submit else "filling"
    # SUBMIT_STARTED marks a real submission in progress, so a restart never repeats it blindly
    row.message = SUBMIT_STARTED if real_submit else ""
    row.save(update_fields=["status", "message"])

    payload = [{"question": f.question, "instance": f.instance, "group": f.group, "value": f.value, "flags": f.flags}
               for f in fields]

    # check runs (and submit runs with submission off) can't send anything, so a browser error or
    # time limit is simply tried again; a real submission is tried once only
    attempts = 1 if real_submit else 1 + len(RETRY_WAITS)
    res, error = None, ""
    for attempt in range(attempts):
        if attempt:
            wait = RETRY_WAITS[attempt - 1]
            log.info(f"Row {row_id}: browser error, retrying in {wait}s ({error})")
            Row.objects.filter(pk=row_id).update(
                message=f"Browser error, trying again ({attempt + 1} of {attempts}): {error}"[:500])
            time.sleep(wait)
        try:
            res = browser.run_row(cfg.form_url, payload, submit=real_submit,
                                  may_submit=_submit_still_enabled, headless=cfg.headless,
                                  slow_mo=cfg.slow_mo, screenshot=True, log=log.info)
        except Exception as e:
            res, error = None, f"{type(e).__name__}: {e}"
            continue
        if not res["message"].startswith("Browser error") or res["sent"]:
            break
        error = res["message"]
    if res is None:
        try:
            row.status, row.message = "error", error
            row.save(update_fields=["status", "message"])
        except Row.DoesNotExist:
            pass
        return

    results = res["results"] + [{"status": "FAILED: not reached (browser stopped)", "kind": None, "options": []}] * (
        len(fields) - len(res["results"]))
    for f, r in zip(fields, results):
        f.status = r["status"]
        if r["kind"] and r["options"]:  # the live form's own options beat the ones read from the Excel
            f.kind, f.options = r["kind"], r["options"]
    Field.objects.bulk_update(fields, ["status", "kind", "options"])

    # values still waiting for their crop keep the row in review even if the crop question was cleared
    flagged = [f for f in fields if f.needs_review or f.status.startswith(browser.WAITING)]
    msg = [res["message"]]
    if res["invalid"]:
        msg.append("Form shows errors on: " + "; ".join(res["invalid"]))

    if res["submitted"]:
        status = "submitted"
    elif res["sent"]:
        # the submission left the browser but no clear "accepted" came back: it may be in Kobo
        status = "submit_failed"
        msg = [res["message"], "The submission was sent but Kobo's answer was not confirmed - it may "
               "already be in Kobo. Check Kobo before submitting this row again."]
    elif flagged or res["invalid"] or res["message"].startswith("Browser error"):
        status = "review"
    elif submit and not cfg.submit_enabled:
        status = "dry_run"
        msg = ["Everything filled. Real submission is switched off, so nothing was sent."]
    elif submit:
        status = "submit_failed"
    else:
        status = "ready"

    try:
        row.status, row.message = status, "\n".join(m for m in msg if m)
        if res["screenshot"]:
            row.screenshot = res["screenshot"]
        row.save(update_fields=["status", "message", "screenshot"])
    except Row.DoesNotExist:
        pass


def submit_rows(row_ids):
    for k, pk in enumerate(row_ids):
        if k:
            time.sleep(ROW_PAUSE)
        fill_row(pk, submit=True)


def resume_after_restart():
    """
    Called once when the app starts. Work running in the old process died with it:
      * jobs still queued/processing are started again; they skip rows already stored and checked
      * single rows that were being checked are checked again
      * submissions are never redone: one that had started may already be in Kobo, so it is marked
        for the user to verify; one that was only queued is handed back (nothing was sent)
    """
    started = Row.objects.filter(status="submitting", message=SUBMIT_STARTED)
    n_started = started.update(status="submit_failed", message=(
        "The app restarted while this row was being submitted. It may or may not have reached Kobo - "
        "check Kobo before submitting this row again."))
    n_queued = Row.objects.filter(status="submitting").update(status="review", message=(
        "The app restarted before this row's queued submission ran. Nothing was sent; submit it again."))
    jobs = list(Job.objects.filter(status__in=("queued", "processing")).order_by("created")
                .values_list("pk", flat=True))
    rows = list(Row.objects.filter(status="filling").exclude(job_id__in=jobs).values_list("pk", flat=True))
    for pk in jobs:
        start(process_job, pk)
    for pk in rows:
        start(fill_row, pk, False)
    if jobs or rows or n_started or n_queued:
        log.warning(f"Resumed after restart: {len(jobs)} job(s), {len(rows)} row check(s); "
                    f"{n_started} interrupted submission(s) to verify, {n_queued} queued submission(s) handed back.")
