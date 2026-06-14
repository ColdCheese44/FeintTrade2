"""Send today's journal entry as an email digest."""

import os
import sys


def send_digest(journal_path):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
    except ImportError:
        print("sendgrid not installed — skipping email. Run: pip install sendgrid")
        return

    api_key = os.getenv("SENDGRID_API_KEY")
    notify_email = os.getenv("NOTIFY_EMAIL")

    if not api_key or not notify_email:
        print("SENDGRID_API_KEY or NOTIFY_EMAIL not set — skipping email.")
        return

    with open(journal_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    date_part = os.path.basename(journal_path).replace(".md", "")
    sg = sendgrid.SendGridAPIClient(api_key)
    message = Mail(
        from_email="agent@yourdomain.com",
        to_emails=notify_email,
        subject=f"Trading Agent Report — {date_part}",
        plain_text_content=content,
    )
    response = sg.send(message)
    print(f"Email sent: status {response.status_code}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: notify.py <journal_path>")
        sys.exit(1)
    send_digest(sys.argv[1])
