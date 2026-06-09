#!/usr/bin/env python3
"""
VESPER EMAIL SENDER
Send emails from the command line via Gmail SMTP.
Credentials read from export_gmail.sh automatically.

Usage:
  python3 send_email.py --to "friend@email.com" --subject "Hey" --body "Hello!"
  python3 send_email.py --to "boss@work.com" --subject "Update" --body-file draft.txt
  echo "Hello" | python3 send_email.py --to "someone@gmail.com" --subject "Test" --stdin
"""

import smtplib, ssl, re, os, sys, argparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def get_creds():
    script = os.path.expanduser("~/vesper_agent/export_gmail.sh")
    addr = pw = ""
    try:
        for line in open(script):
            m = re.match(r'^GMAIL_ADDRESS="(.+)"', line.strip())
            if m: addr = m.group(1)
            m = re.match(r'^GMAIL_APP_PASSWORD="(.+)"', line.strip())
            if m: pw = m.group(1)
    except Exception as e:
        print(f"❌ Could not read credentials: {e}")
        sys.exit(1)
    return addr, pw

def send(to, subject, body, cc=None, reply_to=None):
    addr, pw = get_creds()
    if not pw:
        print("❌ GMAIL_APP_PASSWORD not set in export_gmail.sh")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["From"]    = addr
    msg["To"]      = to
    msg["Subject"] = subject
    if cc:       msg["Cc"]       = cc
    if reply_to: msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [to] + ([cc] if cc else [])

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(addr, pw)
            server.sendmail(addr, recipients, msg.as_string())
        print(f"✅ Email sent to {to}")
        print(f"   Subject: {subject}")
        return True
    except Exception as e:
        print(f"❌ Send failed: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vesper email sender")
    parser.add_argument("--to",        required=True,  help="Recipient email address")
    parser.add_argument("--subject",   required=True,  help="Email subject")
    parser.add_argument("--body",      default="",     help="Email body text")
    parser.add_argument("--body-file", default="",     help="Read body from file")
    parser.add_argument("--stdin",     action="store_true", help="Read body from stdin")
    parser.add_argument("--cc",        default="",     help="CC address")
    args = parser.parse_args()

    body = args.body
    if args.body_file:
        body = open(args.body_file).read()
    elif args.stdin:
        body = sys.stdin.read()

    if not body.strip():
        print("❌ Email body is empty. Use --body, --body-file, or --stdin")
        sys.exit(1)

    send(
        to      = args.to,
        subject = args.subject,
        body    = body,
        cc      = args.cc or None,
    )
