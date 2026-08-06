"""Fires notifications when a water body's status is RED.

Channels, all optional and all independent - configure whichever you want in
.env:

  * desktop  - native macOS notification (osascript) or Linux `notify-send`.
               Skipped automatically when there's no desktop session, which is
               the normal case under cron on a headless box.
  * ntfy.sh  - https://ntfy.sh/<topic>, no account needed.
  * Pushover - https://pushover.net, needs a user key and an application token.
"""
import os
import shutil
import subprocess
import sys

import requests

from envfile import env_flag

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _desktop_notification(title: str, message: str):
    if not env_flag("DESKTOP_NOTIFY", default=True):
        return
    if sys.platform == "darwin":
        script = (
            f'display notification "{_escape_applescript(message)}" '
            f'with title "{_escape_applescript(title)}" sound name "Basso"'
        )
        subprocess.run(["osascript", "-e", script], check=False)
        return
    # Linux: only meaningful when there's a session bus to talk to. Under cron
    # on a server there isn't one, so stay quiet rather than erroring out.
    if shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        subprocess.run(["notify-send", "-u", "critical", title, message], check=False)


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", ", ")


def _ntfy_push(topic: str, title: str, message: str):
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning,pool"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}")


def _pushover_push(token: str, user_key: str, title: str, message: str):
    """Pushover priority: -2 silent, -1 quiet, 0 normal, 1 high, 2 requires ack.

    Priority 2 also needs retry/expire, so they're sent whenever priority is 2.
    """
    try:
        priority = int(os.environ.get("PUSHOVER_PRIORITY", "1"))
    except ValueError:
        priority = 1

    payload = {
        "token": token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
    }
    device = os.environ.get("PUSHOVER_DEVICE")
    if device:
        payload["device"] = device
    sound = os.environ.get("PUSHOVER_SOUND")
    if sound:
        payload["sound"] = sound
    if priority == 2:
        payload["retry"] = os.environ.get("PUSHOVER_RETRY", "60")
        payload["expire"] = os.environ.get("PUSHOVER_EXPIRE", "3600")

    try:
        resp = requests.post(PUSHOVER_URL, data=payload, timeout=10)
        if resp.status_code != 200:
            print(f"pushover push failed: {resp.status_code} {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"pushover push failed: {e}")


def send_notification(title: str, message: str):
    """Fans one alert out to every configured channel."""
    _desktop_notification(title, message)

    ntfy_topic = os.environ.get("NTFY_TOPIC")
    if ntfy_topic:
        _ntfy_push(ntfy_topic, title, message)

    po_token = os.environ.get("PUSHOVER_API_TOKEN")
    po_user = os.environ.get("PUSHOVER_USER_KEY")
    if po_token and po_user:
        _pushover_push(po_token, po_user, title, message)
    elif po_token or po_user:
        print("pushover: set both PUSHOVER_API_TOKEN and PUSHOVER_USER_KEY, skipping")


def check_and_alert(rows: list[dict]):
    for row in rows:
        name = row["name"] or "Pool"

        for title, message in _status_alerts(row, name) + _cassette_alerts(row, name):
            send_notification(title, message)


def _status_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    status = row["status"]
    prev_status = row.get("prev_status")

    if status == "RED":
        title = f"{name}: pool status RED"
        message = _format_alerts(row["alerts_json"]) or "Check the dashboard for details."
        return [(title, message)]
    if prev_status == "RED" and status != "RED":
        return [(f"{name}: back to normal", f"Status is now {status}.")]
    return []


def _cassette_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    status = row.get("cassette_status")
    prev_status = row.get("prev_cassette_status")
    if status is None:
        return []

    # Fire once on the transition into RED (or urgent), not on every subsequent RED reading.
    needs_replacement = status == "RED" or row.get("cassette_urgent")
    was_flagged = prev_status == "RED"

    if needs_replacement and not was_flagged:
        pct = row.get("cassette_pct_left")
        days = row.get("cassette_days_left") or ""
        pct_text = f"{pct:.0f}% left" if pct is not None else ""
        detail = ", ".join(x for x in [pct_text, days] if x)
        return [(f"{name}: replace the cassette", detail or "Cassette is running low.")]

    if prev_status == "RED" and status != "RED":
        return [(f"{name}: cassette replaced", "Cassette level is back to normal.")]

    return []


def _format_alerts(alerts_json: str) -> str:
    import json

    try:
        alerts = json.loads(alerts_json)
    except (TypeError, ValueError):
        return ""
    texts = [a.get("text") for a in alerts if a.get("status") == "RED" and a.get("text")]
    return "; ".join(texts)


if __name__ == "__main__":
    # `python alerts.py` sends a test alert through every configured channel.
    from envfile import load_dotenv

    load_dotenv()
    send_notification("WaterGuru: test alert", "If you can read this, notifications are wired up.")
    print("Test alert sent to all configured channels.")
