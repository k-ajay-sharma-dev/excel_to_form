"""
Excel/CSV side: read the sheet, repair the column shift, and turn each row into a list of fields.

Why the repair is needed: in the Kobo export we get, the data cells drift to the right of their
headers (0 columns at Q1, 1 at Q9, 2 at Q16, 3 from Q20 on), and the header row also contains a
block of old-version questions that have no data at all. The data itself is complete and in form
order, so we re-anchor it: every choice question's answer is an XML choice name (e.g.
"rainfed_only") that we can recognise next to its option columns ("…/Rainfed only"). The column
where that name really sits tells us the shift at that point of the sheet.
"""
import datetime as dt
import io
import re

import pandas as pd

SKIP_HEADERS = {"start", "end"}
META_STATUS_VALUES = {"submitted_via_web", "submitted_via_app"}  # what Kobo writes in _status


def slug(s):
    """Kobo's label -> XML choice name rule, with runs of '_' collapsed so small differences match."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", str(s).lower())).strip("_")


def read_table(src, name=None):
    """src: a file path, or the file's bytes (then `name` tells CSV from Excel)."""
    name = str(name or src)
    if isinstance(src, (bytes, bytearray, memoryview)):
        src = io.BytesIO(bytes(src))
    if name.lower().endswith(".csv"):
        return pd.read_csv(src, dtype=object)
    return pd.read_excel(src, dtype=object)


def is_blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "NaT", "None")


def clean_value(header, v):
    """Excel cell -> string to type into the form ('' = nothing to fill)."""
    if is_blank(v):
        return ""
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        # Excel serial date (e.g. 46264), only for real date columns and plausible serials
        if re.search(r"\bdate\b", header, re.I) and 20000 < float(v) < 80000:
            return (dt.datetime(1899, 12, 30) + dt.timedelta(days=int(v))).strftime("%Y-%m-%d")
        if float(v).is_integer():  # 41.0 -> "41", 8489810382.0 -> "8489810382"
            return str(int(v))
        return str(v)
    return str(v).strip()


def split_instance(header):
    """pandas renames duplicate headers 'Crop Name', 'Crop Name.1', ... -> (label, repeat index)."""
    m = re.match(r"^(.*\S)\.(\d+)$", header)
    if m and not re.match(r"^\d+$", m.group(1)):
        return m.group(1), int(m.group(2))
    return header, 0


def _skip_header(h):
    low = h.strip().lower()
    return (
        low in SKIP_HEADERS
        or low.startswith("signature")  # signature/drawing widgets can't be typed
        or low.endswith("_url")
    )


def _tokens(df, j):
    out = []
    for v in df.iloc[:, j]:
        if not is_blank(v):
            out += str(v).split()
    return out


def _is_choice_name(token, names):
    t = slug(token)
    if not t or t.replace("_", "").isdigit():
        return False
    for n in names:
        if t == n or (len(t) >= 6 and (n.startswith(t) or t.startswith(n) or n.endswith(t))):
            return True
    return False


def _blank_col(df, j):
    return 0 <= j < df.shape[1] and all(is_blank(v) for v in df.iloc[:, j])


def _place_step(df, between, old, new):
    """
    The shift grew from `old` to `new` somewhere among the questions `between` two anchors,
    because extra (empty) data columns were inserted there. The last column that is empty in
    every row is taken as the insertion point: questions before it keep the old shift, questions
    after it get the new one, and the question sitting right on it is flagged for review.
    """
    if not between:
        return
    lo, hi = between[0]["hcol"] + old, between[-1]["hcol"] + new
    blanks = [j for j in range(lo, hi + 1) if _blank_col(df, j)]
    if not blanks:  # can't tell where the step is -> let the user decide for all of them
        for e in between:
            e["alt_off"], e["conf"] = new, "uncertain"
        return
    step = blanks[-1]
    for e in between:
        c = e["hcol"] + old
        if c == step:
            e["alt_off"], e["conf"] = new, "uncertain"
        elif c > step:
            e["off"] = new


def build_mapping(df):
    """
    One entry per form question (option columns are folded into their parent):
      header     original Excel header
      col        data column that really holds the answer (None = no data, skip)
      kid_cols   data columns of the 0/1 option flags (choice questions)
      alt_col    second candidate column when the shift changes between two anchors
      conf       'ok' | 'uncertain' | 'phantom' (header has no data column) | 'skip'
    """
    H = [str(c) for c in df.columns]
    n = len(H)
    kids = {}
    for h in H:
        k = [o for o in H if o != h and o.startswith(h) and o[len(h):].lstrip().startswith("/")]
        if k:
            kids[h] = k
    child = {k for ks in kids.values() for k in ks}

    meta_idx = [i for i, h in enumerate(H) if h.startswith("_") or h.startswith("meta/")]
    first_meta = meta_idx[0] if meta_idx else n
    # where Kobo's own columns (_id, _uuid, ...) really start in the data
    data_meta_start = first_meta
    if "_status" in H:
        for j in range(n):
            if any(t in META_STATUS_VALUES for t in _tokens(df, j)):
                data_meta_start = j - (H.index("_status") - first_meta)
                break

    entries, off, since_anchor = [], 0, []
    for i, h in enumerate(H[:first_meta]):
        if h in child:
            continue
        e = {"header": h, "hcol": i, "off": off, "alt_off": None, "conf": "ok"}
        anchored = None
        if h in kids:
            names = [slug(k[len(h):].lstrip()[1:]) for k in kids[h]]
            for o in range(off, off + 15):  # the shift only ever grows
                j = i + o
                if j >= data_meta_start:
                    break
                toks = _tokens(df, j)
                if toks and all(_is_choice_name(t, names) for t in toks):
                    anchored = o
                    break
            if anchored is not None:
                if anchored != off:
                    _place_step(df, since_anchor, off, anchored)
                    off = anchored
                e["off"] = off
                since_anchor = []
            elif _tokens(df, i + off) if i + off < n else False:
                e["conf"] = "uncertain"  # has data but we could not confirm the position
        entries.append(e)
        if anchored is None:
            since_anchor.append(e)

    mapping = []
    for e in entries:
        h, i = e["header"], e["hcol"]
        col = i + e["off"]
        m = {
            "header": h,
            "col": col,
            "kid_cols": [H.index(k) + e["off"] for k in kids.get(h, [])],
            "alt_col": None if e["alt_off"] is None else i + e["alt_off"],
            "conf": e["conf"],
            # option labels from the "Question/Option" columns, in form order
            "options": [k[len(h):].lstrip()[1:].strip() for k in kids.get(h, [])],
            "multi": False,
        }
        if m["options"]:
            m["multi"] = _is_multi(df, h, col, m["kid_cols"])
        elif 0 <= col < n:
            # plain yes/no questions have no option columns in the export; offer Yes/No anyway
            vals = {clean_value(h, v).lower() for v in df.iloc[:, col]} - {""}
            if vals and vals <= {"yes", "no"}:
                m["options"], m["yes_no"] = ["Yes", "No"], True
        if _skip_header(h):
            m["conf"] = "skip"
        elif col >= data_meta_start:
            m["conf"] = "phantom"
        mapping.append(m)
    return mapping


def _is_multi(df, header, col, kid_cols):
    """Tick-all-that-apply question? (header says so, or some row has several answers)."""
    if re.search(r"tick all|select all|all that apply", header, re.I):
        return True
    for r in range(len(df)):
        row = df.iloc[r]
        if len(clean_value(header, _cell(row, col)).split()) > 1:
            return True
        if sum(clean_value(header, _cell(row, j)) in ("1", "true", "True") for j in kid_cols) > 1:
            return True
    return False


def choice_selected(value, flags, options):
    """Indexes of the options an Excel answer picks: by XML choice name, else by the 0/1 columns."""
    names = [slug(o) for o in options]
    picked = []
    for t in value.split():
        for i, n in enumerate(names):
            if i not in picked and _is_choice_name(t, [n]):
                picked.append(i)
                break
    if not picked and flags:
        picked = [i for i, f in enumerate(flags) if f and i < len(options)]
    return picked


def _cell(row, j):
    return row.iloc[j] if j is not None and 0 <= j < len(row) else None


def row_fields(df, mapping, r):
    """Fields of data row r that have something to fill, in form order."""
    row = df.iloc[r]
    out = []
    for m in mapping:
        if m["conf"] in ("skip", "phantom"):
            continue
        h = m["header"]
        value = clean_value(h, _cell(row, m["col"]))
        flags = [clean_value(h, _cell(row, j)) in ("1", "true", "True", "TRUE") for j in m["kid_cols"]]
        note, suggestion = "", ""
        if m["alt_col"] is not None and m.get("off", 0) != 0:
            alt = clean_value(h, _cell(row, m["alt_col"]))
            if alt != value and (alt or value):
                suggestion = alt
                note = "Excel columns are shifted here; the answer may be the suggested value instead."
        elif m["conf"] == "uncertain" and value:
            note = "Could not confirm this column's position in the Excel; please check the value."
        if not value and not any(flags) and not note:
            continue  # nothing in Excel -> leave blank
        question, instance = split_instance(h)
        excel_value = value
        kind, options = "", []
        if m["options"]:
            # until the live form tells us its exact options, offer the Excel's own option columns;
            # option values are slugs of the labels, which the filler matches against the form
            kind = "checkbox" if m["multi"] else "radio"
            options = [[slug(o), o] for o in m["options"]]
            picked = choice_selected(value, flags, m["options"])
            if picked and (not value or len(picked) == len(value.split())):
                value = " ".join(options[i][0] for i in picked)
        out.append({
            "header": h,
            "question": question,
            "instance": instance,
            "value": value,
            "excel_value": excel_value,
            "flags": flags if any(flags) else [],
            "suggestion": suggestion,
            "note": note,
            "kind": kind,
            "options": options,
        })
    return out


def row_title(fields, row_no):
    for f in fields:
        if "name of the beneficiary" in f["question"].lower() and f["value"]:
            return f["value"]
    return f"Row {row_no + 1}"
