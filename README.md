# ANS Konin AiR 2026/27 — Automatyka 1 — calendar

Converts the published timetable PDF into an iCalendar feed that Google Calendar
and Apple Calendar can subscribe to, so the schedule updates itself instead of
being re-imported by hand every time the university changes it.

Updates run every Monday 05:00 UTC and also on demand via **Actions →
Timetable PDF -> ICS → Run workflow**.

## Subscribe

**Google Calendar** — open [calendar.google.com](https://calendar.google.com),
click **+** next to *Other calendars* → **From URL**, then paste:

```
https://raw.githubusercontent.com/proxer05/ans-calendar/main/plan.ics
```

**Apple Calendar** — click this link on macOS or iOS (it opens the subscribe
sheet directly):

```
webcal://raw.githubusercontent.com/proxer05/ans-calendar/main/plan.ics
```

If your browser doesn't hand the link to Calendar, subscribe manually:

- **macOS** — Calendar → *File* → *New Calendar Subscription* → paste the
  `webcal://` URL → **Add**.
- **iOS** — *Settings* → *Apps* → *Calendar* → *Accounts* → *Add Account* →
  *Other* → *Add Subscribed Calendar* → paste the `webcal://` URL.

## Download a copy

Latest build as a file (no account needed), also on the
[releases page](https://github.com/proxer05/ans-calendar/releases):

```
https://github.com/proxer05/ans-calendar/releases/latest/download/plan.ics
```

Use this for a one-off import. Use the `raw.githubusercontent.com` URL above if
you want a feed that keeps itself up to date — see below for why the two are
not interchangeable.

## Why two URLs

They are not the same thing, and only one of them works as a subscription.

A GitHub *release asset* is served as `application/octet-stream` with a
`Content-Disposition: attachment` header, out of a CDN cache with no
`Cache-Control` of its own. Calendar clients that poll such a URL can refuse it,
and can keep serving the previously cached copy after the asset is replaced.

A file committed to this repository is served from
`raw.githubusercontent.com` as `text/plain` with `max-age=300`. That is the
shape calendar clients expect, so it re-fetches and picks up changes.

So: **release = download the file. Repository = subscribe to it.**

## Regenerate locally

```sh
pip install -r requirements.txt
curl -o plan.pdf "https://ans.konin.pl/images/MiA/AiR%20plany%202026_2027/plan%20automatyka%201.pdf"
python plan_to_ics.py plan.pdf -o plan.ics --dump-text plan.txt --calname "AiR rok I sem I 2026/27"
```

`plan.txt` is the layout-normalised text the parser sees — useful when the
university changes the PDF and the event count suddenly drops.

## Known source typo

The PDF's own legend for *Metody i języki programowania* ends with a bare `10`
(`ter.: 18,25.11; 2,9,16.12; 10`). That can only be a 23 December, since 10
January 2027 is a Sunday. The parser continues the weekly series as
**23.12.2026** and prints a warning. Every run reports this, so it stays
visible rather than silently becoming a hardcoded date.