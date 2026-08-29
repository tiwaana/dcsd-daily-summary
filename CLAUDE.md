# DCSD Daily School Summary

A small Python tool that signs into the Douglas County School District Infinite
Campus parent portal, pulls each student's academic data, and emails a
formatted HTML daily summary. Forked from `ktinboulder/dps-daily-summary` and
rewritten for DCSD. Single script, standard library plus `requests`.

## What it does

Per student, each run collects and (conditionally) emails:
- Current grades (mark per class)
- Missing assignments
- Attendance — absences / tardies
- Upcoming assignments (next 14 days)

## Project structure

```
dcsd_daily_summary.py   # Main script — sign in, pull, render, email
config.toml.template    # Copy to ~/.config/dcsd-daily-summary/config.toml
requirements.txt        # requests
```

## Configuration

A TOML file **outside the repo**, at `~/.config/dcsd-daily-summary/config.toml`
(override with `DCSD_CONFIG`). Nothing sensitive is read from the machine
environment or the working tree. Sections: `[infinite_campus]` (login),
`[email]` (delivery + recipients), optional `[[students]]` overrides. Students
are auto-discovered. See the template's comments for the available keys.

## Behavior notes

- **Send-only-on-change.** Each run compares a per-student snapshot to the last
  emailed one and sends only when something changed (new/changed grade, a
  missing item added or cleared, an attendance change, or a newly-assigned
  upcoming item). A newly-assigned item counts; an item merely dropping off as
  its due date passes does not. State lives in
  `~/.config/dcsd-daily-summary/state.json` (`DCSD_STATE` override). The
  baseline advances only after a successful send. Emails lead with a "What's
  new" banner. `--force` sends regardless; `--dry-run` sends nothing and does
  not touch state.
- **Delivery.** SMTP by default; the config selects the method and recipients.
- The script is self-documenting — read it for specifics rather than
  duplicating implementation detail here.

## CLI

```
python3 dcsd_daily_summary.py              # email only students with changes
python3 dcsd_daily_summary.py --force      # email regardless of change
python3 dcsd_daily_summary.py --dry-run    # render locally, send nothing
python3 dcsd_daily_summary.py --student ava
```

## Scheduling

Local cron; the machine must be awake to fire. See README.
