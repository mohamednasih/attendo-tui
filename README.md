# Attendo TUI

A dependency-free terminal dashboard for the 1337 Attendo student profile. It displays:

- time left to reach the current campus target;
- today's goal, calculated as time left divided by remaining period days;
- hours recorded in the current period;
- today's saved hours; and
- Attendo's live `HH:MM` estimate.

## Run

Python 3.10+ is required. On first run, the program opens Attendo's official 42 OAuth flow in Firefox. Complete the login there and the TUI will continue automatically. Later runs reuse the unexpired Attendo cookie from your Firefox profile.

You may instead provide the cookie explicitly:

```sh
export ATTENDO_TOKEN='paste-the-student_token-value-here'
export ATTENDO_LOGIN='your-login' # optional
python3 attendo.py
```

Use `r` to refresh and `q` to quit. The dashboard refreshes every 60 seconds by default. You can change that (minimum 15 seconds) with `--refresh 30`.

For a non-interactive status line suitable for scripts:

```sh
python3 attendo.py --once
```

You can alternatively provide a complete cookie header through `ATTENDO_COOKIE`. The program only reads the `student_token` row for the Attendo domain from Firefox; it does not inspect other cookies. Credentials are only sent to `https://attendo.1337.ma/students/profile` and are never written to disk by this program.

## Test

```sh
python3 -m unittest discover -v
```
