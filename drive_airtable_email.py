import os
import smtplib
import requests
import base64
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, request
from googleapiclient.discovery import build
from google.oauth2 import service_account

# === Rebuild service_account.json from env var ===
encoded = os.getenv("GOOGLE_CREDS_BASE64")
if encoded:
    with open("service_account.json", "wb") as f:
        f.write(base64.b64decode(encoded))

# === Configuration ===
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN")
STATE_FILE = "processed_folders.txt"
SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open("activity_log.txt", "a") as f:
        f.write(f"[{timestamp}] {message}\n")
    print(message)

def auth_google_drive():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def get_drive_folders(service):
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder'",
        fields="files(id, name)"
    ).execute()
    return results.get('files', [])

def create_share_link(service, file_id):
    permission = {'type': 'anyone', 'role': 'reader'}
    service.permissions().create(fileId=file_id, body=permission).execute()
    return f"https://drive.google.com/drive/folders/{file_id}"

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

def mark_email_sent(record_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "fields": {
            "Email Sent": True
        }
    }
    response = requests.patch(url, headers=headers, json=data)
    if response.status_code == 200:
        log(f"✅ Airtable updated: Email Sent box checked for record {record_id}")
    else:
        log(f"❌ Failed to update Airtable record {record_id}: {response.text}")

def send_email(to_address, subject, body):
    msg = EmailMessage()
    msg["From"] = "Your Lab <" + SMTP_USER + ">"
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def load_processed():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(line.strip() for line in f.readlines())

def save_processed(folder_name):
    with open(STATE_FILE, "a") as f:
        f.write(folder_name + "\n")

def main():
    drive = auth_google_drive()
    folders = get_drive_folders(drive)
    processed = load_processed()

    for folder in folders:
        name = folder['name']
        if name in processed:
            continue

        twin_sticker = name.strip().split("_")[-1]
        log(f"Looking up twin sticker: {twin_sticker}")
        record = find_airtable_record(twin_sticker)
        if not record:
            log(f"❌ No Airtable match for sticker {twin_sticker}")
            continue

        email = record['fields'].get('Client Email')
        if not email:
            log(f"❌ No email found for record with sticker {twin_sticker}")
            continue

        link = create_share_link(drive, folder['id'])

        subject = f"Your film roll #{twin_sticker} is ready!"
        body = f"""\
Hi there,

Good news! One of the rolls you sent in for development just got scanned.
You can download them from the link below. Thanks for sending in your film.

{link}

Gil

Gil Plaquet Photography
www.gilplaquet.com
"""
        send_email(email, subject, body)
        mark_email_sent(record['id'])
        save_processed(name)
        log(f"✅ Link sent to {email} for folder '{name}'")

app = Flask(__name__)

@app.route('/')
def index():
    return "✅ Render is online."

@app.route('/trigger')
def trigger():
    token = request.args.get("token")
    if token != TRIGGER_TOKEN:
        log("❌ Unauthorized trigger attempt")
        return "❌ Unauthorized", 403
    try:
        main()
        return "✅ Script ran successfully."
    except Exception as e:
        log(f"❌ Script error: {e}")
        return f"❌ Script error: {e}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
