#!/usr/bin/env python3
"""
DCSD Daily School Summary
─────────────────────────
Logs into the Douglas County School District (DCSD) Infinite Campus parent
portal (dcsdk12.infinitecampus.org) via its JSON API, pulls current grades,
missing assignments, attendance, and upcoming work for each student on the
account, and emails a formatted HTML summary.

Unlike the upstream DPS tool this was forked from, DCSD exposes everything
through Infinite Campus directly — so this talks to the IC JSON API over
plain HTTP (no headless browser, no DOM scraping).

Config lives OUTSIDE the repo, in an XDG config file:
    ~/.config/dcsd-daily-summary/config.toml
(override the path with the DCSD_CONFIG env var).

Run:
    python3 dcsd_daily_summary.py              # scrape + email all students
    python3 dcsd_daily_summary.py --dry-run    # write HTML to ./out/, don't send
    python3 dcsd_daily_summary.py --debug      # dump raw IC JSON to ./debug/
    python3 dcsd_daily_summary.py --student Ava # just one student (name match)
"""

import argparse
import html as html_mod
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    print("ERROR: Python 3.11+ required (tomllib missing).")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed.  Fix:  pip install requests")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/dcsd-daily-summary/config.toml")


def load_config() -> dict:
    """Load config from the XDG config file (never from the repo / machine env)."""
    path = os.getenv("DCSD_CONFIG", DEFAULT_CONFIG_PATH)
    p = Path(path).expanduser()
    if not p.is_file():
        print(f"ERROR: config file not found: {p}")
        print("Create it from config.toml.template — see README.md.")
        sys.exit(1)

    # The config holds cleartext credentials — refuse to read it if it is
    # group/other-accessible (mode must be 0600-ish). Symlinks are resolved so
    # a permissive target can't hide behind a tight symlink.
    try:
        st = p.resolve().stat()
        if st.st_mode & 0o077:
            print(f"ERROR: {p} is accessible to other users "
                  f"(mode {st.st_mode & 0o777:03o}). Fix it with:")
            print(f"    chmod 600 {p}")
            sys.exit(1)
    except OSError as e:
        print(f"ERROR: cannot stat config file {p}: {e}")
        sys.exit(1)

    with p.open("rb") as fh:
        cfg = tomllib.load(fh)

    ic = cfg.get("infinite_campus", {})
    if not ic.get("username") or not ic.get("password"):
        print(f"ERROR: infinite_campus.username / .password missing in {p}")
        sys.exit(1)

    cfg.setdefault("infinite_campus", ic)
    ic.setdefault("base_url", "https://dcsdk12.infinitecampus.org")
    ic.setdefault("district", "douglas")  # IC "appName" — from /parents/douglas.jsp

    email = cfg.get("email", {})
    cfg["email"] = email
    cfg["students"] = cfg.get("students", [])  # optional per-student overrides
    return cfg


# ── Change detection (only email when something actually updates) ─────────────
DEFAULT_STATE_PATH = os.path.expanduser("~/.config/dcsd-daily-summary/state.json")


def state_path() -> Path:
    return Path(os.getenv("DCSD_STATE", DEFAULT_STATE_PATH)).expanduser()


def load_state() -> dict:
    p = state_path()
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))
    try:
        os.chmod(p, 0o600)  # holds per-student marks — keep it private
    except OSError:
        pass


def build_snapshot(data: dict) -> dict:
    """Reduce a scrape result to the fields whose change is worth an email."""
    return {
        "grades": {g["course"]: f'{g["grade"]}|{g["pct"]}' for g in data["grades"]},
        "missing": sorted(
            f'{a["course"]}|{a["assignment"]}|{a["due"]}'
            for a in data["missing_assignments"]
        ),
        "absences": {
            a["course"]: [a["absences"], a["tardies"]] for a in data["absences"]
        },
        "upcoming": sorted(
            f'{a["course"]}|{a["assignment"]}|{a["due"]}'
            for a in data["upcoming_assignments"]
        ),
    }


def diff_snapshot(prev: dict | None, cur: dict) -> list:
    """Human-readable list of what changed vs the last emailed snapshot.

    Empty list == nothing worth emailing. A newly-assigned upcoming item counts
    (an addition), but an upcoming item merely dropping off as its due date
    passes does NOT — that's the window sliding, not news.
    """
    if prev is None:
        return ["First summary — baseline for future change alerts."]

    changes = []

    # Grades posted or changed
    for course, mark in cur["grades"].items():
        if prev.get("grades", {}).get(course) != mark:
            letter = mark.split("|")[0]
            changes.append(f"Grade updated — {course}: {letter}")

    # Missing assignments added / cleared
    prev_missing, cur_missing = set(prev.get("missing", [])), set(cur["missing"])
    for m in sorted(cur_missing - prev_missing):
        course, name, _ = m.split("|", 2)
        changes.append(f"New missing — {name} ({course})")
    for m in sorted(prev_missing - cur_missing):
        course, name, _ = m.split("|", 2)
        changes.append(f"Missing cleared — {name} ({course})")

    # Attendance changes
    for course, ct in cur["absences"].items():
        if prev.get("absences", {}).get(course) != ct:
            changes.append(
                f"Attendance — {course}: {ct[0]} absence(s), {ct[1]} tardy(ies)"
            )

    # Newly assigned upcoming work (additions only)
    new_up = set(cur["upcoming"]) - set(prev.get("upcoming", []))
    for u in sorted(new_up):
        course, name, due = u.split("|", 2)
        changes.append(f"New assignment — {name} ({course}, due {due})")

    return changes


# ── Infinite Campus API client ────────────────────────────────────────────────
class ICClient:
    """Thin Infinite Campus Campus-Parent API client (cookie session + XSRF)."""

    def __init__(self, base_url: str, district: str, debug_dir: Path | None = None):
        self.base = base_url.rstrip("/")
        self.district = district
        self.debug_dir = debug_dir
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            }
        )

    def login(self, username: str, password: str) -> None:
        """POST credentials to verify.jsp; IC sets JSESSIONID + XSRF-TOKEN cookies."""
        url = f"{self.base}/campus/verify.jsp?nonBrowser=true"
        r = self.s.post(
            url,
            data={
                "username": username,
                "password": password,
                "appName": self.district,
                "portalLoginPage": "parents",
            },
            timeout=30,
        )
        if r.status_code >= 500:
            raise RuntimeError(f"IC portal unreachable (HTTP {r.status_code}).")
        if r.status_code >= 400:
            raise RuntimeError(f"Login failed (HTTP {r.status_code}) from verify.jsp.")

        body = r.text or ""
        m = re.search(r"<AUTHENTICATION>([^<]+)</AUTHENTICATION>", body)
        state = m.group(1).strip().lower() if m else ""
        if state == "password-error":
            raise RuntimeError("IC returned password-error — wrong username or password.")
        # Require an explicit success — do not treat a missing/unknown auth
        # state as authenticated just because a session cookie came back.
        if state != "success":
            raise RuntimeError(
                f"IC login not successful — auth state '{state or 'unknown'}'."
            )

        # IC echoes XSRF-TOKEN as a cookie; the SPA replays it as a header on
        # every call. Wire it in so state-changing/API calls are accepted.
        xsrf = self.s.cookies.get("XSRF-TOKEN")
        if xsrf:
            self.s.headers["X-XSRF-TOKEN"] = xsrf
        if not self.s.cookies.get("JSESSIONID"):
            raise RuntimeError("Login response carried no JSESSIONID — auth failed.")

    def get(self, path: str, label: str = "", **params):
        """GET a JSON endpoint; return parsed JSON (or None on 404/empty)."""
        url = f"{self.base}{path}"
        r = self.s.get(url, params=params or None, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            # Raise WITHOUT the URL — its query string carries personID (PII)
            # that would otherwise land in stack traces / run.log.
            raise RuntimeError(f"IC request {path} returned HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            return None
        if self.debug_dir and label:
            # Debug dumps contain full student records — keep the dir private.
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.debug_dir, 0o700)
            except OSError:
                pass
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            (self.debug_dir / f"{safe}.json").write_text(
                json.dumps(data, indent=2, default=str)
            )
        return data

    # ── Endpoint wrappers (see docs — IC Campus-Parent API) ───────────────────
    def students(self):
        return self.get("/campus/api/portal/students", label="students") or []

    def grades(self, person_id):
        return self.get(
            "/campus/resources/portal/grades",
            label=f"grades_{person_id}",
            personID=person_id,
        ) or []

    def assignments(self, person_id):
        return self.get(
            "/campus/api/portal/assignment/listView",
            label=f"assignments_{person_id}",
            personID=person_id,
        ) or []

    def attendance(self, enrollment_id, person_id):
        if not enrollment_id:
            return None
        return self.get(
            f"/campus/resources/portal/attendance/{enrollment_id}",
            label=f"attendance_{person_id}",
            courseSummary="true",
            personID=person_id,
        )


# ── Helpers: pull values out of loosely-typed IC JSON ─────────────────────────
def _first(d: dict, *keys, default=""):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _parse_due(val):
    """IC due dates look like '2026-08-30T00:00:00-06:00' or '2026-08-30'."""
    if not val:
        return None
    s = str(val)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_students(raw: list, name_filter: str = "") -> list:
    """Normalize /api/portal/students into [{personID,name,school,grade,enrollmentID}]."""
    out = []
    for s in raw or []:
        pid = _first(s, "personID", "personId", "studentPersonID")
        first = _first(s, "firstName", "firstname")
        last = _first(s, "lastName", "lastname")
        name = (f"{first} {last}").strip() or _first(s, "displayName", "name", default="Student")

        enrollments = s.get("enrollments") or s.get("enrollment") or []
        if isinstance(enrollments, dict):
            enrollments = [enrollments]
        enr = enrollments[0] if enrollments else {}
        school = _first(enr, "schoolName", "school")
        grade = _first(enr, "grade", "gradeLevel")
        enrollment_id = _first(enr, "enrollmentID", "enrollmentId")

        if name_filter and name_filter.lower() not in name.lower():
            continue
        out.append(
            {
                "personID": pid,
                "name": name,
                "school": str(school),
                "grade": str(grade),
                "enrollmentID": enrollment_id,
            }
        )
    return out


# The academic grading task in DCSD's IC; other tasks (e.g. "Work Habits")
# are behavior marks. We prefer this one, then fall back to any scored task.
ACADEMIC_TASK = "Content Knowledge"


def parse_grades(raw) -> list:
    """One (course, mark, pct) per course from enrollment[].courses[].gradingTasks[].

    DCSD grades by standards task: each course has gradingTasks with a
    `progressScore` letter. Two task names appear — "Content Knowledge"
    (the academic mark) and "Work Habits" (behavior). We take Content
    Knowledge when present, else the first task carrying a score. DCSD marks
    are letters, not percentages (`usePercent` is false), so `pct` is usually
    blank — that's expected, not a miss. Non-academic marks are labeled.
    """
    enrollments = raw if isinstance(raw, list) else [raw]
    out = []
    for enr in enrollments:
        if not isinstance(enr, dict):
            continue
        for course in enr.get("courses", []) or []:
            cname = str(course.get("courseName", "")).strip()
            if not cname:
                continue
            tasks = course.get("gradingTasks", []) or []

            chosen = next(
                (t for t in tasks
                 if t.get("taskName") == ACADEMIC_TASK and t.get("progressScore")),
                None,
            ) or next((t for t in tasks if t.get("progressScore")), None)
            if not chosen:
                continue

            letter = str(chosen.get("progressScore")).strip()
            pct = ""
            if chosen.get("usePercent") and chosen.get("progressPercent") not in (None, ""):
                try:
                    pct = f"{float(chosen['progressPercent']):.0f}%"
                except (TypeError, ValueError):
                    pct = str(chosen["progressPercent"])

            label = cname
            task_name = chosen.get("taskName")
            if task_name and task_name != ACADEMIC_TASK:
                label = f"{cname}  ({task_name})"  # e.g. Advisement 8 (Work Habits)

            out.append({"course": label, "grade": letter, "pct": pct})
    return out


def parse_assignments(raw, today: date, window_days: int = 14):
    """Split the assignment listView into (missing, upcoming)."""
    missing, upcoming = [], []
    horizon = today + timedelta(days=window_days)
    for a in raw or []:
        if not isinstance(a, dict):
            continue
        name = _first(a, "assignmentName", "name", default="(assignment)")
        course = _first(a, "courseName", "course", "sectionName")
        due = _parse_due(_first(a, "dueDate", "endDate", "assignedDate"))
        is_missing = bool(a.get("missing"))
        turned_in = bool(a.get("turnedIn") or a.get("submitted"))

        rec = {
            "assignment": str(name).strip(),
            "course": str(course).strip(),
            "due": due.strftime("%m/%d/%Y") if due else "",
        }
        if is_missing:
            missing.append(rec)
        elif due and today <= due <= horizon and not turned_in:
            upcoming.append(rec)

    upcoming.sort(key=lambda r: r["due"])
    return missing, upcoming


def parse_attendance(raw, today: date):
    """Per-course absence/tardy tallies from terms[].courses[].{absentList,tardyList}.

    IC records each absence/tardy as a dated event object in a per-course list.
    We count only events dated on/before `today` — IC also stores pre-arranged
    FUTURE absences (e.g. a planned out-of-town day), and counting those would
    make a kid look absent from every class before it has even happened.
    """
    absences = []
    if not isinstance(raw, dict):
        return absences, ""

    def count_past(events):
        n = 0
        for ev in events or []:
            d = _parse_due(ev.get("date")) if isinstance(ev, dict) else None
            if d is None or d <= today:
                n += 1
        return n

    agg: dict[str, dict] = {}
    for term in raw.get("terms", []) or []:
        for c in term.get("courses", []) or []:
            cname = str(c.get("courseName", "")).strip()
            if not cname:
                continue
            ab = count_past(c.get("absentList"))
            td = count_past(c.get("tardyList"))
            if ab or td:
                e = agg.setdefault(
                    cname, {"course": cname, "absences": 0, "tardies": 0}
                )
                e["absences"] += ab
                e["tardies"] += td
    return list(agg.values()), ""


# ── Per-student scrape ────────────────────────────────────────────────────────
def scrape_student(ic: ICClient, student: dict, overrides: dict) -> dict:
    pid = student["personID"]
    today = date.today()
    result = {
        "student_name": overrides.get("display_name") or student["name"],
        "student_school": student.get("school", ""),
        "student_grade": student.get("grade", ""),
        "student_recipients": overrides.get("recipients", []),
        "date": today.strftime("%A, %B %d, %Y"),
        "grades": [],
        "missing_assignments": [],
        "upcoming_assignments": [],
        "absences": [],
        "attendance_rate": "",
        "error": None,
    }
    try:
        result["grades"] = parse_grades(ic.grades(pid))
        missing, upcoming = parse_assignments(ic.assignments(pid), today)
        result["missing_assignments"] = missing
        result["upcoming_assignments"] = upcoming
        absences, rate = parse_attendance(
            ic.attendance(student.get("enrollmentID"), pid), today
        )
        result["absences"] = absences
        result["attendance_rate"] = rate
    except Exception as e:  # noqa: BLE001 — one bad student shouldn't kill the run
        result["error"] = str(e)
        print(f"   ✗ Error scraping {result['student_name']}: {e}")
    return result


# ── HTML email builder ────────────────────────────────────────────────────────
def _grade_color(letter: str) -> str:
    if letter.startswith("A"):
        return "#16a34a"
    if letter.startswith("B"):
        return "#2563eb"
    if letter.startswith("C"):
        return "#d97706"
    return "#dc2626"


def build_email_html(data: dict, changes: list | None = None) -> str:
    esc = html_mod.escape
    date_str = esc(data.get("date", ""))
    student = esc(data.get("student_name", "Your Child"))
    school = esc(data.get("student_school", ""))
    grade = esc(data.get("student_grade", ""))
    error = esc(data.get("error") or "") or None

    whatsnew_block = ""
    if changes:
        items = "".join(f"<li style='margin:2px 0;'>{esc(c)}</li>" for c in changes)
        whatsnew_block = (
            '<div style="background:#eff6ff;border-left:4px solid #2563eb;'
            'padding:12px 16px;">'
            '<div style="font-weight:700;color:#1e3a8a;font-size:14px;margin-bottom:4px;">'
            '🔔 What\'s new since last time</div>'
            f'<ul style="margin:4px 0 0;padding-left:20px;color:#1e40af;font-size:13px;">{items}</ul>'
            '</div>'
        )

    school_line = ""
    if school or grade:
        parts = [p for p in [school, f"Grade {grade}" if grade else ""] if p]
        school_line = (
            f'<div style="opacity:0.75;font-size:13px;margin-top:3px;">'
            f'{" · ".join(parts)}</div>'
        )

    def table(headers, rows_html, empty_msg, empty_color="#16a34a"):
        if not rows_html:
            return f'<p style="color:{empty_color};font-style:italic;margin:6px 0;">{empty_msg}</p>'
        ths = "".join(
            f'<th style="padding:7px 12px;text-align:left;font-size:12px;color:#6b7280;'
            f'border-bottom:2px solid #e5e7eb;">{h}</th>'
            for h in headers
        )
        return (
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr style="background:#f8fafc;">{ths}</tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
        )

    # Missing
    missing = data.get("missing_assignments", [])
    miss_rows = "".join(
        f'<tr style="border-bottom:1px solid #fecaca;">'
        f'<td style="padding:8px 12px;font-size:13px;color:#991b1b;">{esc(a["assignment"])}</td>'
        f'<td style="padding:8px 12px;font-size:13px;color:#6b7280;">{esc(a["course"])}</td>'
        f'<td style="padding:8px 12px;font-size:13px;color:#6b7280;white-space:nowrap;">{esc(a["due"])}</td>'
        f'</tr>'
        for a in missing
    )
    miss_html = table(
        ["ASSIGNMENT", "CLASS", "DUE"], miss_rows, "✓ No missing assignments"
    )
    miss_count = len(missing)
    miss_color = "#dc2626" if miss_count else "#16a34a"
    miss_bg = "#fee2e2" if miss_count else "#dcfce7"
    miss_icon = "⚠️" if miss_count else "✅"

    # Grades
    grades = data.get("grades", [])
    grade_rows = "".join(
        f'<tr style="border-bottom:1px solid #f3f4f6;">'
        f'<td style="padding:8px 12px;font-size:13px;">{esc(g["course"])}</td>'
        f'<td style="padding:8px 12px;font-size:14px;font-weight:700;text-align:center;'
        f'color:{_grade_color(g["grade"])};">{esc(g["grade"])}</td>'
        f'<td style="padding:8px 12px;font-size:13px;text-align:right;color:#6b7280;">{esc(g["pct"])}</td>'
        f'</tr>'
        for g in grades
    )
    grades_html = table(
        ["COURSE", "GRADE", "%"], grade_rows, "No grade data found", "#6b7280"
    )

    # Absences
    absences = data.get("absences", [])
    att_rate = data.get("attendance_rate", "")
    abs_rows = "".join(
        f'<tr style="border-bottom:1px solid #f3f4f6;">'
        f'<td style="padding:7px 12px;font-size:13px;">{esc(a["course"])}</td>'
        f'<td style="padding:7px 12px;font-size:13px;text-align:center;'
        f'color:{"#dc2626" if a["absences"] > 2 else "#d97706" if a["absences"] else "#16a34a"};">'
        f'{a["absences"]}</td>'
        f'<td style="padding:7px 12px;font-size:13px;text-align:center;color:#6b7280;">{a["tardies"]}</td>'
        f'</tr>'
        for a in absences
    )
    abs_html = table(
        ["COURSE", "ABSENCES", "TARDIES"], abs_rows, "✓ No absences recorded"
    )

    # Upcoming
    upcoming = data.get("upcoming_assignments", [])
    up_rows = "".join(
        f'<tr style="border-bottom:1px solid #f3f4f6;">'
        f'<td style="padding:7px 12px;font-size:13px;">{esc(a["assignment"])}</td>'
        f'<td style="padding:7px 12px;font-size:13px;color:#6b7280;">{esc(a["course"])}</td>'
        f'<td style="padding:7px 12px;font-size:13px;color:#6b7280;white-space:nowrap;">{esc(a["due"])}</td>'
        f'</tr>'
        for a in upcoming
    )
    up_html = table(
        ["ASSIGNMENT", "CLASS", "DUE"],
        up_rows,
        "No upcoming assignments in the next 2 weeks",
        "#6b7280",
    )

    error_block = ""
    if error:
        error_block = (
            f'<div style="background:#fef3c7;border-left:4px solid #f59e0b;'
            f'padding:12px 16px;"><strong>⚠ Note:</strong> Some data may be '
            f'incomplete — {error}</div>'
        )

    att_note = (
        f"<span style='font-size:13px;color:#6b7280;'>Attendance rate: {esc(att_rate)}</span>"
        if att_rate
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;
     overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.10);">
  <div style="background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;padding:26px 24px;">
    <div style="font-size:24px;font-weight:700;margin-bottom:4px;">📚 Daily School Summary</div>
    <div style="opacity:0.9;font-size:14px;">{student} · {date_str}</div>
    {school_line}
  </div>
  {whatsnew_block}
  {error_block}
  <div style="padding:18px 24px;border-bottom:1px solid #e5e7eb;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <span style="font-size:16px;font-weight:700;color:#111827;">{miss_icon} Missing Assignments</span>
      <span style="background:{miss_bg};color:{miss_color};border-radius:20px;
            padding:2px 10px;font-size:13px;font-weight:700;">{miss_count}</span>
    </div>
    {miss_html}
  </div>
  <div style="padding:18px 24px;border-bottom:1px solid #e5e7eb;">
    <div style="font-size:16px;font-weight:700;color:#111827;margin-bottom:12px;">📊 Current Grades</div>
    {grades_html}
  </div>
  <div style="padding:18px 24px;border-bottom:1px solid #e5e7eb;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <span style="font-size:16px;font-weight:700;color:#111827;">🗓️ Absences</span>
      {att_note}
    </div>
    {abs_html}
  </div>
  <div style="padding:18px 24px;">
    <div style="font-size:16px;font-weight:700;color:#111827;margin-bottom:12px;">📝 Upcoming Assignments <span style="font-size:13px;font-weight:400;color:#6b7280;">(next 14 days)</span></div>
    {up_html}
  </div>
  <div style="background:#f8fafc;padding:12px 24px;text-align:center;
       font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;">
    Pulled from DCSD Infinite Campus · {date_str}
  </div>
</div>
</body></html>"""


def _subject(student_name: str, subject_date: str) -> str:
    """Build the email subject, stripping CR/LF so a name can't inject headers."""
    name = re.sub(r"[\r\n]+", " ", str(student_name)).strip()
    return f"📚 Daily School Summary — {name} — {subject_date}"


def _plain_fallback(data: dict) -> str:
    """A short plain-text body so the message is valid even without HTML rendering."""
    lines = [f"Daily school summary for {data['student_name']} — {data['date']}", ""]
    lines.append(f"Missing assignments: {len(data['missing_assignments'])}")
    lines.append(f"Upcoming (14 days):  {len(data['upcoming_assignments'])}")
    if data["grades"]:
        lines.append("")
        lines.append("Marks:")
        for g in data["grades"]:
            lines.append(f"  {g['grade']:>3}  {g['course']}")
    lines.append("")
    lines.append("(Open in an HTML-capable mail client for the full formatted summary.)")
    return "\n".join(lines)


# ── gog backend: send via OAuth (no app password) ─────────────────────────────
def _run_gog(argv: list, html: str) -> None:
    """Invoke a gog wrapper, piping the HTML body in on stdin (--body-html-file -)."""
    proc = subprocess.run(
        argv, input=html, text=True, capture_output=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{os.path.basename(argv[0])} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


def send_via_gog(cfg_email: dict, data: dict, html: str, subject_date: str) -> None:
    """Optional CLI-helper delivery (method = "gog").

    Direct recipients (send_recipients) are emailed by the configured send
    helper; anyone in draft_recipients gets a draft prepared by the draft
    helper for you to review and send. Helper paths/account come from config.
    """
    subject = _subject(data["student_name"], subject_date)
    plain = _plain_fallback(data)

    send_bin = cfg_email.get("gog_send_bin", "")
    draft_bin = cfg_email.get("gog_draft_bin", "")
    draft_account = cfg_email.get("gog_draft_account", "")

    send_to = cfg_email.get("send_recipients", []) or []
    draft_to = cfg_email.get("draft_recipients", []) or []

    for addr in send_to:
        print(f"  → Sending to {addr} via send helper…")
        _run_gog(
            [send_bin, "gmail", "send", "--to", addr,
             "--subject", subject, "--body", plain, "--body-html-file", "-"],
            html,
        )
        print("  → Sent.")

    for addr in draft_to:
        print(f"  → Creating draft for {addr} via draft helper…")
        argv = [draft_bin, "gmail", "drafts", "create"]
        if draft_account:
            argv += ["-a", draft_account]
        argv += ["--to", addr, "--subject", subject,
                 "--body", plain, "--body-html-file", "-"]
        _run_gog(argv, html)
        print("  → Draft created.")


# ── Email sender (SMTP) ───────────────────────────────────────────────────────
def send_email(cfg_email: dict, html: str, student_name: str, subject_date: str,
               recipients: list) -> None:
    sender = cfg_email.get("from") or cfg_email.get("username", "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = _subject(student_name, subject_date)
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    host = cfg_email.get("smtp_host", "smtp.gmail.com")
    port = int(cfg_email.get("smtp_port", 587))
    context = ssl.create_default_context()  # verify cert + hostname on STARTTLS
    print(f"  → Sending to {', '.join(recipients)} via {host}…")
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(cfg_email["username"], cfg_email["password"])
        server.sendmail(sender, recipients, msg.as_string())
    print("  → Sent.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="DCSD Infinite Campus daily summary")
    ap.add_argument("--dry-run", action="store_true",
                    help="write HTML to ./out/ instead of emailing")
    ap.add_argument("--debug", action="store_true",
                    help="dump raw IC JSON responses to ./debug/")
    ap.add_argument("--student", default="",
                    help="only this student (case-insensitive name match)")
    ap.add_argument("--force", action="store_true",
                    help="send even if nothing changed since the last email")
    args = ap.parse_args()

    cfg = load_config()
    ic_cfg = cfg["infinite_campus"]
    email_cfg = cfg["email"]

    here = Path(__file__).resolve().parent
    debug_dir = (here / "debug") if args.debug else None

    print(f"\n{'=' * 55}")
    print(f"  DCSD Daily School Summary  ·  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'=' * 55}\n")

    ic = ICClient(ic_cfg["base_url"], ic_cfg["district"], debug_dir=debug_dir)
    print("→ Logging into Infinite Campus…")
    ic.login(ic_cfg["username"], ic_cfg["password"])
    print("→ Login successful.")

    students = parse_students(ic.students(), name_filter=args.student)
    if not students:
        print("No students found on this account"
              + (f" matching '{args.student}'." if args.student else "."))
        return 1
    print(f"→ Found {len(students)} student(s): "
          + ", ".join(s["name"] for s in students))

    # per-student config overrides, keyed by a case-insensitive name substring
    overrides_by_key = {
        str(s.get("match", "")).lower(): s for s in cfg.get("students", [])
    }

    global_recipients = email_cfg.get("recipients", [])
    if isinstance(global_recipients, str):
        global_recipients = [e.strip() for e in global_recipients.split(",") if e.strip()]

    state = load_state()
    new_state = dict(state)

    exit_code = 0
    for student in students:
        print(f"\n── {student['name']} " + "─" * 30)
        ov = {}
        for key, o in overrides_by_key.items():
            if key and key in student["name"].lower():
                ov = {
                    "display_name": o.get("display_name", ""),
                    "recipients": o.get("recipients", []),
                }
                break
        data = scrape_student(ic, student, ov)

        print(f"  Grades   : {len(data['grades'])} courses")
        print(f"  Missing  : {len(data['missing_assignments'])}")
        print(f"  Upcoming : {len(data['upcoming_assignments'])}")
        print(f"  Absences : {len(data['absences'])} course(s)")
        if data["error"]:
            print(f"  Error    : {data['error']}")
            exit_code = 2

        # ── Change detection: only email when something actually updated ──────
        pid = str(student.get("personID"))
        snap = build_snapshot(data)
        changes = diff_snapshot(state.get(pid), snap)
        if changes:
            print("  Changes  :")
            for c in changes:
                print(f"    • {c}")
        else:
            print("  Changes  : none since last email")

        method = email_cfg.get("method", "smtp").lower()
        recipients = list(dict.fromkeys(global_recipients + ov.get("recipients", [])))
        html = build_email_html(data, changes)

        def write_local_copy() -> Path:
            # Rendered emails carry grades/attendance PII — write only when we're
            # NOT delivering (dry-run / misconfig), into a private directory.
            out_dir = here / "out"
            out_dir.mkdir(mode=0o700, exist_ok=True)
            try:
                os.chmod(out_dir, 0o700)
            except OSError:
                pass
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", data["student_name"])
            f = out_dir / f"{safe}-{date.today():%Y%m%d}.html"
            f.write_text(html)
            return f

        if args.dry_run:
            out_file = write_local_copy()
            verdict = "would send" if (changes or args.force) else "would skip (no change)"
            print(f"  → Wrote {out_file}  (dry-run, {verdict})")
            continue  # never mutate state on a dry run

        # Skip send when nothing changed (unless --force). Refresh the baseline
        # so the next run compares against today's data.
        if not changes and not args.force:
            new_state[pid] = snap
            print("  → Skipped (no change).")
            continue

        try:
            if method == "gog":
                send_via_gog(email_cfg, data, html, data["date"])
            elif recipients:
                send_email(email_cfg, html, data["student_name"], data["date"], recipients)
            else:
                out_file = write_local_copy()
                print(f"  → Wrote {out_file}  (no recipients configured)")
            # Only advance the baseline after a successful send, so a failed
            # send retries (and re-reports the same changes) next run.
            new_state[pid] = snap
        except Exception as e:  # noqa: BLE001
            exit_code = 2
            print(f"  ✗ Send failed (will retry next run): {e}")

    if not args.dry_run:
        save_state(new_state)

    print("\n✓ Done!")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
