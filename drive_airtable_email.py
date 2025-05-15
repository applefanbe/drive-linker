import os
import smtplib
import requests
import base64
import sys
import random
import string
import time
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, request, render_template_string
import boto3
from botocore.client import Config
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
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")

app = Flask(__name__)

# === S3-Compatible Signed URL ===
def generate_signed_url(file_path, expires_in=604800):
    s3 = boto3.client(
        's3',
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        endpoint_url=S3_ENDPOINT_URL,
        config=Config(signature_version='s3v4')
    )
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': B2_BUCKET_NAME, 'Key': file_path},
        ExpiresIn=expires_in
    )

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}", flush=True)

# === Airtable ===
def update_airtable_record(record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.patch(url, headers=headers, json={"fields": fields})
    if response.status_code == 200:
        log(f"✅ Airtable updated: {fields}")
    else:
        log(f"❌ Failed to update Airtable record {record_id}: {response.text}")

def find_airtable_record(twin_sticker):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    formula = f"{{Twin Sticker}}='{twin_sticker}'"
    response = requests.get(url, headers=headers, params={"filterByFormula": formula})
    if response.status_code != 200:
        log(f"❌ Airtable API error: {response.status_code}")
        return None
    records = response.json().get("records", [])
    return records[0] if records else None

# === Email ===
def generate_password(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def send_email(to_address, subject, body):
    bcc_address = "filmlab@gilplaquet.com"
    log("✉️ Composing message...")
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
    <div style='font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;'>{body_html}</div>
    """

    msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype='html')

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log("✅ Email sent successfully.")
    except Exception as e:
        log(f"❌ Email failed: {e}")

# === Folder Utilities ===
def load_processed():
    return set(open(STATE_FILE).read().splitlines()) if os.path.exists(STATE_FILE) else set()

def save_processed(folder_name):
    with open(STATE_FILE, "a") as f:
        f.write(folder_name + "\n")

def list_roll_folders(prefix="rolls/"):
    s3 = boto3.client(
        's3',
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        endpoint_url=S3_ENDPOINT_URL,
        config=Config(signature_version='s3v4')
    )
    result = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
    folders = set()
    for obj in result.get("Contents", []):
        parts = obj["Key"].split("/")
        if len(parts) >= 2:
            folders.add(parts[1])
    return sorted(folders)

# === Main ===
def main():
    log("🚀 Script triggered.")
    processed = load_processed()
    folders = list_roll_folders()

    for folder in folders:
        if folder in processed:
            log(f"⏭️ Already processed: {folder}")
            continue

        twin_sticker = folder.split("_")[-1].lstrip("0")
        record = find_airtable_record(twin_sticker)
        if not record:
            log(f"❌ No Airtable match for {twin_sticker}")
            continue

        if record['fields'].get('Email Sent'):
            log(f"⏭️ Already emailed: {twin_sticker}")
            continue

        email = record['fields'].get('Client Email')
        if not email:
            log(f"❌ Missing Client Email in Airtable record")
            continue

        password = generate_password()
        update_airtable_record(record['id'], {"Password": password})

        gallery_link = f"https://scans.gilplaquet.com/roll/{twin_sticker}"
        subject = f"Your Scans Are Ready - Roll {twin_sticker}"
        body = f"""
Hi there,

Good news! A roll you sent in for development just got scanned.
You can view and download your scans at the link below:

{gallery_link}

To access your gallery, use the password: {password}

This link will remain active for 7 days.

Thanks for sending in your film!

Gil Plaquet
www.gilplaquet.com
        """

        send_email(email, subject, body)
        update_airtable_record(record['id'], {"Email Sent": True})
        save_processed(folder)
        log(f"✅ Processed and emailed: {twin_sticker}")

# === Flask Routes ===
@app.route('/')
def index():
    return "🟢 Render is online."

@app.route('/trigger')
def trigger():
    if request.args.get("token") != TRIGGER_TOKEN:
        return "❌ Unauthorized", 403
    try:
        main()
        return "✅ Script ran successfully."
    except Exception as e:
        return f"❌ Script failed: {e}"

@app.route('/roll/<sticker>', methods=['GET', 'POST'])
def gallery(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    expected_password = record['fields'].get("Password")
    if request.method == "POST":
        if request.form.get("password") != expected_password:
            return "Incorrect password.", 403

        def find_folder_by_suffix(suffix):
            folders = list_roll_folders()
            for name in folders:
                if name.endswith(suffix.zfill(6)):
                    return name
            return None

        folder = find_folder_by_suffix(sticker)
        if not folder:
            return f"No folder found for sticker {sticker}.", 404

        prefix = f"rolls/{folder}/"
        s3 = boto3.client(
            's3',
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            endpoint_url=S3_ENDPOINT_URL,
            config=Config(signature_version='s3v4')
        )
        result = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
        image_files = [obj["Key"] for obj in result.get("Contents", []) if obj["Key"].lower().endswith(('.jpg', '.jpeg', '.png'))]
        CDN_BASE_URL = "https://cdn.gilplaquet.com"
        thumb_urls = []
        for file in image_files:
            if '/THUMB/' in file and file.lower().endswith(('.jpg', '.jpeg', '.png')):
                thumb_urls.append(f"{CDN_BASE_URL}/{file}")

        from datetime import datetime
        from datetime import datetime
        return print("DEBUG sticker:", sticker)
        print("DEBUG thumb_urls:", thumb_urls)
        print("DEBUG zip_url:", zip_url)
        render_template_string(template,
        sticker=sticker,
        thumb_urls=thumb_urls,
        zip_url=zip_url,
        current_year=datetime.now().year
    );
              justify-content: center;
              gap: 16px;
            }
            .gallery img {
              width: 100%;
              height: auto;
              border-radius: 6px;
              box-shadow: 0 1px 4px rgba(0,0,0,0.1);
            }
            .download {
              display: inline-block;
              margin-top: 40px;
              padding: 12px 24px;
              border: 2px solid #333;
              border-radius: 4px;
              text-decoration: none;
              color: #333;
              font-weight: bold;
              transition: background-color 0.3s ease, color 0.3s ease;
            }
            .download:hover {
              background-color: #333;
              color: #fff;
            }
            footer {
              margin-top: 60px;
              text-align: center;
              font-size: 0.9em;
              color: #888;
            }
          </style>
        </head>
        <body>
          <div class="container">
            <div style="text-align: center; margin-bottom: 20px;">
              <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; height: auto;">
            </div>
            <h1>Roll {{ sticker }}</h1>
            <div class="gallery">
              {% for thumb in thumb_urls %}
                <div class="thumb-tile">
                  <img src="{{ thumb }}" alt="Scan {{ loop.index }}" loading="lazy">
                </div>
              {% endfor %}
            </div>
            <div style="text-align: center;">
              <a class="download" href="{{ zip_url }}">Download All (ZIP)</a>
            </div>
            <footer>
              &copy; {{ current_year }} Gil Plaquet FilmLab
            </footer>
          </div>
        </body>
        </html>
        """,
        sticker=sticker,
        thumb_urls=thumb_urls,
        zip_url=zip_url,
        current_year=datetime.now().year
    ).year
        ).year,
        zip=zip
        )

    from datetime import datetime
    return template = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset='UTF-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>Roll {{ sticker }} – Gil Plaquet FilmLab</title>
    <style>
      body {
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
        background-color: #ffffff;
        color: #333333;
        margin: 0;
        padding: 0;
      }
      .container {
        max-width: 960px;
        margin: 0 auto;
        padding: 40px 20px;
        text-align: center;
      }
      h1 {
        font-size: 2em;
        margin-bottom: 1em;
      }
      .gallery {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        justify-content: center;
        gap: 16px;
      }
      .gallery img {
        width: 100%;
        height: auto;
        border-radius: 6px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.1);
      }
      .download {
        display: inline-block;
        margin-top: 40px;
        padding: 12px 24px;
        border: 2px solid #333;
        border-radius: 4px;
        text-decoration: none;
        color: #333;
        font-weight: bold;
        transition: background-color 0.3s ease, color 0.3s ease;
      }
      .download:hover {
        background-color: #333;
        color: #fff;
      }
      footer {
        margin-top: 60px;
        text-align: center;
        font-size: 0.9em;
        color: #888;
      }
    </style>
  </head>
  <body>
    <div class='container'>
      <div style='text-align: center; margin-bottom: 20px;'>
        <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' style='max-width: 200px; height: auto;'>
      </div>
      <h1>Roll {{ sticker }}</h1>
      <div class='gallery'>
        {% for thumb in thumb_urls %}
          <div class='thumb-tile'>
            <img src='{{ thumb }}' alt='Scan {{ loop.index }}' loading='lazy'>
          </div>
        {% endfor %}
      </div>
      <div style='text-align: center;'>
        <a class='download' href='{{ zip_url }}'>Download All (ZIP)</a>
      </div>
      <footer>
        &copy; {{ current_year }} Gil Plaquet FilmLab
      </footer>
    </div>
  </body>
</html>
"""
 sticker=sticker)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
