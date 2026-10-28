"""
Reads keyword exports (built for Wincher, but works with most rank trackers).

Accepts CSV/TSV (any common delimiter or encoding) and .xlsx files, using only
the standard library. Returns the header row, the data rows and a best guess
at which columns hold the keyword, position, ranking URL, search volume and
group, which the dashboard lets you correct.
"""

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_ROWS = 10000
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class ImportError_(ValueError):
    """A problem with the file that the user can fix; the message is shown as-is."""


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_("The file's text encoding wasn't recognised.")


def read_csv(data):
    text = _decode(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (",", ";", "\t")}
        delimiter = max(counts, key=counts.get)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return None
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_("That Excel file couldn't be opened. Try exporting as CSV instead.")
    names = set(z.namelist())
    m = f"{{{SHEET_NS}}}"

    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{m}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{m}t")))

    # First worksheet, found through the workbook relationships.
    sheet_path = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = wb.find(f"{m}sheets/{m}sheet")
        rid = first.get(f"{{{REL_NS}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.iter(f"{{{PKG_REL_NS}}}Relationship"):
            if rel.get("Id") == rid:
                target = rel.get("Target")
                sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    except (KeyError, AttributeError, ET.ParseError):
        pass
    if sheet_path not in names:
        raise ImportError_("No worksheet was found in that Excel file.")

    rows = []
    for row in ET.fromstring(z.read(sheet_path)).iter(f"{m}row"):
        values, next_col = {}, 0
        for c in row.findall(f"{m}c"):
            col = _col_index(c.get("r"))
            col = next_col if col is None else col
            next_col = col + 1
            kind, v = c.get("t"), c.find(f"{m}v")
            if kind == "s":
                val = shared[int(v.text)] if v is not None and v.text else ""
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{m}t"))
            else:
                val = v.text if v is not None and v.text is not None else ""
                if re.fullmatch(r"-?\d+\.0+", val):  # 3.0 -> 3
                    val = val.split(".")[0]
            values[col] = val
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])
        if len(rows) > MAX_ROWS + 20:
            break
    return rows


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

KEYWORD_NAMES = {"keyword", "keywords", "query", "search term", "search query", "term"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}")
POSITION_EXCLUDE = ("change", "diff", "best", "previous", "prev", "avg", "average",
                    "highest", "lowest", "start", "competitor", "difference", "trend")


def _norm(h):
    return re.sub(r"[^a-z0-9 ]", "", (h or "").strip().lower().replace("_", " "))


def find_header_row(rows):
    for i, row in enumerate(rows[:20]):
        if any(_norm(c) in KEYWORD_NAMES for c in row):
            return i
    for i, row in enumerate(rows[:20]):
        if sum(1 for c in row if c.strip()) >= 2:
            return i
    return 0


def detect_columns(headers):
    names = [_norm(h) for h in headers]

    def best(scorer):
        scored = [(scorer(n), i) for i, n in enumerate(names)]
        scored = [x for x in scored if x[0] > 0]
        return max(scored)[1] if scored else None

    def keyword_score(n):
        if n in ("keyword", "keywords"):
            return 100
        return 60 if n in KEYWORD_NAMES else (20 if "keyword" in n else 0)

    def position_score(n):
        if any(x in n for x in POSITION_EXCLUDE):
            return 0
        if n in ("position", "current position", "pos", "rank", "ranking", "current rank"):
            return 100
        if "position" in n or n.startswith("rank"):
            return 50
        return 0

    def url_score(n):
        if n in ("url", "ranking url", "landing page", "ranking page", "page url",
                 "top ranking page", "top ranking url"):
            return 100
        if "status" in n:
            return 0
        if "competitor" in n:
            return 0
        return 50 if ("url" in n or "landing" in n) else (10 if n == "page" else 0)

    def volume_score(n):
        return 100 if n in ("search volume", "volume") else (50 if "volume" in n else 0)

    def group_score(n):
        return 100 if n in ("group", "groups", "tags", "tag", "keyword group") else 0

    def status_score(n):
        return 100 if n in ("change status", "status", "position status") else 0

    detected = {
        "keyword": best(keyword_score),
        "position": best(position_score),
        "url": best(url_score),
        "volume": best(volume_score),
        "group": best(group_score),
        "status": best(status_score),
    }
    # Position history exports have one column per date: use the latest.
    if detected["position"] is None:
        dated = [i for i, h in enumerate(headers) if DATE_RE.match((h or "").strip())]
        if dated:
            detected["position"] = max(dated, key=lambda i: headers[i].strip())
    if detected["keyword"] is None and headers:
        detected["keyword"] = 0
    return detected


def parse_upload(filename, data):
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise ImportError_("Old .xls files aren't supported. Export as CSV or .xlsx instead.")
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        rows = read_xlsx(data)
    else:
        rows = read_csv(data)

    rows = [[(c or "").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ImportError_("That file is empty.")

    h = find_header_row(rows)
    headers = rows[h]
    width = max(len(r) for r in rows[h:h + MAX_ROWS + 1])
    headers = headers + [""] * (width - len(headers))
    headers = [hd or f"Column {i + 1}" for i, hd in enumerate(headers)]
    body = [r + [""] * (width - len(r)) for r in rows[h + 1:h + 1 + MAX_ROWS]]
    if not body:
        raise ImportError_("No keyword rows were found under the header row.")
    return {"filename": filename, "headers": headers, "rows": body,
            "detected": detect_columns(headers), "truncated": len(rows) - h - 1 > MAX_ROWS}
