#!/usr/bin/env python3
"""Print the next few renCal (caldir) events as JSON for the clock panel.

renCal keeps every calendar as a folder of plain .ics files under the caldir
directory (~/caldir unless ~/.config/caldir/config.toml says otherwise), with
each calendar's name and colour in <calendar>/.caldir/config.toml. Recurring
series are expanded here so a weekly standup shows up as its next occurrence.

Output: {"ok": true, "events": [...]} or {"ok": false, "error": "..."}.
"""

import json
import os
import re
import sys
import tomllib
from datetime import date, datetime, time, timedelta, timezone

try:
    import icalendar
    import recurring_ical_events
except ImportError:
    print(json.dumps({"ok": False, "error": "missing-deps"}))
    sys.exit(0)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DAYS_AHEAD = int(args[0]) if len(args) > 0 else 7
LIMIT = int(args[1]) if len(args) > 1 else 8
# Google's working-location entries ("Home", "Office") arrive as all-day
# events on every workday; they say where you are, not what you're doing.
HIDE_WORKING_LOCATION = "--hide-working-location" in sys.argv

# Mirrors renCal's own detection (src/lib/conference.ts) so the panel offers a
# Join button for exactly the events renCal itself would.
CONFERENCE_PROPS = [
    ("X-GOOGLE-CONFERENCE", "Google Meet"),
    ("X-OUTLOOK-CONFERENCE", "Meeting"),
    ("X-PM-CONFERENCE-URL", "Meeting"),
]
MEETING_PATTERNS = [
    ("Zoom", r"https?://(?:[\w-]+\.)*(?:zoom\.us|zoomgov\.com)/(?:j|my|s|w|wc)/[^\s<>\"']+"),
    ("Google Meet", r"https?://meet\.google\.com/[^\s<>\"']+"),
    ("Microsoft Teams", r"https?://teams\.(?:microsoft|live)\.com/[^\s<>\"']+"),
    ("Webex", r"https?://(?:[\w-]+\.)*webex\.com/[^\s<>\"']+"),
    ("Jitsi", r"https?://meet\.jit\.si/[^\s<>\"']+"),
    ("Whereby", r"https?://(?:www\.)?whereby\.com/[^\s<>\"']+"),
    ("Proton Meet", r"https?://meet\.proton\.me/[^\s<>\"']+"),
]


def read_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def calendar_root():
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    config = read_toml(os.path.join(config_home, "caldir", "config.toml"))
    return os.path.expanduser(config.get("calendar_dir", "~/caldir"))


def detect_meeting(text):
    if not text:
        return None
    for label, pattern in MEETING_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return {"url": re.sub(r"[),.;:!?\]]+$", "", match.group(0)), "label": label}
    return None


def meeting_for(event):
    for prop, label in CONFERENCE_PROPS:
        value = str(event.get(prop, "")).strip()
        if value.startswith(("https://", "http://")):
            return {"url": value, "label": label}
    # Location first, as renCal does; descriptions are where invites from
    # other clients usually bury the link, so fall back to that.
    return detect_meeting(str(event.get("LOCATION", ""))) or detect_meeting(str(event.get("DESCRIPTION", "")))


def as_local(value):
    """A DTSTART/DTEND value as an aware datetime; dates become local midnight."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    return datetime.combine(value, time.min).astimezone()


def recurrence_id_for(event, start, recurring_uids):
    """renCal's deep-link recurrence-id: the occurrence's original start.
    The expander stamps a RECURRENCE-ID on every occurrence, one-off events
    included, so whether the event is part of a series is decided from the
    files themselves."""
    if str(event.get("UID", "")) not in recurring_uids:
        return None
    rid = event.get("RECURRENCE-ID")
    value = rid.dt if rid is not None else start
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return value.strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


def load_calendar(path, recurring_uids):
    """One VCALENDAR holding every event in the folder, so overrides of a
    recurring series (often stored as separate files) find their master."""
    merged = icalendar.Calendar()
    seen_tz = set()
    for name in sorted(os.listdir(path)):
        if not name.endswith(".ics"):
            continue
        try:
            with open(os.path.join(path, name), "rb") as f:
                cal = icalendar.Calendar.from_ical(f.read())
        except Exception:
            continue
        for component in cal.walk():
            if component.name == "VEVENT":
                if component.get("RRULE") or component.get("RDATE") or component.get("RECURRENCE-ID"):
                    recurring_uids.add(str(component.get("UID", "")))
                merged.add_component(component)
            elif component.name == "VTIMEZONE":
                tzid = str(component.get("TZID"))
                if tzid not in seen_tz:
                    seen_tz.add(tzid)
                    merged.add_component(component)
    return merged


def main():
    root = calendar_root()
    if not os.path.isdir(root):
        print(json.dumps({"ok": True, "events": [], "calendars": 0}))
        return

    now = datetime.now().astimezone()
    window_end = now + timedelta(days=DAYS_AHEAD)
    events = []
    calendars = 0

    for slug in sorted(os.listdir(root)):
        path = os.path.join(root, slug)
        if slug.startswith(".") or not os.path.isdir(path):
            continue
        calendars += 1
        meta = read_toml(os.path.join(path, ".caldir", "config.toml"))
        recurring_uids = set()
        cal = load_calendar(path, recurring_uids)
        try:
            occurrences = recurring_ical_events.of(cal).between(now - timedelta(days=1), window_end)
        except Exception:
            continue

        for event in occurrences:
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            if HIDE_WORKING_LOCATION and str(event.get("X-GOOGLE-EVENT-TYPE", "")) == "workingLocation":
                continue
            raw_start = event.get("DTSTART")
            if raw_start is None:
                continue
            start_value = raw_start.dt
            all_day = not isinstance(start_value, datetime)
            start = as_local(start_value)
            raw_end = event.get("DTEND")
            if raw_end is not None:
                end = as_local(raw_end.dt)
            elif event.get("DURATION") is not None:
                end = start + event.get("DURATION").dt
            else:
                end = start + (timedelta(days=1) if all_day else timedelta(0))
            if end <= now:
                continue

            meeting = meeting_for(event)
            events.append({
                "uid": str(event.get("UID", "")),
                "recurrenceId": recurrence_id_for(event, start_value, recurring_uids),
                "title": str(event.get("SUMMARY", "")).strip() or "(No title)",
                "start": int(start.timestamp() * 1000),
                "end": int(end.timestamp() * 1000),
                "allDay": all_day,
                "calendar": meta.get("name", slug),
                "color": meta.get("color", ""),
                "joinUrl": meeting["url"] if meeting else "",
                "joinLabel": meeting["label"] if meeting else "",
            })

    events.sort(key=lambda e: (e["start"], not e["allDay"], e["title"]))
    print(json.dumps({"ok": True, "events": events[:LIMIT], "calendars": calendars}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
