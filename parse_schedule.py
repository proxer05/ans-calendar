import os
import re
import urllib.request
from datetime import datetime, date, time
import pdfplumber
from icalendar import Calendar, Event
import pytz

PDF_URL = "https://ans.konin.pl/images/MiA/AiR%20plany%202026_2027/plan%20automatyka%201.pdf"
LOCAL_PDF = "plan.pdf"
OUTPUT_ICS = "plan_automatyka_1.ics"
TZ = pytz.timezone("Europe/Warsaw")

def download_pdf(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
        f.write(resp.read())

def parse_date_ranges(line_str):
    dates = []
    chunks = [c.strip() for c in line_str.split(';') if c.strip()]
    for chunk in chunks:
        m_range = re.search(r'(\d+)\s*-\s*(\d+)\.(\d{1,2})\.(\d{4})', chunk)
        if m_range:
            d_start, d_end, month, year = map(int, m_range.groups())
            for d in range(d_start, d_end + 1):
                try:
                    dates.append(date(year, month, d))
                except ValueError:
                    pass
            continue
        m_single = re.search(r'(\d+)\.(\d{1,2})\.(\d{4})', chunk)
        if m_single:
            d, month, year = map(int, m_single.groups())
            try:
                dates.append(date(year, month, d))
            except ValueError:
                pass
    return dates

def extract_footer_weeks(text):
    tn_dates = set()
    tp_dates = set()
    for line in text.splitlines():
        line_clean = line.strip()
        if "TN – tygodnie nieparzyste" in line_clean or "TN -" in line_clean:
            after_colon = line_clean.split(":", 1)[-1]
            tn_dates.update(parse_date_ranges(after_colon))
        elif "TP – tygodnie parzyste" in line_clean or "TP -" in line_clean:
            after_colon = line_clean.split(":", 1)[-1]
            tp_dates.update(parse_date_ranges(after_colon))
    return tn_dates, tp_dates

def parse_explicit_dates(text, default_year=2026):
    dates = []
    ter_match = re.search(r'ter\.?:\s*([^\n\r]+)', text, re.IGNORECASE)
    if not ter_match:
        return dates

    raw_part = ter_match.group(1).split("Aula")[0].strip()
    segments = re.findall(r'([\d\s,]+)\.(\d{1,2})', raw_part)
    for days_str, month_str in segments:
        m = int(month_str)
        yr = default_year if m >= 9 else default_year + 1
        day_numbers = re.findall(r'\d+', days_str)
        for d in day_numbers:
            try:
                dates.append(date(yr, m, int(d)))
            except ValueError:
                pass
    return dates

def parse_time_override(text, default_start, default_end):
    m = re.search(r'(\d{1,2})[\.:](\d{2})\s*[-–]\s*(\d{1,2})[\.:](\d{2})', text)
    if m:
        return time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
    return default_start, default_end

def parse_schedule():
    with pdfplumber.open(LOCAL_PDF) as pdf:
        full_text = "\n".join([page.extract_text(layout=False) or "" for page in pdf.pages])
        tn_dates, tp_dates = extract_footer_weeks(full_text)
        all_semester_dates = tn_dates.union(tp_dates)

        cal = Calendar()
        cal.add('prodid', '-//ANS Konin AiR Calendar//PL')
        cal.add('version', '2.0')

        page = pdf.pages[0]

        # 1. Wyciągamy wszystkie linie poziome siatki (granice slotów)
        h_lines = sorted(list(set([round(edge['top'], 1) for edge in page.horizontal_edges if edge['width'] > 200])))
        
        # Standardowy układ 5 dni po 6 slotów (lub granice wyznaczone liniami tabeli)
        # Rezerwowy fallback do standardowych 30 slotów (5 dni * 6 godzin)
        DEFAULT_SLOT_TIMES = [
            (time(8, 0), time(9, 30)),
            (time(9, 45), time(11, 15)),
            (time(11, 30), time(13, 0)),
            (time(13, 30), time(15, 0)),
            (time(15, 15), time(16, 45)),
            (time(17, 0), time(18, 30))
        ]

        # Fallback na wypadek gdyby linie nie zostały precyzyjnie wykryte:
        # Wyciągamy tabele z explicit vertical/horizontal strategies
        extracted_tables = page.extract_tables({
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "snap_y_tolerance": 4,
            "intersection_y_tolerance": 4
        })

        if not extracted_tables or len(extracted_tables[0]) < 10:
            # Fallback bez sztywnych linii
            extracted_tables = page.extract_tables()

        # Spłaszczamy wiersze tabeli
        flat_rows = []
        for t in extracted_tables:
            for r in t:
                flat_rows.append([cell.strip() if cell else "" for cell in r])

        # Przypisujemy wiersze do dni (5 dni, każdy ma 6 slotów godzinowych)
        # Filtrujemy wiersze nagłówkowe i stopki
        slot_rows = []
        for r in flat_rows:
            row_str = " ".join(r)
            if "kierunek" in row_str.lower() or "plan zajęć" in row_str.lower() or "tygodnie" in row_str.lower():
                continue
            # Wiersz musi mieć co najmniej godzinę lub treść zajęć
            if any(re.search(r'\d{1,2}[\.:]\d{2}', c) for c in r):
                slot_rows.append(r)

        # Jeśli mamy 30 slotów (6 slotów x 5 dni roboczych)
        for idx, row in enumerate(slot_rows):
            day_idx = min(idx // 6, 4)  # 0=Pn, 1=Wt, 2=Śr, 3=Czw, 4=Pt
            time_idx = idx % 6
            default_start, default_end = DEFAULT_SLOT_TIMES[time_idx]

            # Sprawdzamy komórki z zajęciami
            for cell in set(row):
                if not cell or len(cell) < 4:
                    continue
                # Pomijamy komórkę będącą czystą godziną wiersza (np. 8.00-9.30)
                if re.match(r'^\d{1,2}[\.:]\d{2}\s*[-–]\s*\d{1,2}[\.:]\d{2}$', cell):
                    continue
                if cell.lower() in ["poniedziałek", "wtorek", "środa", "czwartek", "piątek"]:
                    continue

                start_t, end_t = parse_time_override(cell, default_start, default_end)
                explicit = parse_explicit_dates(cell)

                if explicit:
                    target_dates = explicit
                else:
                    is_tn = bool(re.search(r'\bTN\b', cell))
                    is_tp = bool(re.search(r'\bTP\b', cell))

                    if is_tn:
                        target_dates = [d for d in tn_dates if d.weekday() == day_idx]
                    elif is_tp:
                        target_dates = [d for d in tp_dates if d.weekday() == day_idx]
                    else:
                        target_dates = [d for d in all_semester_dates if d.weekday() == day_idx]

                clean_title = re.sub(r'^\d{1,2}[\.:]\d{2}\s*[-–]\s*\d{1,2}[\.:]\d{2}\s*', '', cell).strip()
                clean_title = re.sub(r'\s+', ' ', clean_title)

                for ev_date in sorted(target_dates):
                    ev = Event()
                    ev.add('summary', clean_title)
                    ev.add('dtstart', TZ.localize(datetime.combine(ev_date, start_t)))
                    ev.add('dtend', TZ.localize(datetime.combine(ev_date, end_t)))
                    cal.add_component(ev)

        with open(OUTPUT_ICS, 'wb') as f:
            f.write(cal.to_ical())
        print(f"Wygenerowano ICS z {len(cal.subcomponents)} zdarzeniami.")

if __name__ == "__main__":
    download_pdf(PDF_URL, LOCAL_PDF)
    parse_schedule()
