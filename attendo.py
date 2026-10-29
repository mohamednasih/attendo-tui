#!/usr/bin/env python3
"""A small, dependency-free terminal dashboard for Attendo."""

from __future__ import annotations

import argparse
import configparser
import curses
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_URL = "https://attendo.1337.ma/students/profile"
LOGIN_URL = "https://attendo.1337.ma/api/students/auth/login"
_browser_cookie: str | None = None


class AttendoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Day:
    date: str
    hours: float
    counted: bool


@dataclass(frozen=True)
class Period:
    start: str
    end: str
    total: float
    days: tuple[Day, ...]

    @property
    def counted_days(self) -> int:
        return sum(day.counted for day in self.days)


@dataclass(frozen=True)
class Snapshot:
    login: str
    campus: str
    target: float
    current: Period | None
    previous: Period | None
    today_hours: float
    live_estimate: str | None
    fetched_at: datetime

    @property
    def recorded(self) -> float:
        # Attendo reports today's saved time separately from the completed
        # daily totals in the current period.
        current_total = self.current.total if self.current else 0.0
        return current_total + self.today_hours

    @property
    def time_left(self) -> float:
        return max(0.0, self.target - self.recorded)

    @property
    def days_remaining(self) -> int:
        return max(1, 7 - self.current.counted_days) if self.current else 7

    @property
    def today_goal(self) -> float:
        return self.time_left / self.days_remaining


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_script = False
        self.buffer: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.in_script = True
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.buffer))
            self.in_script = False


def _next_data(html: str) -> str:
    parser = _ScriptCollector()
    parser.feed(html)
    chunks: list[str] = []
    for script in parser.scripts:
        match = re.fullmatch(r"\s*self\.__next_f\.push\((.*)\)\s*", script, re.DOTALL)
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
            chunks.append(payload[1])
    return "\n".join(chunks)


def _last_value(data: str, pattern: str, label: str) -> str:
    matches = re.findall(pattern, data, re.DOTALL)
    if not matches:
        raise AttendoError(f"Could not find {label} in the profile page; the site may have changed.")
    value = matches[-1]
    return value if isinstance(value, str) else value[0]


def parse_profile(html: str, fetched_at: datetime | None = None) -> Snapshot:
    data = _next_data(html)
    if not data:
        # Useful for saved fragments and tolerant of the escaped RSC form.
        data = html.replace(r'\"', '"')

    target = float(_last_value(data, r'"logtimeToClaim"\s*:\s*([0-9.]+)', "campus target"))
    today = float(_last_value(data, r'"todayHours"\s*:\s*([0-9.]+)', "today's hours"))
    estimates = re.findall(r'"todayHoursEstimation"\s*:\s*(?:"([0-9]+:[0-9]{2})"|null)', data)
    estimate = estimates[-1] if estimates and estimates[-1] else None

    logins = re.findall(r'"login"\s*:\s*"([^"\\]+)"', data)
    campuses = re.findall(
        r'"campus"\s*:\s*\{.*?"name"\s*:\s*"([^"\\]+)".*?"logtimeToClaim"',
        data,
        re.DOTALL,
    )
    login = logins[-1] if logins else "student"
    campus = campuses[-1] if campuses else "1337"

    period_re = re.compile(
        r'"weekStart"\s*:\s*"([^"\\]+)"\s*,\s*'
        r'"weekEnd"\s*:\s*"([^"\\]+)"\s*,\s*'
        r'"total"\s*:\s*([0-9.]+)\s*,\s*"daily"\s*:\s*\[(.*?)\]\s*\}',
        re.DOTALL,
    )
    day_re = re.compile(
        r'"date"\s*:\s*"([^"\\]+)"\s*,\s*'
        r'"hours"\s*:\s*([0-9.]+)\s*,\s*"counted"\s*:\s*(true|false)'
    )
    periods: list[Period] = []
    for start, end, total, daily in period_re.findall(data):
        days = tuple(
            Day(date, float(hours), counted == "true")
            for date, hours, counted in day_re.findall(daily)
        )
        periods.append(Period(start, end, float(total), days))
    if not periods:
        raise AttendoError("Could not find attendance periods in the profile page; the site may have changed.")

    # Attendo closes a period after seven counted days.
    if periods[-1].counted_days < 7:
        current = periods[-1]
        previous = periods[-2] if len(periods) > 1 else None
    else:
        current = None
        previous = periods[-1]

    return Snapshot(
        login=login,
        campus=campus,
        target=target,
        current=current,
        previous=previous,
        today_hours=today,
        live_estimate=estimate,
        fetched_at=fetched_at or datetime.now().astimezone(),
    )


def _cookie_from_environment() -> str:
    if _browser_cookie:
        return f"student_token={_browser_cookie}"
    cookie = os.environ.get("ATTENDO_COOKIE", "").strip()
    if cookie:
        return cookie
    token = os.environ.get("ATTENDO_TOKEN", "").strip()
    if not token:
        raise AttendoError(
            "Set ATTENDO_TOKEN to your student_token cookie value "
            "(or set ATTENDO_COOKIE to the complete cookie header)."
        )
    login = os.environ.get("ATTENDO_LOGIN", "").strip()
    cookie = f"student_token={token}"
    if login:
        cookie += f"; student_login={login}"
    return cookie


def _firefox_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".mozilla/firefox",
        home / "snap/firefox/common/.mozilla/firefox",
        home / ".var/app/org.mozilla.firefox/config/mozilla/firefox",
    )


def _firefox_profiles(roots: tuple[Path, ...] | None = None) -> list[Path]:
    profiles: list[Path] = []
    for root in roots or _firefox_roots():
        config = configparser.ConfigParser()
        ini = root / "profiles.ini"
        if ini.is_file():
            try:
                config.read(ini)
                sections = sorted(
                    (section for section in config.sections() if section.startswith("Profile")),
                    key=lambda section: config.getboolean(section, "Default", fallback=False),
                    reverse=True,
                )
                for section in sections:
                    value = config.get(section, "Path", fallback="")
                    if not value:
                        continue
                    path = Path(value).expanduser()
                    if config.getboolean(section, "IsRelative", fallback=True):
                        path = root / path
                    if path not in profiles:
                        profiles.append(path)
            except (configparser.Error, OSError):
                pass
        if root.is_dir():
            for path in root.glob("*.default*"):
                if path.is_dir() and path not in profiles:
                    profiles.append(path)
    return profiles


def firefox_student_token(roots: tuple[Path, ...] | None = None) -> str | None:
    """Read only Attendo's unexpired student token from Firefox profiles."""
    now = int(time.time())
    for profile in _firefox_profiles(roots):
        database = profile / "cookies.sqlite"
        if not database.is_file():
            continue
        def read_cookie(source: Path) -> tuple[str, int] | None:
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=1)
            try:
                return connection.execute(
                    "SELECT value, expiry FROM moz_cookies "
                    "WHERE name = ? AND host IN (?, ?) "
                    "ORDER BY expiry DESC LIMIT 1",
                    ("student_token", "attendo.1337.ma", ".attendo.1337.ma"),
                ).fetchone()
            finally:
                connection.close()

        try:
            row = read_cookie(database)
        except (OSError, sqlite3.Error):
            # Firefox can hold an exclusive lock on its live cookie database.
            # A snapshot preserves its WAL while avoiding that process lock.
            try:
                with tempfile.TemporaryDirectory() as directory:
                    snapshot = Path(directory) / "cookies.sqlite"
                    shutil.copy2(database, snapshot)
                    wal = database.with_name(f"{database.name}-wal")
                    if wal.is_file():
                        shutil.copy2(wal, snapshot.with_name(f"{snapshot.name}-wal"))
                    row = read_cookie(snapshot)
            except (OSError, sqlite3.Error):
                continue
        if row and row[0] and int(row[1]) > now:
            return str(row[0])
    return None


def _firefox_command() -> list[str]:
    """Return a working Firefox command, including sandboxed installations."""
    errors: list[str] = []
    firefox = shutil.which("firefox")
    if firefox:
        try:
            check = subprocess.run(
                [firefox, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
        else:
            if check.returncode == 0:
                return [firefox]
            detail = check.stderr.strip()
            if detail:
                errors.append(detail)

    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            check = subprocess.run(
                [flatpak, "info", "org.mozilla.firefox"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
        else:
            if check.returncode == 0:
                return [flatpak, "run", "org.mozilla.firefox"]

    message = "ATTENDO_TOKEN is not set. Automatic login requires a working Firefox installation."
    if errors:
        message += f" Firefox reported: {errors[0]}"
    raise AttendoError(message)


def ensure_browser_session(timeout: int = 180) -> None:
    """Reuse Firefox's Attendo session or open the supported 42 OAuth flow."""
    global _browser_cookie
    if os.environ.get("ATTENDO_COOKIE", "").strip() or os.environ.get("ATTENDO_TOKEN", "").strip():
        return
    token = firefox_student_token()
    if token:
        _browser_cookie = token
        return
    if not sys.stdin.isatty():
        raise AttendoError("ATTENDO_TOKEN is not set and interactive browser login is unavailable.")
    firefox_command = _firefox_command()
    print("No Attendo session found. Opening the 42 sign-in in Firefox…", file=sys.stderr)
    print("Complete the login there; this terminal will continue automatically.", file=sys.stderr)
    try:
        subprocess.Popen(
            [*firefox_command, LOGIN_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise AttendoError(f"Could not open Firefox: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = firefox_student_token()
        if token:
            _browser_cookie = token
            print("Attendo session acquired.", file=sys.stderr)
            return
        time.sleep(1)
    raise AttendoError("Timed out waiting for the Attendo login. Run the command again to retry.")


def fetch_profile(url: str = DEFAULT_URL, timeout: float = 15.0) -> Snapshot:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": _cookie_from_environment(),
            "User-Agent": "attendo-tui/0.1 (+https://attendo.1337.ma)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AttendoError("Attendo rejected the session. Refresh your ATTENDO_TOKEN.") from exc
        raise AttendoError(f"Attendo returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise AttendoError(f"Could not reach Attendo: {exc.reason}") from exc
    if "/login" in final_url or "students/profile" not in final_url:
        raise AttendoError("The Attendo session expired. Refresh your ATTENDO_TOKEN.")
    return parse_profile(body)


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d")
    except ValueError:
        return value


def panel_text(snapshot: Snapshot) -> str:
    return (
        f"⏳ {_number(snapshot.time_left)}h"
        f"  ·  🎯 {_number(snapshot.today_hours)}/{_number(snapshot.today_goal)}h"
        f"  ·  ● {snapshot.live_estimate or '—'}"
    )


def print_snapshot(snapshot: Snapshot) -> None:
    period = "waiting for the next period"
    if snapshot.current:
        period = f"{_date(snapshot.current.start)} – {_date(snapshot.current.end)}"
    print(f"@{snapshot.login} · {snapshot.campus} · {period}")
    print(f"Time left:       {_number(snapshot.time_left)}h")
    print(
        f"Today's goal:    {_number(snapshot.today_hours)}h / {_number(snapshot.today_goal)}h"
        f" ({snapshot.days_remaining} days remaining)"
    )
    print(f"Hours recorded:  {_number(snapshot.recorded)}h / {_number(snapshot.target)}h")
    print(f"Today recorded:  {_number(snapshot.today_hours)}h")
    print(f"Live estimate:   {snapshot.live_estimate or 'unavailable'}")


def _put(window: curses.window, y: int, x: int, text: str, style: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        window.addnstr(y, max(0, x), text, max(0, width - max(0, x) - 1), style)
    except curses.error:
        pass


def _box(window: curses.window, y: int, x: int, h: int, w: int, title: str, value: str, note: str) -> None:
    _, screen_w = window.getmaxyx()
    w = min(w, screen_w - x)
    if w < 8:
        return
    line = "─" * (w - 2)
    _put(window, y, x, "╭" + line + "╮", curses.color_pair(3))
    for row in range(1, h - 1):
        _put(window, y + row, x, "│" + " " * (w - 2) + "│", curses.color_pair(3))
    _put(window, y + h - 1, x, "╰" + line + "╯", curses.color_pair(3))
    content_width = max(0, w - 4)
    _put(window, y + 1, x + 2, title.upper()[:content_width], curses.color_pair(2) | curses.A_BOLD)
    _put(window, y + 3, x + 2, value[:content_width], curses.color_pair(1) | curses.A_BOLD)
    _put(window, y + h - 2, x + 2, note[:content_width], curses.color_pair(4))


def _draw(window: curses.window, snapshot: Snapshot | None, status: str) -> None:
    window.erase()
    height, width = window.getmaxyx()
    _put(window, 1, 2, "ATTENDO", curses.color_pair(1) | curses.A_BOLD)
    _put(window, 1, 11, "terminal attendance", curses.color_pair(4))
    if not snapshot:
        _put(window, 4, 2, status, curses.color_pair(5) | curses.A_BOLD)
        _put(window, height - 2, 2, "q quit  ·  r retry", curses.color_pair(4))
        window.refresh()
        return

    identity = f"@{snapshot.login}  ·  {snapshot.campus}"
    _put(window, 3, 2, identity, curses.A_BOLD)
    if snapshot.current:
        period_note = (
            f"{_date(snapshot.current.start)} – {_date(snapshot.current.end)}"
            f"  ·  {snapshot.current.counted_days}/7 days counted"
        )
    else:
        period_note = "Last period complete · next period starts with a recorded day"
    _put(window, 4, 2, period_note, curses.color_pair(4))
    card_w = max(24, min(34, (width - 8) // 2))
    if width >= 58:
        _box(window, 6, 2, 7, card_w, "Time left", f"{_number(snapshot.time_left)} hours", "to reach the campus target")
        _box(window, 6, 4 + card_w, 7, card_w, "Current period", f"{_number(snapshot.recorded)} / {_number(snapshot.target)} h", "hours recorded")
        _box(
            window,
            14,
            2,
            7,
            card_w,
            "Today's goal",
            f"{_number(snapshot.today_hours)} / {_number(snapshot.today_goal)} h",
            f"recorded / target · {snapshot.days_remaining}d remain",
        )
        _box(window, 14, 4 + card_w, 7, card_w, "Live estimate", snapshot.live_estimate or "—", "reported by Attendo")
        footer_y = 22
    else:
        card_w = max(12, width - 4)
        cards = (
            ("Time left", f"{_number(snapshot.time_left)} hours", "to reach target"),
            ("Current period", f"{_number(snapshot.recorded)} / {_number(snapshot.target)} h", "hours recorded"),
            (
                "Today's goal",
                f"{_number(snapshot.today_hours)} / {_number(snapshot.today_goal)} h",
                f"recorded / target · {snapshot.days_remaining}d remain",
            ),
            ("Live estimate", snapshot.live_estimate or "—", "reported by Attendo"),
        )
        for index, card in enumerate(cards):
            _box(window, 6 + index * 6, 2, 5, card_w, *card)
        footer_y = 31

    if footer_y < height - 2:
        refreshed = snapshot.fetched_at.strftime("%H:%M:%S")
        _put(window, footer_y, 2, f"Updated {refreshed}  ·  {status}", curses.color_pair(4))
    _put(window, height - 2, 2, "q quit  ·  r refresh", curses.color_pair(4))
    window.refresh()


def run_tui(url: str, refresh_seconds: int, offline_html: str | None = None) -> None:
    def application(window: curses.window) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_BLUE, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        curses.init_pair(5, curses.COLOR_RED, -1)
        window.timeout(250)
        snapshot: Snapshot | None = None
        status = "loading…"
        next_refresh = 0.0
        while True:
            now = time.monotonic()
            if now >= next_refresh:
                try:
                    if offline_html:
                        snapshot = parse_profile(Path(offline_html).read_text(encoding="utf-8"))
                    else:
                        snapshot = fetch_profile(url)
                    status = "auto-refresh on" if not offline_html else "offline file"
                except (AttendoError, OSError) as exc:
                    status = str(exc)
                next_refresh = now + (refresh_seconds if not offline_html else 86400)
            _draw(window, snapshot, status)
            key = window.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), ord("R")):
                next_refresh = 0.0

    curses.wrapper(application)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Terminal dashboard for Attendo attendance")
    parser.add_argument("--once", action="store_true", help="print once instead of opening the TUI")
    parser.add_argument("--panel", action="store_true", help="print a compact GNOME panel status")
    parser.add_argument("--html", metavar="FILE", help="read a saved profile page instead of making a request")
    parser.add_argument("--url", default=DEFAULT_URL, help=argparse.SUPPRESS)
    parser.add_argument("--refresh", type=int, default=60, metavar="SECONDS", help="auto-refresh interval (default: 60)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh < 15:
        print("attendo: --refresh must be at least 15 seconds", file=sys.stderr)
        return 2
    try:
        if not args.html:
            ensure_browser_session()
        if args.once or args.panel:
            snapshot = (
                parse_profile(Path(args.html).read_text(encoding="utf-8"))
                if args.html
                else fetch_profile(args.url)
            )
            if args.panel:
                print(panel_text(snapshot))
            else:
                print_snapshot(snapshot)
        else:
            run_tui(args.url, args.refresh, args.html)
    except (AttendoError, OSError) as exc:
        print(f"attendo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
