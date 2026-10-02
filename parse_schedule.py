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

DAY_NAMES = {
    "poniedziałek": 0,
    "wtorek": 1,
    "środa": 2,
    "czwartek": 3,
    "piątek": 4,
}

def download_pdf(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
        f.write(resp.read())

def parse_date_ranges(line_str):
    """
    Parsuje zakresy dat ze stopki TN/TP, np:
    '1-2.10.2026; 12-16.10.2026; 7-8.01.2027;'
    """
    dates = []
    chunks = [c.strip() for c in line_str.split(';') if c.strip()]
    for chunk in chunks:
        # Zakres dni: DD-DD.MM.YYYY
        m_range = re.search(r'(\d+)\s*-\s*(\d+)\.(\d{1,2})\.(\d{4})', chunk)
        if m_range:
            d_start, d_end, month, year = map(int, m_range.groups())
            for d in range(d_start, d_end + 1):
                try:
                    dates.append(date(year, month, d))
                except ValueError:
                    pass
            continue
        # Pojedynczy dzień: DD.MM.YYYY
        m_single = re.search(r'(\d+)\.(\d{1,2})\.(\d{4})', chunk)
        if m_single:
            d, month, year = map(int, m_single.groups())
            try:
                dates.append(date(year, month, d))
            except ValueError:
                pass
    return dates

def extract_footer_weeks(text):
    """Wyciąga oficjalny zbiór dat dla TN i TP ze stopki dokumentu."""
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
    """
    Wyciąga daty z dopisku 'ter.: 18,25.11; 2,9,16.12; 10.01' lub 'ter.: 2,16.10; 13,27.11; 8.01;'
    """
    dates = []
    ter_match = re.search(r'ter\.?:\s*([^\n\r]+)', text, re.IGNORECASE)
    if not ter_match:
        return dates

    raw_part = ter_match.group(1).split("Aula")[0].strip()
    # Szukamy fragmentów typu '18,25.11' lub '8.01'
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

def extract_time_from_str(text):
    m = re.search(r'(\d{1,2})[\.:](\d{2})\s*[-–]\s*(\d{1,2})[\.:](\d{2})', text)
    if m:
        return time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
    return None, None

def clean_subject(text):
    # Usunięcie godzin na początku wpisu, jeśli występują
    cleaned = re.sub(r'^\d{1,2}[\.:]\d{2}\s*[-–]\s*\d{1,2}[\.:]\d{2}\s*', '', text.strip())
    # Usunięcie powtórzonych spacji i nowych linii
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned

def parse_schedule():
    with pdfplumber.open(LOCAL_PDF) as pdf:
        full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])
        tn_dates, tp_dates = extract_footer_weeks(full_text)
        all_semester_dates = tn_dates.union(tp_dates)

        cal = Calendar()
        cal.add('prodid', '-//ANS Konin AiR Calendar//PL')
        cal.add('version', '2.0')

        page = pdf.pages[0]
        # Wyciągamy tabele bez narzucania sztywnych linii pionowych (obsługa komórek scalonych)
        tables = page.extract_tables()

        current_day = None

        for table in tables:
            for row in table:
                if not row or not any(row):
                    continue

                # Normalizacja komórek
                cells = [c.strip() if c else "" for c in row]
                first_col = cells[0].lower()

                # Ignorowanie nagłówków i stopki
                if "kierunek" in first_col or "plan zajęć" in first_col or "tn – tygodnie" in first_col:
                    continue

                # Sprawdzenie czy wiersz określa dzień tygodnia
                for d_name, d_id in DAY_NAMES.items():
                    if d_name in first_col:
                        current_day = d_id
                        break

                if current_day is None:
                    continue

                # Sprawdzenie domyślnej godziny z wiersza (np. w kolumnie 1 lub 0)
                row_start, row_end = None, None
                for col_idx in [0, 1]:
                    if col_idx < len(cells):
                        s, e = extract_time_from_str(cells[col_idx])
                        if s and e:
                            row_start, row_end = s, e
                            break

                if not row_start:
                    continue

                # Komórki z zajęciami (od kolumny 1 wzwyż)
                subject_cells = set()
                for c in cells[1:]:
                    if not c:
                        continue
                    # Pomijamy komórkę będącą wyłącznie samą godziną
                    if re.match(r'^\d{1,2}[\.:]\d{2}\s*[-–]\s*\d{1,2}[\.:]\d{2}$', c.strip()):
                        continue
                    subject_cells.add(c)

                for cell_content in subject_cells:
                    if len(cell_content) < 4:
                        continue

                    # Sprawdzamy czy komórka nadpisuje godziny (np. 10.00-11.30 Siłownia)
                    custom_start, custom_end = extract_time_from_str(cell_content)
                    start_time = custom_start or row_start
                    end_time = custom_end or row_end

                    # Wyznaczenie konkretnych dat
                    explicit = parse_explicit_dates(cell_content)
                    if explicit:
                        target_dates = explicit
                    else:
                        is_tn = bool(re.search(r'\bTN\b', cell_content))
                        is_tp = bool(re.search(r'\bTP\b', cell_content))

                        if is_tn:
                            target_dates = [d for d in tn_dates if d.weekday() == current_day]
                        elif is_tp:
                            target_dates = [d for d in tp_dates if d.weekday() == current_day]
                        else:
                            # Zajęcia odbywające się co tydzień
                            target_dates = [d for d in all_semester_dates if d.weekday() == current_day]

                    title = clean_subject(cell_content)

                    for event_date in sorted(target_dates):
                        event = Event()
                        event.add('summary', title)
                        event.add('dtstart', TZ.localize(datetime.combine(event_date, start_time)))
                        event.add('dtend', TZ.localize(datetime.combine(event_date, end_time)))
                        cal.add_component(event)

        with open(OUTPUT_ICS, 'wb') as f:
            f.write(cal.to_ical())
        print(f"Wygenerowano poprawny plik {OUTPUT_ICS}")

if __name__ == "__main__":
    download_pdf(PDF_URL, LOCAL_PDF)
    parse_schedule()
