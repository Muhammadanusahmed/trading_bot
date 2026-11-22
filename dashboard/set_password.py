"""
Set the dashboard login (username + password).

On the server:
    sudo /opt/crypto-trend-bot/.venv/bin/python /opt/crypto-trend-bot/dashboard/set_password.py
then:
    sudo systemctl restart crypto-dashboard

It asks for a username and password (the password is not shown while you
type), stores only a scrypt hash of the password in /etc/crypto-bot.env, and
adds a random session secret the first time. Running it again changes the
password and logs everyone out.
"""

import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import hash_password  # noqa: E402

ENV_FILE = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/crypto-bot.env")


def set_values(text, values):
    lines = text.splitlines()
    for key, value in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found. Run deploy/setup_server.sh first.")
    user = input("Dashboard username [admin]: ").strip() or "admin"
    while True:
        password = getpass.getpass("New password (min 10 characters): ")
        if len(password) < 10:
            print("Too short, use at least 10 characters.")
            continue
        if getpass.getpass("Type it again: ") != password:
            print("The two passwords don't match, try again.")
            continue
        break
    text = ENV_FILE.read_text()
    text = set_values(text, {
        "DASHBOARD_USER": user,
        "DASHBOARD_PASSWORD_HASH": hash_password(password),
        # a new secret logs out every phone/laptop that was logged in
        "DASHBOARD_SECRET_KEY": secrets.token_hex(32),
    })
    ENV_FILE.write_text(text)
    print(f"Saved in {ENV_FILE}. Now run: sudo systemctl restart crypto-dashboard")
