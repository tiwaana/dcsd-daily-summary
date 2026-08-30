# DCSD Daily School Summary

A daily check-in for your kid's [Douglas County School District](https://dcsdk12.infinitecampus.org) grades. Logs into the Infinite Campus parent portal, pulls grades, missing assignments, attendance, and upcoming work, and emails a clean summary to the whole family.

> Forked from [`ktinboulder/dps-daily-summary`](https://github.com/ktinboulder/dps-daily-summary) (Denver Public Schools) and rewritten for DCSD.

## What you get

A daily HTML email, per student, with:

- **Current grades** — mark per class
- **Missing assignments** — what's flagged missing and which class it's for
- **Attendance** — absences and tardies per class
- **Upcoming assignments** — what's due in the next two weeks

It authenticates as you and reads your own kids' data — there is no official
parent API, so treat it as a personal-use tool for your own account.

## Requirements

- Python 3.11+
- A DCSD Infinite Campus **parent** portal account
- An email account that can send via SMTP (Gmail with an [App Password](https://myaccount.google.com/apppasswords) works well)

## Setup

```bash
git clone https://github.com/tiwaana/dcsd-daily-summary.git
cd dcsd-daily-summary
pip install -r requirements.txt
```

Then create your config **outside the repo**, under `~/.config` — nothing sensitive touches the working tree or your shell environment:

```bash
mkdir -p ~/.config/dcsd-daily-summary
cp config.toml.template ~/.config/dcsd-daily-summary/config.toml
chmod 600 ~/.config/dcsd-daily-summary/config.toml
$EDITOR ~/.config/dcsd-daily-summary/config.toml
```

Fill in your portal login and email settings. Students are **auto-discovered**, so you only add `[[students]]` blocks to rename a kid in the email or route their summary to extra recipients. See the comments in the template.

(Prefer a different location? Point `DCSD_CONFIG` at any TOML file.)

## Run

```bash
python3 dcsd_daily_summary.py            # email only students with changes
python3 dcsd_daily_summary.py --force    # email even if nothing changed
python3 dcsd_daily_summary.py --dry-run  # render locally, send nothing
python3 dcsd_daily_summary.py --student ava   # just one student (name match)
```

## Only emails when something changes

By default the summary is sent **only when a student's data actually updated**
since the last email — a new or changed grade, a missing assignment added or
cleared, an attendance change, or a newly-assigned upcoming item. A run where
nothing changed sends nothing, so the nightly job stays quiet until there's
something to see. Each email leads with a **"What's new since last time"**
banner listing exactly what changed.

The last-emailed state is remembered in `~/.config/dcsd-daily-summary/state.json`
(override with `DCSD_STATE`). Use `--force` to send regardless, or delete
`state.json` to reset the baseline.

## Tests

Offline harness — no network, no credentials, synthetic data only:

```bash
python3 -m unittest discover -s tests    # or: pytest
```

Covers the parsers, the change-detection logic, and the security behaviors
(HTML escaping, email-subject header-injection guard, config-permission check).

## Schedule (local cron)

Runs on your machine, so nothing leaves it. The machine has to be awake at run time.

```bash
crontab -e
```

```cron
# 7 PM every day
0 19 * * *  cd /path/to/dcsd-daily-summary && /usr/bin/python3 dcsd_daily_summary.py >> ~/.config/dcsd-daily-summary/run.log 2>&1
```

## Notes

- Because it reads the portal pages as you, a DCSD change to their portal may
  occasionally require a small update to the tool.
- Sending: SMTP with an app password works out of the box. See the config
  template for the available options.
- **Privacy:** the config file must be private (mode `600`) or the tool refuses
  to run. `--dry-run` writes a rendered copy to `./out/` and `--debug` dumps raw
  records to `./debug/` — both contain student data, are created `700`, and are
  git-ignored; delete them when you're done poking around.

## Credit

Original DPS version and email design: [`ktinboulder/dps-daily-summary`](https://github.com/ktinboulder/dps-daily-summary).
