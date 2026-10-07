"""
Background work. Playwright runs in a plain thread so the page stays responsive; a single lock
means only one browser runs at a time, and jobs queue up behind it.
"""
import logging
import threading
import traceback

from django.conf import settings
from django.db import close_old_connections

from engine import browser, excel

from .models import AppSettings, Field, Job, Row

log = logging.getLogger("filler")
LOCK = threading.Lock()


def start(fn, *args):
    def run():
        close_old_connections()
        try:
            with LOCK:
                fn(*args)
        except Exception:
            log.exception("background task failed")
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()


def process_job(job_id):
    """Read + realign the sheet, store every row, then check-fill each row (never submits)."""
    job = Job.objects.get(pk=job_id)
    job.status = "processing"
    job.save(update_fields=["status"])
    try:
        df = excel.read_table(job.file.path)
        mapping = excel.build_mapping(df)
        for r in range(len(df)):
            fields = excel.row_fields(df, mapping, r)
            if not fields:
                continue
            row = Row.objects.create(job=job, row_no=r, title=excel.row_title(fields, r)[:255])
            Field.objects.bulk_create([
                Field(row=row, order=k, header=f["header"], question=f["question"], instance=f["instance"],
                      value=f["value"], excel_value=f["excel_value"], suggestion=f["suggestion"],
                      flags=f["flags"], note=f["note"])
                for k, f in enumerate(fields)
            ])
        rows = list(job.rows.values_list("pk", flat=True))
        job.message = f"{len(df)} rows in file, {len(rows)} with data."
        job.save(update_fields=["message"])
        for pk in rows:
            fill_row(pk, submit=False)
        job.status = "done"
    except Exception:
        job.status = "error"
        job.message = traceback.format_exc()[-2000:]
    job.save(update_fields=["status", "message"])


def fill_row(row_id, submit=False):
    """Fill one row in the browser. submit=True submits only if Settings allow it and nothing is wrong."""
    row = Row.objects.get(pk=row_id)
    cfg = AppSettings.get()
    fields = list(row.fields.all())
    unreviewed = any(f.note and not f.reviewed for f in fields)
    real_submit = submit and cfg.submit_enabled and not unreviewed

    row.status = "submitting" if submit else "filling"
    row.save(update_fields=["status"])

    shots = settings.MEDIA_ROOT / "screens"
    shots.mkdir(parents=True, exist_ok=True)
    shot_name = f"screens/job{row.job_id}_row{row.row_no}.png"
    payload = [{"question": f.question, "instance": f.instance, "value": f.value, "flags": f.flags} for f in fields]

    try:
        res = browser.run_row(cfg.form_url, payload, submit=real_submit,
                              may_submit=lambda: AppSettings.get().submit_enabled, headless=cfg.headless,
                              slow_mo=cfg.slow_mo, screenshot=settings.MEDIA_ROOT / shot_name, log=log.info)
    except Exception as e:
        row.status, row.message = "error", f"{type(e).__name__}: {e}"
        row.save(update_fields=["status", "message"])
        return

    results = res["results"] + [{"status": "FAILED: not reached (browser stopped)", "kind": None, "options": []}] * (
        len(fields) - len(res["results"]))
    for f, r in zip(fields, results):
        f.status, f.kind, f.options = r["status"], r["kind"] or "", r["options"] or []
    Field.objects.bulk_update(fields, ["status", "kind", "options"])

    flagged = [f for f in fields if f.needs_review]
    msg = [res["message"]]
    if res["invalid"]:
        msg.append("Form shows errors on: " + "; ".join(res["invalid"]))

    if res["submitted"]:
        status = "submitted"
    elif flagged or res["invalid"] or res["message"].startswith("Browser error"):
        status = "review"
    elif submit and not cfg.submit_enabled:
        status = "dry_run"
        msg = ["Everything filled. Real submission is switched off, so nothing was sent."]
    elif submit:
        status = "submit_failed"
    else:
        status = "ready"

    row.status, row.message = status, "\n".join(m for m in msg if m)
    row.screenshot = shot_name if (settings.MEDIA_ROOT / shot_name).exists() else ""
    row.save(update_fields=["status", "message", "screenshot"])


def submit_rows(row_ids):
    for pk in row_ids:
        fill_row(pk, submit=True)
