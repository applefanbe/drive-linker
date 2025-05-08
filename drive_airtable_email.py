import os
import smtplib
import requests
import base64
import sys
import random
import string
import time
import hmac
import hashlib
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, request

# === Configuration ===
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN")
STATE_FILE = "processed_folders.txt"
BUNNY_STORAGE_ZONE = os.getenv("BUNNY_STORAGE_ZONE")
BUNNY_BASE_URL = os.getenv("BUNNY_BASE_URL")
BUNNY_TOKEN_KEY = os.getenv("BUNNY_TOKEN_KEY")


def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sys.stdout.write(f"[{timestamp}] {message}\n")
    sys.stdout.flush()


def show_processed():
    if not os.path.exists(STATE_FILE):
        log("📄 No processed folders file found.")
        return
    with open(STATE_FILE, "r") as f:
        lines = f.readlines()
        log(f"📄 Processed folders ({len(lines)}): {', '.join([l.strip() for l in lines])}")


def update_airtable_record(record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {"fields": fields}
    response = requests.patch(url, headers=headers, json=data)
    if response.status_code == 200:
        log(f"✅ Airtable updated: {fields}")
    else:
        log(f"❌ Failed to update Airtable record {record_id}: {response.text}")


def find_airtable_record(twin_sticker):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    formula = f"{{Twin Sticker}}='{twin_sticker}'"
    params = {"filterByFormula": formula}
    log(f"Airtable query: {formula}")
    resp = requests.get(url, headers=headers, params=params)
    log(f"Airtable response: {resp.text}")
    if resp.status_code != 200:
        log(f"❌ Airtable API error: {resp.status_code}")
        return None
    records = resp.json().get("records", [])
    return records[0] if records else None


def generate_password(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def generate_signed_url(file_path, expires_in=86400):
    expires = int(time.time()) + expires_in
    path = f"/{BUNNY_STORAGE_ZONE}/{file_path}"
    string_to_sign = f"{path}{expires}"
    token = base64.urlsafe_b64encode(hmac.new(
        BUNNY_TOKEN_KEY.encode(), string_to_sign.encode(), hashlib.sha256
    ).digest()).decode().rstrip("=")
    return f"{BUNNY_BASE_URL}/{file_path}?token={token}&expires={expires}"


def send_email(to_address, subject, body):
    bcc_address = "filmlab@gilplaquet.com"
    log("✉️ Composing message...")
    msg = EmailMessage()
    msg["From"] = "Gil Plaquet FilmLab <filmlab@gilplaquet.com>"
    msg["To"] = to_address
    msg["Bcc"] = bcc_address
    log(f"📥 BCC added: {bcc_address}")
    msg["Subject"] = subject
    msg.set_content(body)

    body_html = body.replace('\n', '<br>')
    html_body = f"""
    <div style='text-align: center;'>
      <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' style='width: 150px; margin-bottom: 20px;'>
    </div>
    <div style='font-family: sans-serif;'>{body_html}</div>
    """

    msg.add_alternative(f"""
    <html>
        <body>{html_body}</body>
    </html>
    """, subtype='html')

    try:
        log(f"📤 Sending email to {to_address} via {SMTP_SERVER}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log("✅ Email sent successfully.")
    except Exception as e:
        log(f"❌ Email failed to send: {e}")


def load_processed():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(line.strip() for line in f.readlines())


def save_processed(folder_name):
    with open(STATE_FILE, "a") as f:
        f.write(folder_name + "\n")


def main():
    log("🚀 Script triggered.")
    processed = load_processed()
    log(f"✅ {len(processed)} folders already processed.")
    show_processed()

    folders = ["Roll_000391"]

    for name in folders:
        log(f"🔍 Checking folder: {name}")
        if name in processed:
            log(f"⏩ Skipping already processed folder: {name}")
            continue

        twin_sticker = name.strip().split("_")[-1]
        log(f"🔎 Looking up twin sticker: {twin_sticker}")
        record = find_airtable_record(twin_sticker)
        if not record:
            log(f"❌ No Airtable match for sticker {twin_sticker}")
            continue

        if record['fields'].get('Email Sent') == True:
            log(f"⛔ Email already sent for sticker {twin_sticker}, skipping.")
            continue

        email = record['fields'].get('Client Email')
        if not email:
            log(f"❌ No email found for record with sticker {twin_sticker}")
            continue

        password = generate_password()
        update_airtable_record(record['id'], {
            "Password": password
        })

        gallery_link = f"https://yourdomain.com/roll/{twin_sticker}"
        subject = f"Your Photos Are Ready - Roll {twin_sticker}"
        body = f"""
Hi there,

Good news! One of the rolls you sent in for development just got scanned.
You can view and download your scans at the link below:

{gallery_link}

To access your gallery, use the password: {password}

Thanks for sending in your film!

Gil

Gil Plaquet Photography
www.gilplaquet.com
        """
        log(f"📧 Preparing to send email to {email} for roll {twin_sticker}")
        send_email(email, subject, body)
        update_airtable_record(record['id'], {
            "Email Sent": True
        })
        save_processed(name)
        log(f"✅ Link sent to {email} for folder '{name}'")


# === Flask app ===
app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Render is online."

@app.route('/trigger')
def trigger():
    token = request.args.get("token")
    log(f"🔐 Trigger received. Token = {token}")
    if token != TRIGGER_TOKEN:
        log("❌ Unauthorized trigger attempt")
        return "❌ Unauthorized", 403
    try:
        main()
        log("✅ Main function completed.")
        return "✅ Script ran successfully."
    except Exception as e:
        log(f"❌ Script error: {e}")
        return f"❌ Script error: {e}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
