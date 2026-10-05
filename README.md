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

## How `ter.:` is read

Rows that meet on specific dates carry them inline, and the list ends with the
classroom:

```
ter.: 18,25.11; 2,9,16.12; 10     ->  five dates, in sala 10
```

Every date carries a month and every `ter.:` list in both published plans ends
in a room (`10`, `18t`, `19t`, `113t`, `204t`, `Aula`), so a trailing segment
without a dot is the room.

This matters because the two readings look identical and produce different
calendars. Reading `10` as a date — the natural first guess, since it follows
`2,9,16.12` — yields a sixth session on 23.12, which the university never
listed. The parser treats it as the room instead, so *Metody i języki
programowania* appears on 18.11, 25.11, 2.12, 9.12 and 16.12 and nothing else.

A room is never guessed and a date is never invented: if a `ter.:` list cannot
be read, the run warns and says so rather than inventing a plausible date.
