import os
import re
import urllib.request
from datetime import datetime, date, time, timedelta
import pdfplumber
from icalendar import Calendar, Event
import pytz

PDF_URL = "https://ans.konin.pl/images/MiA/AiR%20plany%202026_2027/plan%20automatyka%201.pdf"
LOCAL_PDF = "plan.pdf"
OUTPUT_ICS = "plan_automatyka_1.ics"
TZ = pytz.timezone("Europe/Warsaw")

# Zakres semestru zimowego (do generowania powtarzalnych zajęć)
SEMESTER_START = date(2026, 10, 1)
SEMESTER_END = date(2027, 2, 15)

DAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek"]

def download_pdf(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
        f.write(resp.read())

def parse_explicit_dates(text, default_year=2026):
    """Wyciąga daty z formatu np.: ter.: 14,21,28.10; 4,18.11; 2,9,16.12; 13,20.01"""
    found_dates = []
    # Szukamy fragmentów po 'ter.' lub 'terminy:'
    ter_match = re.search(r'ter(?:m|\.|\:)?\s*:?\s*([^;\n]+(?:\s*;\s*[^;\n]+)*)', text, re.IGNORECASE)
    if not ter_match:
        return found_dates

    raw = ter_match.group(1)
    # Rozbijamy po średnikach lub grupach z kropką (np. "14,21.10", "4,18.11")
    chunks = re.findall(r'(\d[\d\s,]*)\.(\d{1,2})', raw)
    for days_str, month_str in chunks:
        m = int(month_str)
        yr = default_year if m >= 9 else default_year + 1
        days = re.findall(r'\d+', days_str)
        for d in days:
            try:
                found_dates.append(date(yr, m, int(d)))
            except ValueError:
                pass
    return found_dates

def parse_time_range(text):
    """Wyciąga godzinę startu i końca, np. 8.00-9.30 lub 08:00 - 09:30"""
    m = re.search(r'(\d{1,2})[\.:](\d{2})\s*[-–]\s*(\d{1,2})[\.:](\d{2})', text)
    if m:
        return time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
    return None, None

def clean_subject_text(text):
    """Czyści tekst komórki z powtórzonych godzin czy śmieci."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    cleaned = []
    for l in lines:
        # Usuń linie będące samymi godzinami
        if re.match(r'^\d{1,2}[\.:]\d{2}\s*[-–]\s*\d{1,2}[\.:]\d{2}$', l):
            continue
        cleaned.append(l)
    return " ".join(cleaned)

def parse_pdf(file_path):
    cal = Calendar()
    cal.add('prodid', '-//ANS Konin Schedule Parser//EN')
    cal.add('version', '2.0')

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 3,
                "join_tolerance": 3,
            })

            # Jeśli standardowe linie nie wykryją tabeli, fallback na 'text'
            if not tables:
                tables = page.extract_tables()

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Szukamy wiersza nagłówka z dniami tygodnia
                header_idx = -1
                col_day_map = {}

                for r_idx, row in enumerate(table):
                    row_str = " ".join([str(c or '').lower() for c in row])
                    matched_days = [d for d in DAYS if d in row_str]
                    if len(matched_days) >= 2:
                        header_idx = r_idx
                        # Mapuj indeks kolumny na dzień tygodnia (0=Poniedziałek, 4=Piątek)
                        for c_idx, cell in enumerate(row):
                            c_text = (cell or '').lower()
                            for d_num, d_name in enumerate(DAYS):
                                if d_name in c_text:
                                    col_day_map[c_idx] = d_num
                        break

                if header_idx == -1:
                    continue

                # Przechodzimy po kolejnych wierszach z zajęciami
                for row in table[header_idx + 1:]:
                    if not row or not any(row):
                        continue

                    # Sprawdź, czy wiersz nie jest stopką z legendą
                    first_cell = str(row[0] or '')
                    if "tygodnie" in first_cell.lower() or "legenda" in first_cell.lower():
                        continue

                    # Pierwsza lub druga kolumna zazwyczaj zawiera godziny
                    row_time_start, row_time_end = None, None
                    for c in row[:2]:
                        s, e = parse_time_range(str(c or ''))
                        if s and e:
                            row_time_start, row_time_end = s, e
                            break

                    # Przeglądamy kolumny z dniami
                    for c_idx, cell in enumerate(row):
                        if c_idx not in col_day_map or not cell:
                            continue

                        cell_text = cell.strip()
                        if len(cell_text) < 4:  # ignorujemy puste/pojedyncze znaki typu "k"
                            continue

                        weekday = col_day_map[c_idx]

                        # Czy wewnątrz komórki jest nadpisana inna godzina?
                        cell_start, cell_end = parse_time_range(cell_text)
                        t_start = cell_start or row_time_start
                        t_end = cell_end or row_time_end

                        if not t_start or not t_end:
                            continue

                        cleaned_title = clean_subject_text(cell_text)
                        explicit_dates = parse_explicit_dates(cell_text)

                        # Jeśli są jawnie wpisane terminy (ter.: ...)
                        if explicit_dates:
                            target_dates = explicit_dates
                        else:
                            # Generuj cotygodniowe zajęcia dla danego dnia tygodnia
                            target_dates = []
                            cur = SEMESTER_START
                            # Dopasuj do pierwszego wystąpienia danego dnia tygodnia
                            cur += timedelta(days=(weekday - cur.weekday()) % 7)

                            is_tn = " TN " in f" {cell_text} " or "TN" in cell_text
                            is_tp = " TP " in f" {cell_text} " or "TP" in cell_text

                            week_counter = 1
                            while cur <= SEMESTER_END:
                                # Uproszczona parzystość: co 2 tygodnie jeśli zaznaczono TN/TP
                                if is_tn and (week_counter % 2 != 1):
                                    pass
                                elif is_tp and (week_counter % 2 != 0):
                                    pass
                                else:
                                    target_dates.append(cur)
                                cur += timedelta(days=7)
                                week_counter += 1

                        for d in target_dates:
                            ev = Event()
                            ev.add('summary', cleaned_title)
                            ev.add('dtstart', TZ.localize(datetime.combine(d, t_start)))
                            ev.add('dtend', TZ.localize(datetime.combine(d, t_end)))
                            cal.add_component(ev)

    with open(OUTPUT_ICS, 'wb') as f:
        f.write(cal.to_ical())
    print(f"Pomyślnie wygenerowano plik: {OUTPUT_ICS}")

if __name__ == "__main__":
    download_pdf(PDF_URL, LOCAL_PDF)
    parse_pdf(LOCAL_PDF)
