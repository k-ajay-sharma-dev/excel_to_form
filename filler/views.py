import mimetypes
from urllib.parse import quote

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from engine import excel

from . import tasks
from .forms import SettingsForm, UploadForm
from .models import AppSettings, Job, Row


def upload(request):
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            job = Job.objects.create(name=f.name, data=f.read(), size=f.size)
            tasks.start(tasks.process_job, job.pk)
            return redirect("job", job.pk)
    else:
        form = UploadForm()
    jobs = Job.objects.defer("data").annotate(
        n_rows=Count("rows"),
        n_submitted=Count("rows", filter=Q(rows__status="submitted")),
        n_review=Count("rows", filter=Q(rows__status="review")),
        n_ready=Count("rows", filter=Q(rows__status="ready")),
    )[:100]
    return render(request, "filler/upload.html", {"form": form, "jobs": jobs, "cfg": AppSettings.get()})


def job_delete(request, pk):
    """Delete an upload with everything stored for it (file, rows, fields, screenshots)."""
    job = get_object_or_404(Job.objects.defer("data"), pk=pk)
    if request.method != "POST":
        return redirect("job", pk)
    if job.busy:
        messages.info(request, "This upload is still being processed. Delete it when it has finished.")
        return redirect("job", pk)
    name = job.name
    job.delete()
    messages.success(request, f"Deleted {name} and all its results.")
    return redirect("upload")


def job_sig(job):
    """Changes whenever anything visible on the job page changes."""
    return job.status + ":" + ",".join(job.rows.order_by("row_no").values_list("status", flat=True))


def row_sig(row):
    return f"{row.status}:{row.updated.timestamp()}"


def job_status(request, pk):
    job = get_object_or_404(Job.objects.defer("data"), pk=pk)
    return JsonResponse({"busy": job.busy, "sig": job_sig(job)})


def row_status(request, pk):
    row = get_object_or_404(Row.objects.defer("screenshot"), pk=pk)
    return JsonResponse({"busy": row.busy, "sig": row_sig(row)})


def job_file(request, pk):
    """Download the originally uploaded file (kept in the database)."""
    job = get_object_or_404(Job, pk=pk)
    ctype = mimetypes.guess_type(job.name)[0] or "application/octet-stream"
    resp = HttpResponse(bytes(job.data), content_type=ctype)
    resp["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(job.name)}"
    return resp


def row_shot(request, pk):
    row = get_object_or_404(Row, pk=pk)
    if not row.screenshot:
        raise Http404
    return HttpResponse(bytes(row.screenshot), content_type="image/jpeg")


def settings_view(request):
    cfg = AppSettings.get()
    form = SettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(request, "filler/settings.html", {"form": form, "cfg": cfg})


def job_detail(request, pk):
    job = get_object_or_404(Job.objects.defer("data"), pk=pk)
    rows = list(job.rows.defer("screenshot").prefetch_related("fields"))
    if request.method == "POST" and request.POST.get("action") == "submit_ready":
        ready = [r.pk for r in rows if r.status == "ready"]
        if ready:
            Row.objects.filter(pk__in=ready).update(status="submitting")
            tasks.start(tasks.submit_rows, ready)
            messages.info(request, f"Submitting {len(ready)} row(s)…")
        return redirect("job", pk)
    for r in rows:
        r.n_flagged = len(r.flagged())
    counts = {}
    for r in rows:
        counts[r.get_status_display()] = counts.get(r.get_status_display(), 0) + 1
    return render(request, "filler/job.html", {
        "job": job, "rows": rows, "counts": counts, "cfg": AppSettings.get(), "sig": job_sig(job),
        "n_ready": sum(r.status == "ready" for r in rows),
    })


def _picked(f, value):
    """(indexes of the options `value` selects, tokens that match no option)."""
    values, labels = [o[0] for o in f.options], [o[1] for o in f.options]
    tokens = value.split() if f.kind == "checkbox" else ([value] if value else [])
    picked, unknown = [], []
    for t in tokens:
        # exact value first, then the same fuzzy match the Excel reader uses (by value, then by label)
        i = next((k for k, v in enumerate(values) if v.lower() == t.lower()), None)
        if i is None:
            hit = excel.choice_selected(t, [], values) or excel.choice_selected(t, [], labels)
            i = hit[0] if hit else None
        (picked if i is not None else unknown).append(i if i is not None else t)
    if not tokens and f.flags:
        picked = [k for k, on in enumerate(f.flags) if on and k < len(values)]
    return (picked if f.kind == "checkbox" else picked[:1]), unknown


def _choices(f):
    """Options for the review widget, with the current answer(s) ticked."""
    picked, unknown = _picked(f, f.value)
    opts = [{"value": v, "label": lab, "selected": k in picked} for k, (v, lab) in enumerate(f.options)]
    for t in unknown:
        opts.insert(0, {"value": t, "label": f"{t}  (from Excel - not an option)", "selected": True})
    # show the raw Excel answer only when the current answer differs from it
    f.changed = sorted(_picked(f, f.excel_value)[0]) != sorted(picked) or bool(unknown)
    return opts


def row_review(request, pk):
    row = get_object_or_404(Row.objects.select_related("job").defer("job__data"), pk=pk)
    fields = list(row.fields.all())

    if request.method == "POST" and not row.busy:
        action = request.POST.get("action")
        for f in fields:
            key = f"f_{f.pk}"
            if key not in request.POST:
                continue
            vals = request.POST.getlist(key)
            new = " ".join(v.strip() for v in vals if v.strip()) if f.kind == "checkbox" else vals[0].strip()
            changed = new != f.value
            if changed:
                f.value = new
                f.flags = []  # the user's answer replaces the Excel 0/1 columns
            if f.note:
                f.reviewed = True  # the user has seen the flagged value and kept or changed it
            f.save(update_fields=["value", "flags", "reviewed"])
        row.status = "submitting" if action == "submit" else "filling"
        row.save(update_fields=["status"])
        tasks.start(tasks.fill_row, row.pk, action == "submit")
        return redirect("row", pk)

    flagged = [f for f in fields if f.needs_review]
    others = [f for f in fields if not f.needs_review]
    for f in fields:
        f.changed = f.excel_value != f.value
        f.choices = _choices(f) if f.kind in ("radio", "checkbox") and f.options else None
    siblings = list(row.job.rows.values_list("pk", flat=True))
    i = siblings.index(row.pk)
    return render(request, "filler/row.html", {
        "row": row, "flagged": flagged, "others": others, "cfg": AppSettings.get(), "sig": row_sig(row),
        "prev": siblings[i - 1] if i > 0 else None,
        "next": siblings[i + 1] if i + 1 < len(siblings) else None,
    })
