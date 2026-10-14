"""
Excel/CSV side: read the sheet, repair the column shift, and turn each row into a list of fields.

Why the repair is needed: in the Kobo export we get, the data cells drift to the right of their
headers (0 columns at Q1, 1 at Q9, 2 at Q16, 3 from Q20 on), and the header row also contains a
block of old-version questions that have no data at all. The data itself is complete and in form
order, so we re-anchor it: every choice question's answer is an XML choice name (e.g.
"rainfed_only") that we can recognise next to its option columns ("…/Rainfed only"). The column
where that name really sits tells us the shift at that point of the sheet.

Kobo can also export "labels" instead of XML values ("Agriculture (own land)"); then the answer
is matched as whole option labels (parse_labels), which anchors the columns the same way.
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


def _norm_label(s):
    return " ".join(str(s).split()).lower()


def parse_labels(value, options):
    """
    Indexes of the options a "labels" export answer is made of, e.g. "Borewell Tank / Pond" ->
    [Borewell, Tank / Pond]. Kobo joins the labels of a multiple choice with spaces, and labels
    contain spaces too, so the whole text must split into known labels (longest label first,
    backtracking if needed). None if it doesn't.
    """
    text = _norm_label(value)
    labels = sorted(((_norm_label(o), i) for i, o in enumerate(options) if _norm_label(o)),
                    key=lambda x: -len(x[0]))
    if not text or not labels:
        return None

    def walk(pos, picked):
        if pos == len(text):
            return picked
        for lab, i in labels:
            end = pos + len(lab)
            if text.startswith(lab, pos) and (end == len(text) or text[end] == " "):
                found = walk(end + 1 if end < len(text) else end, picked + [i])
                if found is not None:
                    return found
        return None

    picked = walk(0, [])
    return list(dict.fromkeys(picked)) if picked is not None else None


def _is_choice_col(df, j, names, options):
    """Does column j hold this question's answers (as XML choice names or as option labels)?"""
    vals = [str(v).strip() for v in df.iloc[:, j] if not is_blank(v)]
    if not vals:
        return False
    if all(_is_choice_name(t, names) for v in vals for t in v.split()):
        return True
    return all(parse_labels(v, options) is not None for v in vals)


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
            labels = [k[len(h):].lstrip()[1:].strip() for k in kids[h]]
            names = [slug(lab) for lab in labels]
            for o in range(off, off + 15):  # the shift only ever grows
                j = i + o
                if j >= data_meta_start:
                    break
                if _is_choice_col(df, j, names, labels):
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
            "kid_cols": [H.index(k) for k in kids.get(h, [])],
            "alt_col": None if e["alt_off"] is None else i + e["alt_off"],
            "conf": e["conf"],
            # option labels from the "Question/Option" columns, in form order
            "options": [k[len(h):].lstrip()[1:].strip() for k in kids.get(h, [])],
            "multi": False,
        }
        if m["options"]:
            m["multi"] = _is_multi(df, h, col, m["kid_cols"], m["options"])
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
    _crop_tracker(df, mapping)
    return mapping


# ----------------------------------------------------------------- crop tracker (Q27)
# The form has one block per crop ("27 a1 kharif (Paddy)": Area, Yield, ...) that appears when the
# crop is ticked in the season's "... Crop Name" question. Older form versions instead had one set
# per season plus a free-text "Crop Name", and the "all versions" export spreads those values over
# odd columns (Rabi yield in "Yield (Quintal/Acre).11", Rabi area in "Area (Acres).35", ...). So for
# each season we tick the crop named in the text and fill the values into that crop's block.

CROP_ALIASES = [  # free-text spellings seen in the Excel -> crop option label on the form
    (r"pad+[a-z]?y|padfy|\badt\b|\bcr ?\d{3,4}\b|\bn?dlr\b|\bnlr\b|\brice\b", "Paddy"),
    (r"\bg\.? ?nuts?\b|grou?n?d ?nuts?|^ground$", "Groundnut"),
    (r"topica|cassava", "Tapioca"),
    (r"^cash", "Cashewnut"),
    (r"mosa?m?bi|sweet ?lime", "Mosambi"),
    (r"totapuri|mango", "Mango"),
    (r"అరటి", "Banana"),
    (r"\bgram\b|\bdh?all?\b", "Pulses"),
]


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def resolve_crops(text, vocab):
    """Free-text crop name(s) -> (crop option labels, parts that match no crop)."""
    crops, unknown = [], []
    for part in re.split(r",|/|&|\+|\band\b", str(text), flags=re.I):
        part = part.strip()
        if not re.sub(r"[\W_]+", "", part):  # only punctuation, e.g. "_"
            continue
        c = _compact(part)
        hit = next((v for v in vocab if c and (c == _compact(v) or c.startswith(_compact(v)))), None)
        if hit is None:
            hit = next((crop for rx, crop in CROP_ALIASES if re.search(rx, part, re.I)), None)
        if hit is None:
            unknown.append(part)
        elif hit not in crops:
            crops.append(hit)
    return crops, unknown


def _rows_with_data(df, j):
    return {r for r, v in enumerate(df.iloc[:, j]) if not is_blank(v)}


def _crop_tracker(df, mapping):
    H = [str(c) for c in df.columns]
    seasons = [m for m in mapping if m["options"] and not m.get("yes_no")
               and re.search(r"crop name$", m["header"], re.I)]
    names = sorted((split_instance(h)[1], j) for j, h in enumerate(H) if split_instance(h)[0].lower() == "crop name")
    if not seasons or len(names) != len(seasons):
        return
    if any(not _blank_col(df, m["col"]) for m in seasons):
        return  # the export already has the crop ticks of the current form; nothing to repair

    pos = {m["header"]: k for k, m in enumerate(mapping)}
    starts = [pos[m["header"]] for m in seasons]
    # value labels of a season block: the plain questions right after the first season question
    slot_labels = []
    for m in mapping[starts[0] + 1:]:
        q = split_instance(m["header"])[0]
        if m["options"] or q in slot_labels:
            break
        slot_labels.append(q)
    in_slot = lambda m: split_instance(m["header"])[0] in slot_labels
    spans = []
    for k, s in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(mapping)
        span = []
        for m in mapping[s + 1:end]:
            if not in_slot(m):
                if k + 1 < len(starts):
                    continue
                break
            span.append(m)
        spans.append(span)
    after = pos[spans[-1][-1]["header"]] if spans[-1] else starts[-1]
    trailing = [m for m in mapping[after + 1:] if in_slot(m)]

    vocab = [o for m in seasons for o in m["options"]]
    for k, season in enumerate(seasons):
        crop_col = names[k][1]
        crop_rows = _rows_with_data(df, crop_col)
        cols = []
        for q in slot_labels:
            best, score = None, 0.0
            for m in spans[k] + trailing:
                if split_instance(m["header"])[0] != q or m["conf"] == "tracker":
                    continue
                rows = _rows_with_data(df, m["col"])
                j = len(rows & crop_rows) / len(rows | crop_rows) if rows else 0.0
                if j > score:
                    best, score = m, j
            if best is not None:
                cols.append((best["header"], q, best["col"]))
        season["tracker"] = {"crop_col": crop_col, "cols": cols, "vocab": vocab}
        for m in spans[k]:
            m["conf"] = "tracker"
    for m in trailing:
        m["conf"] = "tracker"
    for m in mapping:
        if m["col"] in [j for _, j in names]:
            m["conf"] = "tracker"  # the free-text Crop Name is not a question of the form


def _tracker_fields(df, m, row):
    """Fields of one season: tick the crop, then fill its block (located on the form by crop)."""
    t = m["tracker"]
    h = m["header"]
    question = split_instance(h)[0]
    crop_text = clean_value(h, _cell(row, t["crop_col"]))
    values = [(hdr, q, clean_value(hdr, _cell(row, j))) for hdr, q, j in t["cols"]]
    values = [v for v in values if v[2]]
    if not crop_text and not values:
        return []
    crops, unknown = resolve_crops(crop_text, t["vocab"])
    options = [[slug(o), o] for o in m["options"]]
    options += [[slug(c), c] for c in crops if slug(c) not in [o[0] for o in options]]
    note = ""
    if not crop_text:
        note = "The Excel has values for this season but no crop name; pick the crop."
    elif unknown or not crops:
        note = f"The Excel crop name '{crop_text}' is not one of the form's crops; pick the crop."
    elif len(crops) > 1:
        note = (f"The Excel crop name '{crop_text}' names {len(crops)} crops, but there is only one set "
                f"of Area/Yield values; keep the one crop they belong to.")
    out = [{
        "header": h, "question": question, "instance": 0,
        "value": " ".join(slug(c) for c in crops) if crops and not unknown else crop_text,
        "excel_value": crop_text, "flags": [], "suggestion": "", "note": note,
        "kind": "checkbox", "options": options, "group": "",
    }]
    for hdr, q, v in values:
        out.append({
            "header": hdr, "question": q, "instance": 0, "value": v, "excel_value": v,
            "flags": [], "suggestion": "", "note": "", "kind": "", "options": [],
            "group": question,  # filled inside the block of the crop ticked in `question`
        })
    return out


def _is_multi(df, header, col, kid_cols, options):
    """Tick-all-that-apply question? (header says so, or some row has several answers)."""
    if re.search(r"tick all|select all|all that apply", header, re.I):
        return True
    opt_slugs = [slug(o) for o in options]
    for r in range(len(df)):
        row = df.iloc[r]
        value = clean_value(header, _cell(row, col))
        if value and len(choice_selected(value, None, options)) > 1:
            return True
        if sum(is_option_selected(_cell(row, j), opt_slugs[k] if opt_slugs and k < len(opt_slugs) else None) for k, j in enumerate(kid_cols)) > 1:
            return True
    return False


def choice_selected(value, flags, options):
    """
    Indexes of the options an Excel answer picks: by option label ("labels" export) or by XML
    choice name, combined with the 0/1 columns.
    """
    picked = parse_labels(value, options)
    if picked is None:
        names = [slug(o) for o in options]
        picked = []
        for t in value.split():
            for i, n in enumerate(names):
                if i not in picked and _is_choice_name(t, [n]):
                    picked.append(i)
                    break
    if flags:
        for i, f in enumerate(flags):
            if f and i < len(options) and i not in picked:
                picked.append(i)
    return picked


def _cell(row, j):
    return row.iloc[j] if j is not None and 0 <= j < len(row) else None


def is_option_selected(v, opt_slug=None):
    if is_blank(v):
        return False
    s = str(v).strip().lower()
    if s in ("0", "false", "no", "none", "nan"):
        return False
    if s in ("1", "true", "yes"):
        return True
    if opt_slug:
        st = slug(s)
        return st == opt_slug or _is_choice_name(s, [opt_slug])
    return True


def row_fields(df, mapping, r):
    """Fields of data row r that have something to fill, in form order."""
    row = df.iloc[r]
    out = []
    for m in mapping:
        if m["conf"] in ("skip", "phantom", "tracker"):
            continue
        if "tracker" in m:
            out += _tracker_fields(df, m, row)
            continue
        h = m["header"]
        value = clean_value(h, _cell(row, m["col"]))
        options = [[slug(o), o] for o in m.get("options", [])]
        opt_slugs = [o[0] for o in options]
        flags = [is_option_selected(_cell(row, j), opt_slugs[k] if k < len(opt_slugs) else None) for k, j in enumerate(m["kid_cols"])]
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
        kind = ""
        if m["options"]:
            # until the live form tells us its exact options, offer the Excel's own option columns;
            # option values are slugs of the labels, which the filler matches against the form
            kind = "checkbox" if m["multi"] else "radio"
            picked = choice_selected(value, flags, m["options"])
            if not m["multi"] and len(picked) > 1:
                primary = [i for i in picked if _is_choice_name(value, [opt_slugs[i]])]
                picked = primary[:1] if primary else picked[:1]
            if picked:
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
            "group": "",
        })
    return out


def row_title(fields, row_no):
    for f in fields:
        if "name of the beneficiary" in f["question"].lower() and f["value"]:
            return f["value"]
    return f"Row {row_no + 1}"
