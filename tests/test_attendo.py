import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from attendo import (
    _firefox_command,
    _firefox_roots,
    firefox_student_token,
    panel_data,
    panel_text,
    parse_profile,
    print_snapshot,
)


def profile_html(last_days: int = 4) -> str:
    days = [
        {"date": f"2026-10-{25 + index:02d}", "hours": [0, 0, 11, 7][index] if index < 4 else 1, "counted": True}
        for index in range(last_days)
    ]
    data = {
        "profile": {
            "login": "mnasih",
            "campus": {"name": "Rabat", "logtimeToClaim": 30},
        },
        "weeks": [
            {
                "weekStart": "2026-10-18",
                "weekEnd": "2026-10-24",
                "total": 30,
                "daily": [{"date": "2026-10-18", "hours": 4, "counted": True}] * 7,
            },
            {
                "weekStart": "2026-10-25",
                "weekEnd": "2026-10-28",
                "total": 18 if last_days == 4 else 21,
                "daily": days,
            },
        ],
        "todayHours": 2,
        "todayHoursEstimation": "02:09",
    }
    rsc = "1:" + json.dumps(data, separators=(",", ":"))
    payload = json.dumps([1, rsc])
    return f"<html><script>self.__next_f.push({payload})</script></html>"


class ParseProfileTests(unittest.TestCase):
    def test_extracts_requested_metrics(self):
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        snapshot = parse_profile(profile_html(), fetched_at=now)

        self.assertEqual(snapshot.login, "mnasih")
        self.assertEqual(snapshot.campus, "Rabat")
        self.assertEqual(snapshot.recorded, 20)
        self.assertEqual(snapshot.target, 30)
        self.assertEqual(snapshot.time_left, 10)
        self.assertEqual(snapshot.days_remaining, 3)
        self.assertEqual(snapshot.today_goal, 4)
        self.assertEqual(snapshot.hours_per_day, 4)
        self.assertEqual(snapshot.today_hours, 2)
        self.assertEqual(snapshot.live_estimate, "02:09")
        self.assertEqual(snapshot.current.counted_days, 4)
        self.assertEqual(snapshot.previous.total, 30)

    def test_panel_text_contains_live_summary(self):
        snapshot = parse_profile(profile_html())

        self.assertEqual(
            panel_text(snapshot),
            "⏳ 10h",
        )
        self.assertEqual(
            panel_data(snapshot),
            {
                "time_left": "10h",
                "today_goal": "2 / 4h",
                "days_counted": "4 / 7",
                "pace": "4h/day",
                "live_estimate": "02:09",
            },
        )

    def test_prints_whole_hour_daily_pace(self):
        output = StringIO()

        with redirect_stdout(output):
            print_snapshot(parse_profile(profile_html()))

        self.assertIn("Pace to finish:  4h/day", output.getvalue())

    def test_seven_counted_days_closes_period(self):
        snapshot = parse_profile(profile_html(last_days=7))

        self.assertIsNone(snapshot.current)
        self.assertEqual(snapshot.recorded, 2)
        self.assertEqual(snapshot.time_left, 28)
        self.assertEqual(snapshot.days_remaining, 7)
        self.assertAlmostEqual(snapshot.today_goal, 30 / 7)
        self.assertEqual(snapshot.previous.total, 21)

    def test_reads_only_attendo_token_from_firefox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "test.default-release"
            profile.mkdir()
            (root / "profiles.ini").write_text(
                "[Profile0]\nName=default\nIsRelative=1\nPath=test.default-release\nDefault=1\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(profile / "cookies.sqlite")
            connection.execute(
                "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)"
            )
            connection.executemany(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
                [
                    ("example.com", "private", "ignore-me", 4102444800),
                    ("attendo.1337.ma", "student_token", "expected-token", 4102444800),
                ],
            )
            connection.commit()
            connection.close()

            self.assertEqual(firefox_student_token((root,)), "expected-token")

    def test_reads_cookie_snapshot_when_firefox_locks_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "test.default-release"
            profile.mkdir()
            connection = sqlite3.connect(profile / "cookies.sqlite")
            connection.execute(
                "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)"
            )
            connection.execute(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
                ("attendo.1337.ma", "student_token", "locked-token", 4102444800),
            )
            connection.commit()
            connection.execute("BEGIN EXCLUSIVE")
            try:
                self.assertEqual(firefox_student_token((root,)), "locked-token")
            finally:
                connection.close()

    @patch("attendo.Path.home", return_value=Path("/home/student"))
    def test_flatpak_profile_root_uses_current_location(self, _home):
        self.assertIn(
            Path("/home/student/.var/app/org.mozilla.firefox/config/mozilla/firefox"),
            _firefox_roots(),
        )

    @patch("attendo.subprocess.run")
    @patch("attendo.shutil.which")
    def test_uses_flatpak_when_native_firefox_is_a_broken_launcher(self, which, run):
        which.side_effect = lambda command: f"/usr/bin/{command}"
        run.side_effect = [
            CompletedProcess(
                ["/usr/bin/firefox", "--version"],
                1,
                stderr="requires the firefox snap to be installed",
            ),
            CompletedProcess(
                ["/usr/bin/flatpak", "info", "org.mozilla.firefox"],
                0,
                stderr="",
            ),
        ]

        self.assertEqual(
            _firefox_command(),
            ["/usr/bin/flatpak", "run", "org.mozilla.firefox"],
        )


if __name__ == "__main__":
    unittest.main()
