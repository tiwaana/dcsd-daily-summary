"""Offline test harness for dcsd_daily_summary.

No network, no credentials, no real student data — everything runs against
synthetic Infinite-Campus-shaped fixtures. Run with either:

    python3 -m unittest discover -s tests
    pytest

Covers the parsers, the change-detection logic, and the security-relevant
behaviors flagged in review (HTML escaping, subject header-injection, and the
config-permission guard).
"""

import importlib.util
import os
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

# ── Load the single-file module by path (repo root is this file's parent's parent)
_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("dcsd", _ROOT / "dcsd_daily_summary.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

TODAY = date.today()


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%dT00:00:00.000Z")


# ── Fixtures ──────────────────────────────────────────────────────────────────
STUDENTS_RAW = [
    {"personID": 501, "firstName": "Ava", "lastName": "Smith",
     "enrollments": [{"enrollmentID": 9001, "schoolName": "Rock Canyon HS", "grade": "10"}]},
    {"personID": 502, "firstName": "Sam", "lastName": "Smith",
     "enrollment": {"enrollmentID": 9002, "schoolName": "Ridge MS", "gradeLevel": "7"}},
]

GRADES_RAW = [{"courses": [
    {"courseName": "Language Arts", "gradingTasks": [
        {"taskName": "Work Habits", "progressScore": "A"},
        {"taskName": "Content Knowledge", "progressScore": "B", "usePercent": True,
         "progressPercent": 88.4},
    ]},
    {"courseName": "Choir", "gradingTasks": [
        {"taskName": "Work Habits", "progressScore": "A"},
    ]},
    {"courseName": "Ungraded Elective", "gradingTasks": [
        {"taskName": "Content Knowledge"},  # no progressScore -> skipped
    ]},
]}]

ASSIGN_RAW = [
    {"assignmentName": "Essay", "courseName": "Language Arts",
     "dueDate": _iso(TODAY + timedelta(days=3)), "missing": False, "turnedIn": False},
    {"assignmentName": "Late Lab", "courseName": "Science",
     "dueDate": _iso(TODAY - timedelta(days=5)), "missing": True, "turnedIn": False},
    {"assignmentName": "Done Early", "courseName": "Math",
     "dueDate": _iso(TODAY + timedelta(days=2)), "missing": False, "turnedIn": True},
    {"assignmentName": "Far Off", "courseName": "History",
     "dueDate": _iso(TODAY + timedelta(days=40)), "missing": False, "turnedIn": False},
]

ATTEND_RAW = {"terms": [{"courses": [
    {"courseName": "Math", "absentList": [
        {"date": (TODAY - timedelta(days=2)).isoformat()},   # past -> counts
        {"date": (TODAY + timedelta(days=30)).isoformat()},  # future -> ignored
    ], "tardyList": [{"date": (TODAY - timedelta(days=1)).isoformat()}]},
    {"courseName": "Art", "absentList": [], "tardyList": []},
]}]}


# ── Parsers ───────────────────────────────────────────────────────────────────
class TestParsers(unittest.TestCase):
    def test_students(self):
        studs = m.parse_students(STUDENTS_RAW)
        self.assertEqual([s["name"] for s in studs], ["Ava Smith", "Sam Smith"])
        self.assertEqual(studs[0]["personID"], 501)
        self.assertEqual(studs[0]["enrollmentID"], 9001)
        # handles singular "enrollment" alias + gradeLevel alias
        self.assertEqual(studs[1]["enrollmentID"], 9002)
        self.assertEqual(studs[1]["grade"], "7")

    def test_students_name_filter(self):
        self.assertEqual(len(m.parse_students(STUDENTS_RAW, "ava")), 1)
        self.assertEqual(m.parse_students(STUDENTS_RAW, "sam")[0]["name"], "Sam Smith")

    def test_grades_prefers_content_knowledge_and_labels_others(self):
        g = {x["course"]: x for x in m.parse_grades(GRADES_RAW)}
        self.assertEqual(g["Language Arts"]["grade"], "B")     # not the Work Habits "A"
        self.assertEqual(g["Language Arts"]["pct"], "88%")
        self.assertIn("Work Habits", g["Choir  (Work Habits)"]["course"])
        self.assertNotIn("Ungraded Elective", g)               # no score -> skipped

    def test_assignments_missing_vs_upcoming(self):
        missing, upcoming = m.parse_assignments(ASSIGN_RAW, TODAY)
        self.assertEqual([a["assignment"] for a in missing], ["Late Lab"])
        # Essay in window; Done Early excluded (turnedIn); Far Off excluded (>14d)
        self.assertEqual([a["assignment"] for a in upcoming], ["Essay"])

    def test_attendance_counts_past_only(self):
        absences, _ = m.parse_attendance(ATTEND_RAW, TODAY)
        self.assertEqual(absences, [{"course": "Math", "absences": 1, "tardies": 1}])

    def test_parse_due_formats(self):
        self.assertEqual(m._parse_due("2026-08-30T05:00:00.000Z"), date(2026, 8, 30))
        self.assertEqual(m._parse_due("2026-08-30"), date(2026, 8, 30))
        self.assertIsNone(m._parse_due(""))
        self.assertIsNone(m._parse_due("not-a-date"))


# ── Change detection ──────────────────────────────────────────────────────────
def _data(grades=None, missing=None, upcoming=None, absences=None):
    return {
        "grades": grades or [],
        "missing_assignments": missing or [],
        "upcoming_assignments": upcoming or [],
        "absences": absences or [],
    }


class TestChangeDetection(unittest.TestCase):
    def setUp(self):
        self.base = _data(
            grades=[{"course": "Math", "grade": "A", "pct": ""}],
            missing=[{"course": "Sci", "assignment": "Lab", "due": "08/20/2026"}],
            upcoming=[{"course": "Math", "assignment": "HW3", "due": "09/02/2026"}],
            absences=[{"course": "Math", "absences": 1, "tardies": 0}],
        )
        self.snap = m.build_snapshot(self.base)

    def test_first_run_sends(self):
        self.assertTrue(m.diff_snapshot(None, self.snap)[0].startswith("First summary"))

    def test_no_change_is_empty(self):
        self.assertEqual(m.diff_snapshot(self.snap, dict(self.snap)), [])

    def test_grade_change(self):
        cur = m.build_snapshot(_data(
            grades=[{"course": "Math", "grade": "B", "pct": ""}]))
        self.assertTrue(any("Grade updated — Math: B" in c
                            for c in m.diff_snapshot(self.snap, cur)))

    def test_new_and_cleared_missing(self):
        cur = m.build_snapshot(_data(
            missing=[{"course": "Math", "assignment": "HW1", "due": "08/28/2026"}]))
        ch = m.diff_snapshot(self.snap, cur)
        self.assertTrue(any("New missing — HW1" in c for c in ch))
        self.assertTrue(any("Missing cleared — Lab" in c for c in ch))

    def test_attendance_change(self):
        cur = m.build_snapshot(_data(
            absences=[{"course": "Math", "absences": 2, "tardies": 0}]))
        self.assertTrue(any("Attendance — Math: 2 absence" in c
                            for c in m.diff_snapshot(self.snap, cur)))

    def test_new_upcoming_triggers(self):
        cur = m.build_snapshot(_data(upcoming=[
            {"course": "Math", "assignment": "HW3", "due": "09/02/2026"},
            {"course": "Sci", "assignment": "Quiz", "due": "09/05/2026"}]))
        self.assertTrue(any("New assignment — Quiz" in c
                            for c in m.diff_snapshot(self.snap, cur)))

    def test_window_slide_removal_does_not_trigger(self):
        # HW3 drops off (due date passed) and nothing else changed -> no email
        prev = m.build_snapshot(self.base)
        cur = m.build_snapshot(_data(
            grades=self.base["grades"], missing=self.base["missing_assignments"],
            absences=self.base["absences"], upcoming=[]))
        # grades/missing/absences identical, only upcoming removed
        cur["grades"], cur["missing"], cur["absences"] = (
            prev["grades"], prev["missing"], prev["absences"])
        self.assertEqual(m.diff_snapshot(prev, cur), [])


# ── Security-relevant behavior ────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_html_is_escaped(self):
        evil = '<script>alert(1)</script>'
        data = {
            "student_name": evil, "student_school": "", "student_grade": "",
            "date": "Today",
            "grades": [{"course": evil, "grade": "A", "pct": ""}],
            "missing_assignments": [{"course": evil, "assignment": evil, "due": evil}],
            "upcoming_assignments": [],
            "absences": [], "attendance_rate": "", "error": None,
        }
        html = m.build_email_html(data, changes=[evil])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)  # escaped form present

    def test_subject_strips_crlf(self):
        subj = m._subject("Ava\r\nBcc: evil@example.com", "Today")
        self.assertNotIn("\r", subj)
        self.assertNotIn("\n", subj)

    def test_config_rejects_world_readable(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text('[infinite_campus]\nusername="u"\npassword="p"\n')
            os.chmod(cfg, 0o644)  # group/other readable -> must be refused
            os.environ["DCSD_CONFIG"] = str(cfg)
            try:
                with self.assertRaises(SystemExit):
                    m.load_config()
            finally:
                os.environ.pop("DCSD_CONFIG", None)

    def test_config_accepts_private_mode(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text('[infinite_campus]\nusername="u"\npassword="p"\n')
            os.chmod(cfg, 0o600)
            os.environ["DCSD_CONFIG"] = str(cfg)
            try:
                loaded = m.load_config()
                self.assertEqual(loaded["infinite_campus"]["username"], "u")
                self.assertEqual(loaded["infinite_campus"]["district"], "douglas")
            finally:
                os.environ.pop("DCSD_CONFIG", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
