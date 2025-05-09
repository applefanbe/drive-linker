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
from flask import Flask, request, render_template_string
from b2sdk.v2 import InMemoryAccountInfo, B2Api
from urllib.parse import quote

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
B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APP_KEY = os.getenv("B2_APP_KEY")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")
B2_DOWNLOAD_URL = os.getenv("B2_DOWNLOAD_URL")

b2_api = None
app = Flask(__name__)

def init_b2():
    global b2_api
    if b2_api is None:
        info = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production", B2_KEY_ID, B2_APP_KEY)

def generate_signed_url(file_path, expires_in=3600):
    init_b2()
    bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
    file_name = quote(file_path)
    download_url = f"{B2_DOWNLOAD_URL}/{file_name}"
    auth_token = bucket.get_download_authorization(file_path, expires_in)
    return f"{download_url}?Authorization={auth_token}"

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sys.stdout.write(f"[{timestamp}] {message}\n")
    sys.stdout.flush()

def show_processed():
    if not os.path.exists(STATE_FILE):
        log("\ud83d\udcc4 No processed folders file found.")
        return
    with open(STATE_FILE, "r") as f:
        lines = f.readlines()
        log(f"\ud83d\udcc4 Processed folders ({len(lines)}): {', '.join([l.strip() for l in lines])}")

def update_airtable_record(record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {"fields": fields}
    response = requests.patch(url, headers=headers, json=data)
    if response.status_code == 200:
        log(f"\u2705 Airtable updated: {fields}")
    else:
        log(f"\u274c Failed to update Airtable record {record_id}: {response.text}")

def find_airtable_record(twin_sticker):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    formula = f"{{Twin Sticker}}='{twin_sticker}'"
    params = {"filterByFormula": formula}
    log(f"Airtable query: {formula}")
    resp = requests.get(url, headers=headers, params=params)
    log(f"Airtable response: {resp.text}")
    if resp.status_code != 200:
        log(f"\u274c Airtable API error: {resp.status_code}")
        return None
    records = resp.json().get("records", [])
    return records[0] if records else None

def generate_password(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def send_email(to_address, subject, body):
    bcc_address = "filmlab@gilplaquet.com"
    log("\u2709\ufe0f Composing message...")
    msg = EmailMessage()
    msg["From"] = "Gil Plaquet FilmLab <filmlab@gilplaquet.com>"
    msg["To"] = to_address
    msg["Bcc"] = bcc_address
    msg["Subject"] = subject
    msg.set_content(body)

    body_html = body.replace('\n', '<br>')
    html_body = f"""
    <div style='text-align: center;'>
      <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' style='width: 250px; margin-bottom: 20px;'>
    </div>
    <div style='font-family: sans-serif;'>{body_html}</div>
    """

    msg.add_alternative(f"""
    <html>
        <body>{html_body}</body>
    </html>
    """, subtype='html')

    try:
        log(f"\ud83d\udce4 Sending email to {to_address} via {SMTP_SERVER}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log("\u2705 Email sent successfully.")
    except Exception as e:
        log(f"\u274c Email failed to send: {e}")

def load_processed():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(line.strip() for line in f.readlines())

def save_processed(folder_name):
    with open(STATE_FILE, "a") as f:
        f.write(folder_name + "\n")

def list_roll_folders(prefix="rolls/"):
    init_b2()
    bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
    roll_names = set()

    log(f"\ud83d\udcc1 Scanning B2 for folders under prefix '{prefix}'")
    for file_version, _ in bucket.ls(prefix):
        log(f"\ud83d\udd0e Found file: {file_version.file_name}")
        parts = file_version.file_name.split("/")
        if len(parts) >= 2:
            roll_folder = parts[1]
            roll_names.add(roll_folder)

    log(f"\ud83d\udcc1 Found roll folders: {sorted(roll_names)}")
    return sorted(roll_names)

def main():
    log("\ud83d\ude80 Script triggered.")
    processed = load_processed()
    show_processed()

    folders = list_roll_folders()

    for name in folders:
        log(f"\ud83d\udd0d Checking folder: {name}")
        if name in processed:
            log(f"\u23e9 Already processed: {name}")
            continue

        twin_sticker = name.split("_")[-1].lstrip("0")
        record = find_airtable_record(twin_sticker)
        if not record:
            log(f"\u274c No Airtable match for {twin_sticker}")
            continue

        if record['fields'].get('Email Sent') == True:
            log(f"\u26d4 Already emailed: {twin_sticker}")
            continue

        email = record['fields'].get('Client Email')
        if not email:
            log(f"\u274c No email in Airtable record")
            continue

        password = generate_password()
        update_airtable_record(record['id'], {"Password": password})

        gallery_link = f"https://scans.gilplaquet.com/roll/{twin_sticker}"
        subject = f"Your Scans Are Ready - Roll {twin_sticker}"
        body = f"""
Hi there,

Good news! (One of) The roll(s) you sent in for development just got scanned.
You can view and download your scans at the link below:

{gallery_link}

To access your gallery, use the password: {password}

Thanks for sending in your film!

Gil Plaquet
www.gilplaquet.com
        """
        send_email(email, subject, body)
        update_airtable_record(record['id'], {"Email Sent": True})
        save_processed(name)
        log(f"\u2705 Processed and emailed: {twin_sticker}")

@app.route('/')
def index():
    return "\u2705 Render is online."

@app.route('/trigger')
def trigger():
    token = request.args.get("token")
    if token != TRIGGER_TOKEN:
        return "\u274c Unauthorized", 403
    try:
        main()
        return "\u2705 Script ran successfully."
    except Exception as e:
        return f"\u274c Script error: {e}"

@app.route('/roll/<sticker>', methods=['GET', 'POST'])
def gallery(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    expected_password = record['fields'].get("Password")
    if request.method == "POST":
        input_password = request.form.get("password")
        if input_password != expected_password:
            return "Incorrect password.", 403

        def find_roll_folder_by_sticker(sticker):
            init_b2()
            bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
            roll_folders = set()
            for file_version, _ in bucket.ls("rolls/"):
                parts = file_version.file_name.split("/")
                if len(parts) >= 2:
                    folder = parts[1]
                    if folder.endswith(sticker.zfill(6)):
                        roll_folders.add(folder)
            return sorted(roll_folders)[0] if roll_folders else None

        folder = find_roll_folder_by_sticker(sticker)
        if not folder:
            return f"No folder found for sticker {sticker}", 404

        prefix = f"rolls/{folder}/"
        bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)
        image_files = []
        for file_version, _ in bucket.ls(prefix):
            if file_version.file_name.lower().endswith((".jpg", ".jpeg", ".png")):
                image_files.append(file_version.file_name)

        image_urls = [generate_signed_url(f) for f in image_files]
        zip_url = generate_signed_url(f"{prefix}Roll_{sticker}.zip")

        return render_template_string("""
        <h2>Gallery for Roll {{ sticker }}</h2>
        <div style="display: flex; flex-wrap: wrap; gap: 10px;">
            {% for url in image_urls %}
                <img src="{{ url }}" style="width: 200px; height: auto;">
            {% endfor %}
        </div>
        <p><a href="{{ zip_url }}">Download All (ZIP)</a></p>
        <hr>
        <p><strong>Print Order Form (coming soon)</strong></p>
        """, sticker=sticker, image_urls=image_urls, zip_url=zip_url)

    return render_template_string("""
    <h2>Enter password to access Roll {{ sticker }}</h2>
    <form method="POST">
        <input type="password" name="password">
        <button type="submit">Submit</button>
    </form>
    """, sticker=sticker)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
