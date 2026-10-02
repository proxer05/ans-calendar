#!/usr/bin/env python3
"""
Convert an ANS Konin timetable PDF ("PLAN ZAJĘĆ") into an .ics file
that can be imported into Google Calendar.

How the PDF is interpreted
--------------------------
The page is a ruled table, so the text is rebuilt from glyph positions rather
than from pdfplumber's plain reading order (the weekday names are rotated, see
`PDF -> normalised text` below).  The result is one line per table row.

* Day headers:  Poniedziałek / Wtorek / Środa / Czwartek / Piątek ...
* Entry lines:  "<grid slot> [<real time>] Subject - teacher - wyk. TN 109t"
    - if a line has two time ranges, the LAST one is the real class time
      (the first one is just the grid row label)
    - TN / TP    -> odd / even weeks, dates come from the legend at the bottom
    - ter.: ...  -> explicit list of dates, e.g. "18,25.11; 2,9,16.12; 10".
                    Segments are chronological, so a segment carrying only a
                    day number ("...; 10") is placed in the surrounding
                    series: first by month rollover, then - if that lands on
                    the wrong weekday - by continuing the series cadence.
    - no marker  -> every teaching day (union of the TN and TP legend dates,
                    so free days / holidays are skipped automatically)
    - room       -> tokens like 109t, 17t, Aula
* Legend lines: "TN – tygodnie nieparzyste: 1-2.10.2026; 12-16.10.2026; ..."

Usage
-----
    python plan_to_ics.py plan.pdf -o plan.ics [--dump-text plan.txt]
    python plan_to_ics.py --from-text plan.txt -o plan.ics     # for debugging
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import date, datetime, timedelta

DAYS = {
    "poniedziałek": 0, "wtorek": 1, "środa": 2, "czwartek": 3,
    "piątek": 4, "sobota": 5, "niedziela": 6,
}
TIME = r"\d{1,2}[.:]\d{2}\s*-\s*\d{1,2}[.:]\d{2}"
TIME_RE = re.compile(rf"^\s*({TIME})\s*")
ROOM_RE = re.compile(r"\b(\d{1,3}t|Aula)\b")
TYPE_TOKEN = r"(?:wyk|ćw|lab|konw|proj|sem)\."
TYPE_RE = re.compile(rf"({TYPE_TOKEN}(?:\s*/\s*{TYPE_TOKEN})*)")
LEGEND_RE = re.compile(r"^\s*(TN|TP)\s*[–-]\s*[^:]*:\s*(.*)$")
RANGE_RE = re.compile(
    r"(\d{1,2})(?:\.(\d{1,2}))?\.?\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})"
)
SINGLE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


# --------------------------------------------------------------------------
# PDF -> normalised text
#
# The plan is a ruled table: a narrow "Godz." column with the grid slot times
# followed by one block of rows per weekday.  The weekday names are printed
# rotated by -90 degrees, which makes plain extract_text() shred them into
# single letters ("k", "e", "ła", "iz") and scatter them through the body
# text, so no day header is ever recognised.  The text is therefore rebuilt
# from glyph positions instead:
#
#   * the drawn horizontal rules give the row bands,
#   * the rotated glyph runs give the weekday labels and where each block sits,
#   * every band becomes one line, read left to right, so the grid slot time
#     comes first and the real class time (when it differs) comes second,
#     which is exactly what parse_text() expects.
# --------------------------------------------------------------------------
CONTENT_RE = re.compile(rf"^\s*{TIME}")


def _is_horizontal(obj) -> bool:
    """pdfplumber's char.upright only knows about page rotation, so look at
    the text matrix itself: horizontal text has a non-zero x scale."""
    m = obj.get("matrix")
    if m is None:
        return True
    return abs(m[0]) > 0.5 or abs(m[1]) < 0.5


def _cluster_lines(words: list[dict], tol: float = 2.0) -> list[dict]:
    """Group words into visual text lines by their top coordinate."""
    rows: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for row in rows:
            if abs(w["top"] - row["top"]) <= tol:
                row["words"].append(w)
                row["top"] = sum(x["top"] for x in row["words"]) / len(row["words"])
                break
        else:
            rows.append({"top": w["top"], "words": [w]})
    return sorted(rows, key=lambda r: r["top"])


def _line_text(row: dict) -> str:
    return " ".join(w["text"] for w in sorted(row["words"], key=lambda w: w["x0"]))


def _horizontal_rules(page) -> list[float]:
    """Y positions of the table's horizontal rules, de-duplicated."""
    wide = page.width * 0.15
    tops = [round(r["top"], 2) for r in page.rects
            if r["x1"] - r["x0"] > wide and r["bottom"] - r["top"] <= 3.0]
    tops += [round(e["top"], 2) for e in page.edges
             if e["orientation"] == "h" and abs(e["x1"] - e["x0"]) > wide]
    out: list[float] = []
    for t in sorted(tops):
        if not out or t - out[-1] > 2.0:
            out.append(t)
    return out


def _rotated_labels(page) -> list[dict]:
    """Vertical (rotated -90 deg) labels: text plus the vertical span they cover."""
    rot = [c for c in page.chars if not _is_horizontal(c)]
    cols: list[list[dict]] = []
    for c in sorted(rot, key=lambda c: c["x0"]):
        if cols and abs(cols[-1][0]["x0"] - c["x0"]) < 3.0:
            cols[-1].append(c)
        else:
            cols.append([c])

    out = []
    for col in cols:
        # glyphs of one label sit within a couple of font sizes of each other,
        # neighbouring labels are far apart
        limit = 2.0 * max(c["size"] for c in col)
        col.sort(key=lambda c: -c["top"])  # rotated text reads bottom-up
        runs: list[list[dict]] = []
        for c in col:
            prev = runs[-1][-1] if runs else None
            if prev is not None and 0.0 <= prev["top"] - c["top"] <= limit:
                runs[-1].append(c)
            else:
                runs.append([c])
        for run in runs:
            txt = "".join(c["text"] for c in run).strip()
            if not txt:
                continue
            lo = min(c["top"] for c in run)
            hi = max(c["bottom"] for c in run)
            out.append({"text": txt, "top": lo, "bottom": hi, "center": (lo + hi) / 2})
    return out


def _layout_page(page) -> list[str]:
    rules = _horizontal_rules(page)
    heads = [h for h in _rotated_labels(page) if h["text"].lower() in DAYS]
    if not rules:
        # not a ruled table - fall back to the plain reading order
        page = page.filter(lambda o: _is_horizontal(o) if "text" in o else True)
        return [_line_text(r) for r in _cluster_lines(page.extract_words())]

    page = page.filter(lambda o: _is_horizontal(o) if "text" in o else True)
    words = page.extract_words()
    lines = [_line_text(r) for r in _cluster_lines(
        [w for w in words if w["bottom"] <= rules[0] + 1])]

    cur: dict | None = None
    for top, bot in zip(rules, rules[1:]):
        cw = [w for w in words if top - 1 <= (w["top"] + w["bottom"]) / 2 <= bot + 1]
        if not cw:
            continue
        rows = [_line_text(r) for r in _cluster_lines(cw)]
        # weekday labels are centred on their block of rows; the header row
        # ("Godz.") belongs to no weekday, so only re-anchor on real entries
        if heads and CONTENT_RE.match(rows[0]):
            near = min(heads, key=lambda h: abs(h["center"] - (top + bot) / 2))
            if cur is None or near["text"] != cur["text"]:
                if cur is None or near["center"] > cur["center"]:
                    lines.append(near["text"])
                    cur = near
        lines.extend(rows)

    lines += [_line_text(r) for r in _cluster_lines(
        [w for w in words if w["top"] >= rules[-1] - 1])]
    return lines


def pdf_to_text(path: str) -> str:
    import pdfplumber

    out: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            out.extend(_layout_page(page))
    return "\n".join(out)


# --------------------------------------------------------------------------
# Date helpers
# --------------------------------------------------------------------------
def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def parse_legend_dates(s: str) -> set[date]:
    """'1-2.10.2026; 30.11.-4.12.2026; ...' -> set of dates."""
    out: set[date] = set()
    for m in RANGE_RE.finditer(s):
        d1, m1, d2, m2, y = m.groups()
        end = date(int(y), int(m2), int(d2))
        start = date(int(y), int(m1 or m2), int(d1))
        if start > end:  # range crossing New Year
            start = date(int(y) - 1, int(m1 or m2), int(d1))
        out.update(daterange(start, end))
    rest = RANGE_RE.sub(" ", s)
    for m in SINGLE_RE.finditer(rest):
        d, mo, y = map(int, m.groups())
        out.add(date(y, mo, d))
    return out


def _resolve_bare_day(day: int, prev: date | None, prev_day: int,
                      prev_month: int, gaps: list[int], expect_wd: int,
                      warnings: list[str], seg: str) -> date | None:
    """Resolve a 'ter.:' segment that is only a day number, e.g. '...; 10'.

    The PDF sometimes drops the month ("2,9,16.12; 10") or garbles it.  Two
    readings are tried: the usual month rollover - the author merely left the
    month out - and, if that lands on the wrong weekday, carrying on with the
    cadence of the surrounding series (weekly lists step by 7 days).  Falling
    back is always reported, because it means the PDF itself is inconsistent.
    """
    if prev is not None and 1 <= day <= 31:
        year, month = prev.year, prev_month
        if day <= prev_day:  # the list moved on to the next month
            month += 1
            if month > 12:
                month, year = 1, year + 1
        try:
            cand = date(year, month, day)
        except ValueError:  # e.g. 30.02
            cand = None
        if cand is not None and cand.weekday() == expect_wd and cand > prev:
            return cand
    if prev is not None and gaps:
        step = max(set(gaps), key=gaps.count)
        nxt = prev + timedelta(days=step)
        if nxt.weekday() == expect_wd:
            warnings.append(
                f"'ter.:' segment {seg!r} has no usable month and lands on the "
                f"wrong weekday - continued the series as {nxt:%d.%m.%Y}")
            return nxt
    warnings.append(
        f"Cannot resolve date segment {seg!r} in 'ter.:' list - skipped")
    return None


def parse_terms(s: str, acad_year: int, expect_wd: int,
                warnings: list[str]) -> set[date]:
    """'18,25.11; 2,9,16.12; 8.01' -> set of dates (Sep-Dec = start year).

    Segments are chronological, which lets a bare day number be placed in the
    surrounding series.  expect_wd is the weekday the class actually meets on.
    """
    out: set[date] = set()
    prev: date | None = None
    prev_day = prev_month = 0
    gaps: list[int] = []

    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        m = re.fullmatch(r"([\d,\s]+)\.(\d{1,2})\.?(?:(\d{4}))?", seg)
        if m:
            month = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else (
                acad_year if month >= 9 else acad_year + 1
            )
            for d in sorted(int(x) for x in re.findall(r"\d+", m.group(1))):
                cur = date(year, month, d)
                if prev is not None and cur > prev:
                    gaps.append((cur - prev).days)
                prev, prev_day, prev_month = cur, d, month
                out.add(cur)
            continue
        if seg.isdigit():
            cur = _resolve_bare_day(int(seg), prev, prev_day, prev_month,
                                    gaps, expect_wd, warnings, seg)
            if cur is None:
                continue
            if prev is not None and cur > prev:
                gaps.append((cur - prev).days)
            prev = cur
            out.add(cur)
            continue
        warnings.append(f"Cannot parse date segment {seg!r} in 'ter.:' list")
    return out


# --------------------------------------------------------------------------
# Text -> entries
# --------------------------------------------------------------------------
def clean_time(t: str) -> tuple[int, int, int, int]:
    h1, m1, h2, m2 = map(int, re.findall(r"\d+", t))
    return h1, m1, h2, m2


def parse_text(text: str, fb_start: date | None, fb_end: date | None):
    warnings: list[str] = []
    lines = [ln.rstrip() for ln in text.splitlines()]

    # academic year
    m = re.search(r"(\d{4})\s*/\s*(\d{4})", text)
    acad_year = int(m.group(1)) if m else None

    # ---- pass 1: legend + raw entries ------------------------------------
    legend: dict[str, str] = {"TN": "", "TP": ""}
    legend_key: str | None = None
    raw: list[dict] = []
    day: int | None = None
    saw_day = False

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue

        lm = LEGEND_RE.match(stripped)
        if lm:
            legend_key = lm.group(1)
            legend[legend_key] += " " + lm.group(2)
            continue
        if legend_key and re.match(r"^[\d\s,;.–-]", stripped):
            # wrapped legend line, but do not swallow real content after it
            legend[legend_key] += " " + stripped
            continue

        low = stripped.lower().rstrip(":")
        if low in DAYS:
            day = DAYS[low]
            saw_day = True
            continue
        if day is None:
            continue

        tm = TIME_RE.match(stripped)
        if tm:
            raw.append({"day": day, "text": stripped})
        elif raw and raw[-1]["day"] == day and not stripped.startswith("Godz"):
            raw[-1]["text"] += " " + stripped  # continuation of wrapped cell

    parity = {k: parse_legend_dates(v) for k, v in legend.items()}
    all_days = parity["TN"] | parity["TP"]

    if not saw_day and any(TIME_RE.match(ln.strip()) for ln in lines):
        warnings.append(
            "No weekday header found - the PDF layout may have changed, "
            "check --dump-text")

    if acad_year is None:
        acad_year = min(all_days).year if all_days else date.today().year

    if not all_days:
        if fb_start and fb_end:
            all_days = set(daterange(fb_start, fb_end))
            warnings.append("No TN/TP legend found - using --start/--end range")
        else:
            warnings.append("No TN/TP legend found and no --start/--end given")

    # ---- pass 2: build events -------------------------------------------
    events = []
    for r in raw:
        rest = r["text"]
        times = []
        while True:
            tm = TIME_RE.match(rest)
            if not tm:
                break
            times.append(tm.group(1))
            rest = rest[tm.end():]
        body = " ".join(rest.split())
        if not body:
            continue  # empty grid row
        h1, m1, h2, m2 = clean_time(times[-1])

        terms_raw = None
        tm = re.search(r"\bter\.?:\s*(.*)$", body)
        room_m = ROOM_RE.search(body)
        room = room_m.group(1) if room_m else ""
        if room_m:
            body = (body[:room_m.start()] + body[room_m.end():]).strip()
            # re-run after room removal so 'ter.:' text does not contain it
            tm = re.search(r"\bter\.?:\s*(.*)$", body)
        if tm:
            terms_raw = tm.group(1)
            body = body[:tm.start()].strip()
        if not room:  # bare trailing room number, e.g. "... Skwarczyńska 109"
            bm = re.search(r"\s(\d{2,3})$", body)
            if bm:
                room = bm.group(1)
                body = body[:bm.start()].strip()

        pm = re.search(r"\b(TN|TP)\b", body)
        par = pm.group(1) if pm else None
        if pm:
            body = (body[:pm.start()] + body[pm.end():]).strip()

        typ = ""
        ty = TYPE_RE.search(body)
        if ty:
            typ = ty.group(1)
            body = (body[:ty.start()] + body[ty.end():]).strip()

        body = re.sub(r"(\s+[-–])+\s*$", "", " ".join(body.split()))
        parts = [p.strip() for p in re.split(r"\s+[-–]\s+", body) if p.strip()]
        if not parts:
            warnings.append(f"Skipped unparsable line: {r['text']!r}")
            continue
        subject, teacher = parts[0], ", ".join(parts[1:])

        if terms_raw:
            dates = sorted(parse_terms(terms_raw, acad_year, r["day"], warnings))
            for d in dates:
                if d.weekday() != r["day"]:
                    warnings.append(
                        f"{subject}: {d} is not the expected weekday"
                    )
            if not dates:
                warnings.append(f"{subject}: 'ter.:' present but no dates parsed")
        elif par:
            dates = sorted(d for d in parity[par] if d.weekday() == r["day"])
        else:
            dates = sorted(d for d in all_days if d.weekday() == r["day"])

        for d in dates:
            events.append({
                "subject": subject, "teacher": teacher, "type": typ,
                "room": room, "parity": par, "date": d,
                "start": datetime(d.year, d.month, d.day, h1, m1),
                "end": datetime(d.year, d.month, d.day, h2, m2),
            })

    events.sort(key=lambda e: e["start"])
    return events, warnings


# --------------------------------------------------------------------------
# ICS writer
# --------------------------------------------------------------------------
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Warsaw
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZNAME:CET
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZNAME:CEST
END:DAYLIGHT
END:VTIMEZONE"""


def esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def build_ics(events, calname: str) -> str:
    # Stable DTSTAMP so the file only changes when the timetable changes.
    stamp = (min(e["start"] for e in events) if events
             else datetime(2000, 1, 1)).strftime("%Y%m%dT000000Z")
    L = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//plan-to-ics//ANS Konin//PL", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{esc(calname)}",
        "X-WR-TIMEZONE:Europe/Warsaw", *VTIMEZONE.splitlines(),
    ]
    for e in events:
        summary = e["subject"] + (f" ({e['type']})" if e["type"] else "")
        desc = []
        if e["teacher"]:
            desc.append(f"Prowadzący: {e['teacher']}")
        if e["parity"]:
            desc.append("Tydzień: " + ("nieparzysty" if e["parity"] == "TN"
                                       else "parzysty"))
        uid_src = f"{e['subject']}|{e['type']}|{e['start'].isoformat()}"
        uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20] + "@plan-to-ics"
        L += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Warsaw:{e['start']:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=Europe/Warsaw:{e['end']:%Y%m%dT%H%M%S}",
            f"SUMMARY:{esc(summary)}",
        ]
        if e["room"]:
            L.append(f"LOCATION:{esc('sala ' + e['room'] if e['room'] != 'Aula' else 'Aula')}")
        if desc:
            L.append(f"DESCRIPTION:{esc(chr(10).join(desc))}")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="timetable PDF")
    ap.add_argument("--from-text", help="parse an already extracted .txt instead")
    ap.add_argument("-o", "--output", default="plan.ics")
    ap.add_argument("--calname", default="Plan zajęć")
    ap.add_argument("--dump-text", help="write the extracted PDF text here")
    ap.add_argument("--start", help="fallback semester start YYYY-MM-DD")
    ap.add_argument("--end", help="fallback semester end YYYY-MM-DD")
    a = ap.parse_args()

    if a.from_text:
        text = open(a.from_text, encoding="utf-8").read()
    elif a.pdf:
        text = pdf_to_text(a.pdf)
    else:
        ap.error("give a PDF path or --from-text")

    if a.dump_text:
        with open(a.dump_text, "w", encoding="utf-8") as f:
            f.write(text)

    fb_s = date.fromisoformat(a.start) if a.start else None
    fb_e = date.fromisoformat(a.end) if a.end else None
    events, warnings = parse_text(text, fb_s, fb_e)

    for w in warnings:
        print(f"::warning::{w}")  # shows up as annotation in GitHub Actions

    if not events:
        print("ERROR: no events produced - check the extracted text", file=sys.stderr)
        return 1

    with open(a.output, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(events, a.calname))

    print(f"Wrote {len(events)} events to {a.output}")
    seen = {}
    for e in events:
        seen.setdefault(e["subject"], 0)
        seen[e["subject"]] += 1
    for k, v in seen.items():
        print(f"  {v:3d} x {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
