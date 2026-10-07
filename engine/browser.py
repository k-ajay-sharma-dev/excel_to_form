"""
Browser side: fill one record into the KoboToolbox (Enketo) web form with Playwright.

SUBMISSION SAFETY
  * run_row(..., submit=False) can never submit: the Submit/Save-draft buttons are removed from the
    page, service workers are disabled, and every non-GET request is aborted (any URL), except the
    POST Enketo uses to load the form (/transform/).
  * run_row(..., submit=True) blocks the same way while filling. The gate opens only for the single
    click on Submit, only to the submission URL, only if every field filled without a problem, and
    only if may_submit() still says yes at that moment.
"""
import os
import re
import sys

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

PROBLEM = ("FAILED", "SKIPPED")
BLOCKS = "form.or label.question, form.or fieldset.question"

LABELS_JS = """() => [...document.querySelectorAll('form.or label.question, form.or fieldset.question')]
    .map(e => { const l = e.querySelector('.question-label'); return l ? l.innerText : ''; })"""

OPTIONS_JS = """(el, kind) => [...el.querySelectorAll('input[type=' + kind + ']')]
    .map(i => { const l = i.closest('label'); return [i.value, (l ? l.innerText : i.value).trim()]; })"""

INDEX_JS = """(el, [kind, v]) => [...el.querySelectorAll('input[type=' + kind + ']')].findIndex(i => i.value === v)"""

INVALID_JS = """() => [...document.querySelectorAll('.invalid-required, .invalid-constraint')]
    .filter(e => e.offsetParent !== null)
    .map(e => { const l = e.querySelector('.question-label'); return (l ? l.innerText : e.innerText).trim().slice(0, 150); })"""


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def slug(s):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", str(s).lower())).strip("_")


# ----------------------------------------------------------------- locating questions
def _matching_blocks(page, label):
    target = norm(label)
    labels = [norm(t) for t in page.evaluate(LABELS_JS)]
    exact = [i for i, t in enumerate(labels) if t and t == target]
    if exact:
        return exact
    return [i for i, t in enumerate(labels)
            if t and (t.startswith(target[:30]) or (len(t) >= 15 and target.startswith(t)))]


def _add_repeat(page, block):
    """Click the '+' of the repeat group the block belongs to. False if it is not in a repeat."""
    rep = block.locator("xpath=ancestor::section[contains(concat(' ', normalize-space(@class), ' '), ' or-repeat ')][1]")
    if rep.count() == 0:
        return False
    btn = rep.locator("xpath=..").locator(".add-repeat-btn").first
    if btn.count() == 0:
        return False
    btn.click(force=True)
    page.wait_for_timeout(400)
    return True


def find_question(page, label, instance=0):
    """Question block for an Excel header; instance N = the N-th copy inside a repeat group."""
    blocks = page.locator(BLOCKS)
    idx = _matching_blocks(page, label)
    for _ in range(20):
        if not idx or len(idx) > instance:
            break
        if not _add_repeat(page, blocks.nth(idx[-1])):
            break
        idx = _matching_blocks(page, label)
    return blocks.nth(idx[instance]) if len(idx) > instance else None


def bring_into_view(page, q):
    """Enketo may be paged: click Next until the question is visible."""
    for _ in range(25):
        if q.is_visible():
            return True
        nxt = page.locator(".next-page:visible, a.next-page:visible, button.next-page:visible").first
        if nxt.count() == 0:
            return False
        nxt.click()
        page.wait_for_timeout(300)
    return q.is_visible()


# ----------------------------------------------------------------- filling
def _check(loc):
    """Tick/select an option; if the raw input is hidden, click its label instead."""
    try:
        loc.check(force=True, timeout=3000)
    except Exception:
        loc.locator("xpath=ancestor::label[1]").click(force=True, timeout=3000)


def _pick(token, values, labels):
    """Index of the option matching an Excel token: XML value, then label."""
    t = token.strip()
    low = [v.lower() for v in values]
    if t.lower() in low:
        return low.index(t.lower())
    st = slug(t)
    for i, lab in enumerate(labels):
        if st and (slug(lab) == st or slug(values[i]) == st):
            return i
    want = norm(t.replace("_", " "))
    for i, lab in enumerate(labels):
        nl = norm(lab)
        if want and nl and (want == nl or (len(want) >= 4 and (want in nl or nl in want))):
            return i
    return None


def fill_question(page, q, value, flags=None):
    """Fill one question. Returns (status, kind, options)."""
    kind = None
    if q.locator("input[type=radio]").count():
        kind = "radio"
    elif q.locator("input[type=checkbox]").count():
        kind = "checkbox"

    if kind:
        opts = [o for o in q.evaluate(OPTIONS_JS, kind) if o[0] != ""]
        values, labels = [o[0] for o in opts], [o[1] for o in opts]
        tokens = value.split() if kind == "checkbox" else ([value] if value else [])
        picks, missing = [], []
        for t in tokens:
            i = _pick(t, values, labels)
            if i is None:
                missing.append(t)
            elif i not in picks:
                picks.append(i)
        how = ""
        if flags and any(flags):
            for i, f in enumerate(flags):
                if f and i < len(opts) and i not in picks:
                    picks.append(i)
            if not tokens:
                missing = []
            how = " (with 0/1 columns)"
        if missing:
            return f"FAILED: {', '.join(missing)} is not an option of this question", kind, opts
        if not picks:
            return "FAILED: no option to select", kind, opts
        if kind == "radio":
            picks = picks[:1]
        boxes = q.locator(f"input[type={kind}]")
        for i in picks:
            # look the option up by value in the DOM so quotes etc. in values can't break a selector
            j = q.evaluate(INDEX_JS, [kind, values[i]])
            _check(boxes.nth(j if j >= 0 else i))
        return f"filled ({kind}{how})", kind, opts

    sel = q.locator("select")
    if sel.count():
        opts = sel.first.evaluate("s => [...s.options].filter(o => o.value).map(o => [o.value, o.text.trim()])")
        i = _pick(value, [o[0] for o in opts], [o[1] for o in opts])
        if i is None:
            return f"FAILED: {value} is not an option of this dropdown", "radio", opts
        sel.first.select_option(value=opts[i][0], timeout=3000)
        return "filled (dropdown)", "radio", opts

    inp = q.locator(
        "input.widget:visible, input[type=text]:visible, input[type=number]:visible, "
        "input[type=tel]:visible, input[type=date]:visible, textarea:visible"
    ).first
    if inp.count() == 0:
        return "FAILED: no input box found for this question", "text", []
    val = value.strip() if value else ""
    if not val:
        return "left empty", "text", []
    if (inp.get_attribute("type") or "") == "number":
        try:
            float(val)
        except ValueError:
            return f"FAILED: '{val}' is text but this question needs a number", "text", []
    inp.fill(val, timeout=5000)
    inp.press("Tab")  # blur so Enketo validates / updates skip logic
    return "filled (text)", "text", []


def _fill_field(page, f):
    val = (f.get("value") or "").strip()
    if not val and not any(f.get("flags") or []):
        return "left empty", None, []
    q = find_question(page, f["question"], f.get("instance", 0))
    if q is None:
        return "FAILED: question not found on the form", None, []
    if not bring_into_view(page, q):
        return "SKIPPED: question is hidden on the form (skip logic) - clear the value if it should be empty", None, []
    try:
        return fill_question(page, q, f["value"], f.get("flags"))
    except Exception as e:  # keep going, report at the end
        return f"FAILED: {type(e).__name__}: {str(e).splitlines()[0][:150]}", None, []


def _go_to_submit(page):
    btn = page.locator("#submit-form, .submit-form").first
    for _ in range(25):
        if btn.is_visible():
            return btn
        nxt = page.locator(".next-page:visible").first
        if nxt.count() == 0:
            break
        nxt.click()
        page.wait_for_timeout(300)
    return btn


# Non-GET requests Enketo needs just to load the form (it POSTs to /transform/ to fetch it).
# Everything else that could write data is blocked, not only "/submission".
SAFE_POST = re.compile(r"/transform/", re.I)
SUBMISSION = re.compile(r"/submission", re.I)


def run_row(form_url, fields, *, submit=False, may_submit=None, headless=True, slow_mo=0,
            screenshot=False, log=print):
    """
    Fill one record. fields: [{question, instance, value, flags}].
    may_submit: callable re-checked right before the Submit click (e.g. reads the Settings switch);
    if it returns False at that moment, nothing is sent.
    screenshot=True: return a JPEG of the filled form (before any submit) in out["screenshot"].
    Returns {results: [{status, kind, options}], invalid: [...], submitted: bool, message: str, screenshot}.
    """
    out = {"results": [], "invalid": [], "submitted": False, "message": "", "screenshot": None}
    gate = {"open": False}

    with sync_playwright() as p:
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            headless = True  # server without a screen (e.g. Render)
        # /dev/shm is tiny in containers; these flags keep Chromium inside a 512 MB instance
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo,
                                    args=["--disable-dev-shm-usage", "--disable-gpu"])
        # service workers are blocked: their requests would bypass the route guard below, and
        # Enketo's offline mode could otherwise queue a record and upload it in the background
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")

        def guard(route):  # SAFETY: every request of the page passes through here
            req = route.request
            if req.method in ("GET", "HEAD", "OPTIONS"):
                route.continue_()
            elif gate["open"] and SUBMISSION.search(req.url):
                route.continue_()  # only during the one Submit click, only to the submission URL
            elif SAFE_POST.search(req.url) and not SUBMISSION.search(req.url):
                route.continue_()
            else:
                log(f"BLOCKED {req.method} {req.url}")
                route.abort()

        ctx.route("**/*", guard)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())
        try:
            page.goto(form_url)
            page.wait_for_selector("form.or", timeout=60000)
            remove = "#save-draft, .save-draft" + ("" if submit else ", #submit-form, .submit-form")
            page.evaluate("sel => document.querySelectorAll(sel).forEach(e => e.remove())", remove)

            for f in fields:
                status, kind, opts = _fill_field(page, f)
                out["results"].append({"status": status, "kind": kind, "options": opts})
                log(f"  {f['question'][:55]:55} = {f['value'][:25]:25} -> {status}")

            out["invalid"] = page.evaluate(INVALID_JS)
            if screenshot:
                out["screenshot"] = page.screenshot(full_page=True, type="jpeg", quality=55)

            problems = [r for r in out["results"] if r["status"].startswith(PROBLEM)]
            if not submit:
                out["message"] = "Filled (check only, nothing submitted)."
            elif problems or out["invalid"]:
                out["message"] = (f"Not submitted: {len(problems)} field(s) could not be filled"
                                  f"{' and the form shows errors' if out['invalid'] else ''}.")
            elif may_submit is not None and not may_submit():
                out["message"] = "Not submitted: real submission was switched off in Settings."
            else:
                btn = _go_to_submit(page)
                gate["open"] = True
                try:
                    with page.expect_request(
                        lambda r: "/submission" in r.url and r.method == "POST", timeout=20000
                    ) as req_info:
                        btn.click()
                    resp = req_info.value.response()
                    code = resp.status if resp else None
                    out["submitted"] = code in (200, 201, 202)
                    out["message"] = (f"Submitted to Kobo (HTTP {code})." if out["submitted"]
                                      else f"Kobo did not accept the submission (HTTP {code}).")
                    page.wait_for_timeout(1500)
                except PWTimeout:
                    out["invalid"] = page.evaluate(INVALID_JS)
                    out["message"] = "Not submitted: the form refused to submit (required or invalid fields)."
                finally:
                    gate["open"] = False
        except Exception as e:
            out["message"] = f"Browser error: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
        finally:
            browser.close()
    return out
