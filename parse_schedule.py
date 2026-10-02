import os
import re
import urllib.request
from datetime import datetime, time
import pdfplumber
from icalendar import Calendar, Event
import pytz

PDF_URL = "https://ans.konin.pl/images/MiA/AiR%20plany%202026_2027/plan%20automatyka%201.pdf"
LOCAL_PDF = "plan.pdf"
OUTPUT_ICS = "plan_automatyka_1.ics"
TZ = pytz.timezone("Europe/Warsaw")

DAY_MAP = {
    "poniedziałek": 0, "pon": 0,
    "wtorek": 1, "wt": 1,
    "środa": 2, "śr": 2,
    "czwartek": 3, "czw": 3,
    "piątek": 4, "pt": 4
}

def download_pdf(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
        f.write(resp.read())

def parse_week_ranges(text):
    """Wyciąga daty tygodni TN i TP ze stopki/opisu planu."""
    tn_dates = []
    tp_dates = []
    
    tn_match = re.search(r'TN\s*–\s*tygodnie nieparzyste:\s*([^\n;]+)', text, re.IGNORECASE)
    tp_match = re.search(r'TP\s*–\s*tygodnie parzyste:\s*([^\n;]+)', text, re.IGNORECASE)

    def extract_dates(raw_str):
        dates = []
        parts = [p.strip() for p in raw_str.split(';') if p.strip()]
        for p in parts:
            # Zakresy np. 12-16.10.2026 lub 1-2.10.2026
            m_range = re.match(r'(\d+)-(\d+)\.(\d{1,2})\.(\d{4})', p)
            if m_range:
                d_start, d_end, month, year = map(int, m_range.groups())
                for day in range(d_start, d_end + 1):
                    dates.append(datetime(year, month, day).date())
                continue
            # Pojedyncza data np. 4.11.2026
            m_single = re.match(r'(\d+)\.(\d{1,2})\.(\d{4})', p)
            if m_single:
                day, month, year = map(int, m_single.groups())
                dates.append(datetime(year, month, day).date())
        return dates

    if tn_match:
        tn_dates = extract_dates(tn_match.group(1))
    if tp_match:
        tp_dates = extract_dates(tp_match.group(1))
        
    return set(tn_dates), set(tp_dates)

def parse_pdf(file_path):
    cal = Calendar()
    cal.add('prodid', '-//ANS Konin Schedule Parser//EN')
    cal.add('version', '2.0')

    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])
        tn_dates, tp_dates = parse_week_ranges(full_text)

        # Przykładowy regex wykrywający bloki zajęć:
        # np. "8.00-9.30 Matematyka - dr Kowalski TN 204t"
        pattern = re.compile(
            r'(\d{1,2})[\.:](\d{2})\s*-\s*(\d{1,2})[\.:](\d{2})\s+([^\n\r]+)',
            re.MULTILINE
        )

        for match in pattern.finditer(full_text):
            h_start, m_start, h_end, m_end, content = match.groups()
            content = content.strip()

            # Domyślnie bierzemy rok akademicki z kontekstu
            academic_year = 2026

            is_tn = " TN " in f" {content} "
            is_tp = " TP " in f" {content} "

            # Sprawdzenie konkretnych terminów typu "ter.: 14,21,28.10; 18.11;"
            explicit_dates = []
            ter_match = re.search(r'ter\.?:\s*([0-9\.,;\s]+)', content, re.IGNORECASE)
            if ter_match:
                ter_str = ter_match.group(1)
                for chunk in ter_str.split(';'):
                    if '.' in chunk:
                        days_part, month_part = chunk.strip().split('.')
                        m = int(month_part)
                        yr = academic_year if m >= 9 else academic_year + 1
                        for d in re.findall(r'\d+', days_part):
                            explicit_dates.append(datetime(yr, m, int(d)).date())

            target_dates = explicit_dates
            if not target_dates:
                # W przypadku braku konkretnych dat przypisz wg TN / TP lub wszystkie
                if is_tn:
                    target_dates = list(tn_dates)
                elif is_tp:
                    target_dates = list(tp_dates)
                else:
                    target_dates = list(tn_dates.union(tp_dates))

            for d in target_dates:
                ev = Event()
                ev.add('summary', content)
                ev.add('dtstart', TZ.localize(datetime.combine(d, time(int(h_start), int(m_start)))))
                ev.add('dtend', TZ.localize(datetime.combine(d, time(int(h_end), int(m_end)))))
                cal.add_component(ev)

    with open(OUTPUT_ICS, 'wb') as f:
        f.write(cal.to_ical())
    print(f"Saved calendar to {OUTPUT_ICS}")

if __name__ == "__main__":
    download_pdf(PDF_URL, LOCAL_PDF)
    parse_pdf(LOCAL_PDF)
