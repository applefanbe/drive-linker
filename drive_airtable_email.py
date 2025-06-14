import os
import smtplib
import requests
import base64
import sys
import random
import string
import time
import json
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, request, render_template_string, session, redirect, url_for
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
app.secret_key = os.getenv("SECRET_KEY") or "fallback-secret"

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

# === Airtable: Store print order in existing Rolls table ===
def store_print_order_in_roll(sticker, submitted_order, mollie_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    formula = f"{{Twin Sticker}}='{sticker}'"
    response = requests.get(url, headers=headers, params={"filterByFormula": formula})
    records = response.json().get("records", [])
    if not records:
        log(f"❌ No matching roll found for sticker {sticker}")
        return

    roll_id = records[0]["id"]
    patch_url = f"{url}/{roll_id}"
    patch_data = {
        "fields": {
            "Print Order JSON": json.dumps(submitted_order),
            "Mollie ID": mollie_id,
            "Print Order Paid": False
        }
    }
    patch_response = requests.patch(patch_url, headers=headers, json=patch_data)
    if patch_response.status_code == 200:
        log("✅ Print order saved to Rolls table.")
    else:
        log(f"❌ Failed to update Rolls record: {patch_response.text}")

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
    msg.set_content(body)  # fallback for plain-text clients

    # HTML version with inline styles for email compatibility
    body_html = body.replace('\n', '<br>')
    html_body = f"""
    <html>
    <body style="margin:0;padding:0;font-family:Helvetica,Arial,sans-serif;background:#fff;">
      <div style="width:100%;text-align:center;padding:40px 20px;">
        <div style="display:inline-block;text-align:left;max-width:600px;width:100%;">
          <div style="text-align:center;">
            <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="width:250px;margin-bottom:20px;">
          </div>
          <div style="font-size:16px;color:#333;line-height:1.5;">
            {body_html}
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    msg.add_alternative(html_body, subtype='html')

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
        order_link = f"https://scans.gilplaquet.com/roll/{twin_sticker}/order"
        subject = f"Your Scans Are Ready - Roll {twin_sticker}"
        body = f"""
Hi there,

Good news! A roll you sent in for development just got scanned.
You can view and download your scans as a .zip at the link below:

{gallery_link}

To access your gallery, use the password: {password}

Prints can be ordered from the gallery or through this link:

{order_link}

Thanks for sending in your film!

These links will remain active for 7 days.

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
    updated_time = record['fields'].get("Password Updated")

    if not expected_password or not updated_time:
        return "Missing password data.", 403

    try:
        password_age = (datetime.utcnow() - datetime.strptime(updated_time, "%Y-%m-%dT%H:%M:%S.%fZ")).total_seconds()
        if password_age > 604800:
            return "Password expired.", 403
    except Exception as e:
        return f"Invalid password timestamp format: {e}", 403

    if session.get(f"access_{sticker}") == expected_password:
        password_ok = True
    elif request.method == "POST" and request.form.get("password") == expected_password:
        session[f"access_{sticker}"] = expected_password
        password_ok = True
    else:
        password_ok = False

    if not password_ok:
        return render_template_string("""
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Enter Password – Roll {{ sticker }}</title>
          <style>
            body {
              font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
              background-color: #ffffff;
              color: #333333;
              margin: 0;
              padding: 0;
              text-align: center;
            }
            .container {
              max-width: 400px;
              margin: 100px auto;
              padding: 20px;
              border: 1px solid #ddd;
              border-radius: 8px;
              text-align: center;
            }
            img {
              max-width: 200px;
              height: auto;
              margin-bottom: 20px;
            }
            h2 {
              font-size: 1.5em;
              margin-bottom: 1em;
            }
            input[type="password"] {
              width: 100%;
              padding: 10px;
              font-size: 1em;
              margin-bottom: 1em;
              border: 1px solid #ccc;
              border-radius: 4px;
            }
            button {
              padding: 10px 20px;
              font-size: 1em;
              border: 2px solid #333;
              border-radius: 4px;
              background-color: #fff;
              color: #333;
              cursor: pointer;
              transition: background-color 0.3s ease, color 0.3s ease;
            }
            button:hover {
              background-color: #333;
              color: #fff;
            }
          </style>
        </head>
        <body>
          <div class="container">
            <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo">
            <h2>Enter password to access Roll {{ sticker }}</h2>
            <form method="POST" style="display: flex; flex-direction: column; align-items: center; gap: 16px;">
              <input type="password" name="password" placeholder="Password" required style="width: 100%; max-width: 300px; padding: 10px; font-size: 1em; border: 1px solid #ccc; border-radius: 4px;">
              <button type="submit">Submit</button>
            </form>
          </div>
        </body>
        </html>
        """, sticker=sticker)

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
    image_urls = [generate_signed_url(f) for f in image_files]
    zip_url = generate_signed_url(f"{prefix}{sticker}.zip")

    comment = record['fields'].get('Comment', '').strip()

    return render_template_string("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Roll {{ sticker }} – Gil Plaquet FilmLab</title>
      <style>
        body {
          font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
          background-color: #ffffff;
          color: #333333;
          margin: 0;
          padding: 0;
          text-align: center;
        }
        .container {
          max-width: 960px;
          margin: 0 auto;
          padding: 40px 20px;
        }
        h1 {
          font-size: 2em;
          margin-bottom: 0.5em;
        }
        .gallery {
          display: flex;
          flex-wrap: wrap;
          justify-content: center;
          gap: 10px;
          margin-top: 30px;
        }
        .gallery-item {
          width: 260px;
          height: 260px;
          background-color: #f8f8f8;
          border-radius: 8px;
          overflow: hidden;
          display: flex;
          align-items: center;
          justify-content: center;
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
        }
        .gallery-item img {
          max-width: 100%;
          max-height: 100%;
          object-fit: contain;
          display: block;
        }
        .download {
          display: inline-block;
          margin-bottom: 30px;
          padding: 12px 24px;
          border: 2px solid #333333;
          border-radius: 4px;
          text-decoration: none;
          color: #333333;
          font-weight: bold;
          transition: background-color 0.3s ease, color 0.3s ease;
        }
        .download:hover {
          background-color: #333333;
          color: #ffffff;
        }
        footer {
          margin-top: 60px;
          font-size: 0.9em;
          color: #888888;
        }
        .roll-info {
          font-size: 1.1em;
          margin: 20px 0;
          line-height: 1.6;
        }
        .roll-info span {
          display: block;
        }
        @media (min-width: 600px) {
          .roll-info span {
            display: inline;
          }
          .roll-info span:not(:last-child)::after {
            content: " – ";
          }
        }
      </style>
    </head>
    <body>
      <div class="container">
        <div>
          <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; height: auto; margin-bottom: 20px;">
        </div>
        <a class="download" href="{{ zip_url }}">Download All (ZIP)</a>
        <a class="download" href="/roll/{{ sticker }}/order">Order Prints</a>
        <div class="roll-info">
          <span><strong>Roll:</strong> {{ sticker }}</span>
          {% if record['fields'].get('Size') %}
            <span><strong>Size:</strong> {{ record['fields']['Size'] }}</span>
          {% endif %}
          {% if record['fields'].get('Stock') %}
            <span><strong>Film Stock:</strong> {{ record['fields']['Stock'][0] }}</span>
          {% endif %}
          {% if record['fields'].get('Scan') %}
            <span><strong>Scan:</strong> {{ record['fields']['Scan'] }}</span>
          {% endif %}
          {% if comment %}
            <br><span><strong>Comment:</strong> {{ comment }}</span>
          {% endif %}
        </div>
        <div class="gallery">
          {% for url in image_urls %}
            <div class="gallery-item">
              <img src="{{ url }}" alt="Scan {{ loop.index }}">
            </div>
          {% endfor %}
        </div>    
        <footer>
          &copy; {{ current_year }} Gil Plaquet
        </footer>
      </div>
    </body>
    </html>
    """, 
    sticker=sticker, 
    image_urls=image_urls, 
    zip_url=zip_url, 
    current_year=datetime.now().year,
    record=record,
    comment=comment
    )

@app.route('/roll/<sticker>/order', methods=['GET', 'POST'])
def order_page(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    expected_password = record['fields'].get("Password")
    updated_time = record['fields'].get("Password Updated")

    if not expected_password or not updated_time:
        return "Missing password data.", 403

    try:
        password_age = (datetime.utcnow() - datetime.strptime(updated_time, "%Y-%m-%dT%H:%M:%S.%fZ")).total_seconds()
        if password_age > 604800:
            return "Password expired.", 403
    except Exception as e:
        return f"Invalid password timestamp format: {e}", 403

    if session.get(f"access_{sticker}") == expected_password:
        password_ok = True
    elif request.method == "POST" and request.form.get("password") == expected_password:
        session[f"access_{sticker}"] = expected_password
        password_ok = True
    else:
        password_ok = False

    if not password_ok:
        return render_template_string("""
        <!DOCTYPE html>
        <html lang=\"en\">
        <head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
        <title>Enter Password – Roll {{ sticker }}</title>
        <style>body { font-family: Helvetica; text-align: center; margin-top: 100px; }
        input, button { padding: 10px; font-size: 1em; margin-top: 10px; }</style>
        </head>
        <body>
          <h2>Enter password to access Roll {{ sticker }}</h2>
          <form method=\"POST\">
            <input type=\"password\" name=\"password\" placeholder=\"Password\" required>
            <br>
            <button type=\"submit\">Submit</button>
          </form>
        </body></html>
        """, sticker=sticker)

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
    image_urls = [generate_signed_url(f) for f in image_files]

    film_size = record['fields'].get("Size", "")
    scan_type = record['fields'].get("Scan", "")

    is_half_frame = film_size == "Half Frame"
    is_standard_35mm = film_size == "35mm"
    image_count = len(image_urls)

    show_whole_roll_buttons = (is_standard_35mm and image_count >= 20) or (is_half_frame and image_count >= 20)
    show_select_all_button = not show_whole_roll_buttons
    allow_border_option = "Hires" in scan_type

    if is_half_frame:
        roll_label = "Half-Frame Roll"
        if image_count > 55:
            price_cap = "€25"
            cap_explainer = "10x15 prints of all images capped at €25. Extra 10x15 prints: €0.50 each. Other sizes full price."
        else:
            price_cap = "€20"
            cap_explainer = "10x15 prints of all images capped at €20. Extra 10x15 prints: €0.50 each. Other sizes full price."
    elif is_standard_35mm:
        roll_label = "Whole Roll"
        if image_count > 30:
            price_cap = "€15"
            cap_explainer = "10x15 prints of all images capped at €15. Extra 10x15 prints: €0.50 each. Other sizes full price."
        else:
            price_cap = "€10"
            cap_explainer = "10x15 prints of all images capped at €10. Extra 10x15 prints: €0.50 each. Other sizes full price."
    else:
        roll_label = "Roll"
        price_cap = "€0"
        cap_explainer = "Cap rules apply to 10x15 prints only depending on film type and number of images."

    return render_template_string("""<!DOCTYPE html>
<html><head><meta charset=\"UTF-8\">
<title>Select Prints – Roll {{ sticker }}</title>
<style>
body { font-family: Helvetica; background-color: #fff; color: #333; margin: 0; padding: 0; }
.container { max-width: 1280px; margin: auto; padding: 40px 20px; text-align: center; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.grid-item { border: 1px solid #eee; border-radius: 6px; padding: 8px; }
.grid-item img { height: 150px; width: auto; display: block; margin: 0 auto 8px auto; object-fit: contain; }
.button-row { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-bottom: 20px; }
button { padding: 10px 18px; font-size: 0.95em; border: 2px solid #333; background: #fff; color: #333; cursor: pointer; border-radius: 4px; }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button:hover:enabled { background: #333; color: #fff; }
.note { font-size: 0.95em; margin-top: 10px; color: #666; }
.download { display: inline-block; margin-bottom: 20px; padding: 10px 16px; border: 2px solid #333; border-radius: 4px; text-decoration: none; color: #333; }
.download:hover { background-color: #333; color: #fff; }
</style>
<script>
function submitWholeRoll(paperType) {
  const isHalfFrame = {{ 'true' if is_half_frame else 'false' }};
  const imageCount = {{ image_count }};
  let cap = 0;
  if (isHalfFrame) {
    cap = imageCount > 55 ? 25 : 20;
  } else {
    cap = imageCount > 30 ? 15 : 10;
  }
  const label = isHalfFrame ? "half-frame roll" : "roll";
  if (!confirm(`This will print the entire ${label} on 10x15 ${paperType} paper. Each print normally costs €0.75. As you've selected all images, the total is capped at €${cap}. Continue?`)) return;
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = `/roll/{{ sticker }}/submit-order`;
  document.querySelectorAll('input[name="selected_images"]').forEach((cb, i) => {
    const url = cb.value;
    form.innerHTML += `<input type="hidden" name="order[${i}][url]" value="${url}">`;
    form.innerHTML += `<input type="hidden" name="order[${i}][size]" value="10x15">`;
    form.innerHTML += `<input type="hidden" name="order[${i}][paper]" value="${paperType}">`;
    form.innerHTML += `<input type="hidden" name="order[${i}][border]" value="No">`;
  });
  document.body.appendChild(form); form.submit();
}
function selectAllImages() {
  document.querySelectorAll('input[name="selected_images"]').forEach(cb => cb.checked = true);
  updateSubmitState();
}
function deselectAllImages() {
  document.querySelectorAll('input[name="selected_images"]').forEach(cb => cb.checked = false);
  updateSubmitState();
}
function updateSubmitState() {
  const count = document.querySelectorAll('input[name="selected_images"]:checked').length;
  document.getElementById('nextButton').disabled = count === 0;
  const topBtn = document.getElementById('topOrderButton');
  if (topBtn) topBtn.disabled = count === 0;
}
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[name="selected_images"]').forEach(input => {
    input.addEventListener('change', updateSubmitState);
  });
  updateSubmitState();
});
</script></head><body>
<div class="container">
  <div><img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; margin-bottom: 20px;"></div>
  <a class="download" href="/roll/{{ sticker }}">&larr; Back to Gallery</a>
  <p class="note"><strong>{{ cap_explainer }}</strong></p>
  <form method="POST" action="/roll/{{ sticker }}/submit-order">
    <div class="button-row">
      {% if show_whole_roll_buttons %}
        <button type="button" onclick="submitWholeRoll('Matte')">Print {{ roll_label }} on 10x15 Matte ({{ price_cap }})</button>
        <button type="button" onclick="submitWholeRoll('Glossy')">Print {{ roll_label }} on 10x15 Glossy ({{ price_cap }})</button>
        <button type="button" onclick="submitWholeRoll('Luster')">Print {{ roll_label }} on 10x15 Luster ({{ price_cap }})</button>
      {% elif show_select_all_button %}
        <button type="button" onclick="selectAllImages()">Select All</button>
        <button type="button" onclick="deselectAllImages()">Deselect All</button>
      {% endif %}
      <button type="submit" id="topOrderButton">Order Selected Prints</button>
    </div>
    <p class="note">Select your prints below</p>
    <div class="grid">
      {% for url in image_urls %}
      <div class="grid-item">
        <label style="cursor: pointer; display: block;">
          <img src="{{ url }}" alt="Scan {{ loop.index }}">
          <input type="checkbox" name="selected_images" value="{{ url }}" style="margin-top: 6px;">
        </label>
      </div>
      {% endfor %}
    </div>
    <div style="margin: 40px 0;"></div>
    <button id="nextButton" type="submit">Order Selected Prints</button>
  </form>
</div>
</body>
</html>
""",
    sticker=sticker,
    image_urls=image_urls,
    is_half_frame=is_half_frame,
    roll_label=roll_label,
    show_whole_roll_buttons=show_whole_roll_buttons,
    show_select_all_button=show_select_all_button,
    allow_border_option=allow_border_option,
    image_count=image_count,
    price_cap=price_cap,
    cap_explainer=cap_explainer)

@app.route('/roll/<sticker>/submit-order', methods=['POST'])
def submit_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    scan_level = record['fields'].get('Scan', '')
    allow_border_option = 'Hires' in scan_level
    film_size = record['fields'].get('Size', '')

    submitted_order = []
    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            submitted_order.append({
                'url': request.form.get(f'order[{index}][url]'),
                'size': request.form.get(f'order[{index}][size]', '10x15'),
                'paper': request.form.get(f'order[{index}][paper]', 'Glossy'),
                'border': request.form.get(f'order[{index}][border]', 'No'),
                'quantity': int(request.form.get(f'order[{index}][quantity]', '1'))
            })

    if not submitted_order:
        return "No images selected.", 400

    expanded_order = []
    for item in submitted_order:
        for _ in range(item['quantity']):
            expanded_order.append({
                'url': item['url'],
                'size': item['size'],
                'paper': item['paper'],
                'border': item['border']
            })

    gallery_count = len({item['url'] for item in expanded_order})
    if film_size == '35mm':
        cap_amount = 10 if gallery_count <= 30 else 15
        cap_limit = gallery_count
        cap_explainer = f"10x15 prints of all images capped at €{cap_amount}. Extra 10x15 prints: €0.50. Other sizes full price."
    elif film_size == 'Half Frame':
        cap_amount = 20 if gallery_count <= 55 else 25
        cap_limit = gallery_count
        cap_explainer = f"10x15 prints of all images capped at €{cap_amount}. Extra 10x15 prints: €0.50. Other sizes full price."
    else:
        cap_amount = None
        cap_limit = 0
        cap_explainer = "Caps apply only to 10x15 prints depending on film type and image count."

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Confirm Order – Roll {{ sticker }}</title>
      <style>
        body { font-family: Helvetica, sans-serif; background: #fff; color: #333; margin: 0; padding: 0; }
        .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; text-align: center; }
        .controls { display: flex; justify-content: center; flex-wrap: wrap; gap: 12px; margin-bottom: 30px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 40px; }
        .grid-item { border: 1px solid #ccc; border-radius: 6px; padding: 12px; text-align: center; }
        .grid-item img { max-height: 180px; width: auto; margin-bottom: 10px; }
        .selectors { display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
        .price-tag { font-size: 0.95em; color: #555; margin-top: 4px; }
        select, input[type=number] { padding: 4px 6px; font-size: 0.95em; }
        .actions { margin: 20px 0; display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; }
        .total-price { font-size: 1.2em; margin-top: 10px; }
        button, .button-link {
          padding: 10px 20px;
          font-size: 1em;
          border: 2px solid #333;
          border-radius: 4px;
          background: #fff;
          color: #333;
          cursor: pointer;
          text-decoration: none;
        }
        button:hover, .button-link:hover {
          background: #333;
          color: #fff;
        }
      </style>
      <script>
        function getPrice(size) {
          if (size === '10x15') return 0.75;
          if (size === 'A6') return 1.5;
          if (size === 'A5') return 3.0;
          if (size === 'A4') return 6.0;
          if (size === 'A3') return 12.0;
          return 0;
        }
        function updateTotal() {
          let total = 0;
          let count_10x15 = 0;
          const capAmount = {{ cap_amount if cap_amount else 'null' }};
          const capLimit = {{ cap_limit }};
          document.querySelectorAll('[data-row]').forEach(row => {
            const size = row.querySelector('.size').value;
            const quantity = parseInt(row.querySelector('.quantity').value);
            const price = getPrice(size);
            let effectivePrice = price;
            if (size === '10x15') {
              if (capAmount !== null) {
                const base = Math.min(quantity, capLimit - count_10x15);
                const extra = quantity - base;
                total += 0.5 * extra;
                count_10x15 += quantity;
              } else {
                total += price * quantity;
              }
            } else {
              total += price * quantity;
            }
            row.querySelector('.price-tag').textContent = `€${(price * quantity).toFixed(2)}`;
          });
          if (capAmount !== null && count_10x15 > 0) {
            const base = Math.min(count_10x15, capLimit);
            const extra = count_10x15 - base;
            total = capAmount + (extra * 0.5);
          }
          document.getElementById('totalDisplay').textContent = `Your order total is €${total.toFixed(2)}`;
        }
        document.addEventListener('DOMContentLoaded', () => {
          document.querySelectorAll('select, input.quantity').forEach(el => el.addEventListener('change', updateTotal));
          updateTotal();
        });
      </script>
    </head>
    <body>
      <div class="container">
        <div><img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; margin-bottom: 20px;"></div>
        <h1>Confirm Your Print Order – Roll {{ sticker }}</h1>
        <p><strong>{{ cap_explainer }}</strong></p>
        <p class="total-price" id="totalDisplay">Your order total is €0.00</p>
        <form method="POST" action="/roll/{{ sticker }}/review-order">
          <div class="actions">
            <a class="button-link" href="/roll/{{ sticker }}/order">← Back to Selection</a>
            <button type="submit">Review & Pay</button>
          </div>
          <div class="grid">
            {% for item in expanded_order %}
              <div class="grid-item" data-row>
                <img src="{{ item.url }}">
                <div class="selectors">
                  <select name="order[{{ loop.index0 }}][size]" class="size">
                    <option {% if item.size == '10x15' %}selected{% endif %}>10x15</option>
                    <option {% if item.size == 'A6' %}selected{% endif %}>A6</option>
                    <option {% if item.size == 'A5' %}selected{% endif %}>A5</option>
                    <option {% if item.size == 'A4' %}selected{% endif %}>A4</option>
                    <option {% if item.size == 'A3' %}selected{% endif %}>A3</option>
                  </select>
                  <select name="order[{{ loop.index0 }}][paper]" class="paper">
                    <option {% if item.paper == 'Glossy' %}selected{% endif %}>Glossy</option>
                    <option {% if item.paper == 'Matte' %}selected{% endif %}>Matte</option>
                    <option {% if item.paper == 'Luster' %}selected{% endif %}>Luster</option>
                  </select>
                  {% if allow_border_option %}
                  <select name="order[{{ loop.index0 }}][border]" class="border">
                    <option value="No" {% if item.border == 'No' %}selected{% endif %}>No Scan Border</option>
                    <option value="Yes" {% if item.border == 'Yes' %}selected{% endif %}>Print Scan Border</option>
                  </select>
                  {% else %}
                  <input type="hidden" name="order[{{ loop.index0 }}][border]" value="No">
                  {% endif %}
                  <input type="number" name="order[{{ loop.index0 }}][quantity]" class="quantity" value="1" min="1" style="width: 60px;">
                </div>
                <div class="price-tag">€0.00</div>
                <input type="hidden" name="order[{{ loop.index0 }}][url]" value="{{ item.url }}">
              </div>
            {% endfor %}
          </div>
          <div style="margin-bottom: 40px;"></div>
          <button type="submit">Review & Pay</button>
        </form>
      </div>
    </body>
    </html>
    """,
        sticker=sticker,
        expanded_order=expanded_order,
        allow_border_option=allow_border_option,
        cap_explainer=cap_explainer,
        cap_amount=cap_amount,
        cap_limit=cap_limit)

@app.route('/roll/<sticker>/review-order', methods=['POST'])
def review_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    scan_quality = record['fields'].get('Scan', '')
    allow_border = 'Hires' in scan_quality
    film_size = record['fields'].get('Size', '')

    submitted_order = []
    type_counter = {}
    subtotal = 0.0
    tax_rate = 0.21
    count_10x15 = 0
    gallery_image_urls = set()

    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            url = request.form.get(f'order[{index}][url]')
            size = request.form.get(f'order[{index}][size]', '10x15')
            paper = request.form.get(f'order[{index}][paper]', 'Glossy')
            border = request.form.get(f'order[{index}][border]', 'No')
            quantity = int(request.form.get(f'order[{index}][quantity]', '1'))

            gallery_image_urls.add(url)

            if size == '10x15':
                count_10x15 += quantity
                price = 0.75 * quantity
            elif size == 'A6':
                price = 1.5 * quantity
            elif size == 'A5':
                price = 3.0 * quantity
            elif size == 'A4':
                price = 6.0 * quantity
            elif size == 'A3':
                price = 12.0 * quantity
            else:
                price = 0.0

            key_type = f"{quantity}× {size} - {paper}"
            type_counter[key_type] = type_counter.get(key_type, 0) + 1
            submitted_order.append({'url': url, 'size': size, 'paper': paper, 'border': border, 'price': price, 'quantity': quantity})
            subtotal += price

    gallery_count = len(gallery_image_urls)

    if film_size == 'Half Frame':
        cap_amount = 20 if gallery_count <= 55 else 25
    elif film_size == '35mm':
        cap_amount = 10 if gallery_count <= 30 else 15
    else:
        cap_amount = None

    if cap_amount is not None and count_10x15 > 0:
        base_10x15 = min(count_10x15, gallery_count)
        extra_10x15 = count_10x15 - base_10x15
        subtotal = cap_amount + (extra_10x15 * 0.5)
        cap_explainer = f"10x15 prints of all images capped at €{cap_amount}. Extra 10x15 prints: €0.50. Other sizes full price."
    else:
        cap_explainer = "No cap applied."

    tax = subtotal * tax_rate / (1 + tax_rate)
    total = subtotal

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset=\"UTF-8\">
      <title>Review Print Order – Roll {{ sticker }}</title>
      <style>
        body { font-family: Helvetica, sans-serif; background-color: #fff; color: #333; margin: 0; padding: 0; }
        .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; text-align: center; }
        .summary { margin: 20px 0 30px 0; }
        .summary h2 { margin-bottom: 10px; }
        .summary p { margin: 4px 0; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
        .grid-item { border: 1px solid #ccc; border-radius: 6px; padding: 10px; }
        .grid-item img { height: 180px; width: auto; object-fit: contain; display: block; margin: 0 auto 10px auto; }
        .button-row { margin-top: 20px; display: flex; justify-content: center; gap: 10px; flex-wrap: wrap; }
        .button-row button {
          padding: 12px 24px; font-size: 1em; border: 2px solid #333; border-radius: 4px;
          background: #fff; color: #333; cursor: pointer; transition: background 0.3s, color 0.3s;
        }
        .button-row button:hover, .button-row button:active { background: #333; color: #fff; }
        .logo { max-width: 200px; margin: 20px auto 30px; display: block; }
      </style>
    </head>
    <body>
      <div class=\"container\">
        <div><img src=\"https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png\" alt=\"Logo\" class=\"logo\"></div>
        <h1>Review Your Print Order – Roll {{ sticker }}</h1>
        <p><strong>{{ cap_explainer }}</strong></p>
        <div class=\"summary\">
          <h2>Total: €{{ '%.2f'|format(total) }} (incl. VAT)</h2>
          <form method=\"POST\">
            {% for item in submitted_order %}
              <input type=\"hidden\" name=\"order[{{ loop.index0 }}][url]\" value=\"{{ item.url }}\">
              <input type=\"hidden\" name=\"order[{{ loop.index0 }}][size]\" value=\"{{ item.size }}\">
              <input type=\"hidden\" name=\"order[{{ loop.index0 }}][paper]\" value=\"{{ item.paper }}\">
              <input type=\"hidden\" name=\"order[{{ loop.index0 }}][quantity]\" value=\"{{ item.quantity }}\">
              {% if allow_border %}
                <input type=\"hidden\" name=\"order[{{ loop.index0 }}][border]\" value=\"{{ item.border }}\">
              {% endif %}
            {% endfor %}
            <div class=\"button-row\">
              <button type=\"submit\" formaction=\"/roll/{{ sticker }}/submit-order\">\u2190 Back to Edit</button>
              <button type=\"submit\" formaction=\"/roll/{{ sticker }}/finalize-order\">Pay with Mollie</button>
            </div>
          </form>
          <h3 style=\"margin-top:30px;\">Order Breakdown:</h3>
          {% for type, count in type_counter.items() %}
            <p>{{ type }}</p>
          {% endfor %}
          <p>Subtotal (excl. VAT): €{{ '%.2f'|format(total - tax) }}</p>
          <p>VAT (21%): €{{ '%.2f'|format(tax) }}</p>
        </div>
        <div class=\"grid\">
          {% for item in submitted_order %}
            <div class=\"grid-item\">
              <img src=\"{{ item.url }}\">
              <p>{{ item.quantity }}x {{ item.size }} – {{ item.paper }}<br>€{{ '%.2f'|format(item.price) }}</p>
            </div>
          {% endfor %}
        </div>
      </div>
    </body>
    </html>
    """,
    sticker=sticker,
    submitted_order=submitted_order,
    total=total,
    tax=tax,
    type_counter=type_counter,
    allow_border=allow_border,
    cap_explainer=cap_explainer)

@app.route('/roll/<sticker>/finalize-order', methods=['POST'])
def finalize_order(sticker):
    from mollie.api.client import Client as MollieClient

    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    mollie_api_key = os.getenv("MOLLIE_API_KEY")
    if not mollie_api_key:
        return "Mollie API key not set.", 500

    mollie_client = MollieClient()
    mollie_client.set_api_key(mollie_api_key)

    submitted_order = []
    total = 0.0
    count_10x15 = 0
    gallery_image_urls = set()

    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            url = request.form.get(f'order[{index}][url]')
            size = request.form.get(f'order[{index}][size]', '10x15')
            paper = request.form.get(f'order[{index}][paper]', 'Glossy')
            border = request.form.get(f'order[{index}][border]', 'No')
            quantity = int(request.form.get(f'order[{index}][quantity]', '1'))

            submitted_order.append({
                'url': url,
                'size': size,
                'paper': paper,
                'border': border,
                'quantity': quantity
            })

            if size == '10x15':
                count_10x15 += quantity
                total += 0.75 * quantity
                gallery_image_urls.add(url)
            elif size == 'A6':
                total += 1.5 * quantity
            elif size == 'A5':
                total += 3.0 * quantity
            elif size == 'A4':
                total += 6.0 * quantity
            elif size == 'A3':
                total += 12.0 * quantity

    roll_size = record['fields'].get('Size', '')
    gallery_count = len(gallery_image_urls)

    if roll_size == 'Half Frame':
        cap_amount = 20 if gallery_count <= 55 else 25
    elif roll_size == '35mm':
        cap_amount = 10 if gallery_count <= 30 else 15
    else:
        cap_amount = None

    if cap_amount is not None and count_10x15 > 0:
        base_10x15 = min(count_10x15, gallery_count)
        extra_10x15 = count_10x15 - base_10x15
        capped_total = cap_amount + (extra_10x15 * 0.5)
    else:
        capped_total = total

    description = f"Print order for roll {sticker}"
    redirect_url = f"https://scans.gilplaquet.com/roll/{sticker}/thank-you"
    webhook_url = "https://scans.gilplaquet.com/mollie-webhook"

    try:
        payment = mollie_client.payments.create({
            "amount": {
                "currency": "EUR",
                "value": f"{capped_total:.2f}"
            },
            "description": description,
            "redirectUrl": redirect_url,
            "webhookUrl": webhook_url,
            "metadata": {
                "sticker": sticker
            }
        })

        store_print_order_in_roll(sticker, submitted_order, payment.id)
        return redirect(payment.checkout_url)

    except Exception as e:
        return f"Payment creation failed: {e}", 500

@app.route('/roll/<sticker>/thank-you')
def thank_you(sticker):
    record = find_airtable_record(sticker)
    if not record or 'fields' not in record:
        return "Roll not found or incomplete.", 404

    fields = record['fields']
    raw_email = fields.get('Client Email', 'your email')
    email = str(raw_email).strip('"').strip("'") if raw_email else 'your email'
    print_order = fields.get('Print Order', [])

    subtotal = 0.0
    for item in print_order:
        quantity = int(item.get('quantity', 1))
        size = item.get('size', '10x15')
        if size == '10x15':
            subtotal += 0.75 * quantity
        elif size == 'A6':
            subtotal += 1.5 * quantity
        elif size == 'A5':
            subtotal += 3.0 * quantity
        elif size == 'A4':
            subtotal += 6.0 * quantity
        elif size == 'A3':
            subtotal += 12.0 * quantity

    tax_rate = 0.21
    tax = subtotal * tax_rate / (1 + tax_rate)
    total = subtotal

    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset=\"UTF-8\">
        <title>Thank You – Roll {sticker}</title>
        <style>
            body {{
                font-family: Helvetica, sans-serif;
                background-color: #ffffff;
                margin: 0;
                padding: 0;
                color: #333;
                text-align: center;
            }}
            .wrapper {{
                padding: 60px 20px;
                max-width: 700px;
                margin: auto;
            }}
            h1 {{
                font-size: 2em;
                margin-bottom: 20px;
            }}
            p {{
                font-size: 1.1em;
                margin-bottom: 20px;
                line-height: 1.5;
            }}
            img.logo {{
                max-width: 200px;
                margin-bottom: 30px;
            }}
            img.thumb {{
                max-width: 120px;
                height: auto;
                margin: 8px;
                border: 1px solid #ccc;
                border-radius: 4px;
            }}
            a.button {{
                display: inline-block;
                padding: 10px 20px;
                font-size: 1em;
                border: 2px solid #333;
                border-radius: 4px;
                background: #fff;
                color: #333;
                text-decoration: none;
                margin-top: 30px;
            }}
            a.button:hover {{
                background: #333;
                color: #fff;
            }}
            .order-summary {{
                margin-top: 40px;
                text-align: left;
            }}
            .order-summary h2 {{
                font-size: 1.2em;
                border-bottom: 1px solid #ccc;
                padding-bottom: 6px;
                margin-bottom: 12px;
            }}
        </style>
    </head>
    <body>
        <div class=\"wrapper\">
            <img class='logo' src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo'>
            <h1>Thank you for your order!</h1>
            <p>Your payment was successful and your print order for roll <strong>{sticker}</strong> has been received.</p>
            <p>You’ll receive a confirmation email shortly at <strong>{email}</strong>.</p>
            <a class=\"button\" href='/roll/{sticker}'>← Back to Gallery</a>

            <div class=\"order-summary\">
                <h2>Print Order Summary:</h2>
                {''.join(f"<p>{item.get('quantity', 1)}× {item.get('size', '')} – {item.get('paper', '')} – Border: {item.get('border', 'No')}</p><img class='thumb' src='{item.get('url', '')}'>" for item in print_order)}
                <p><strong>Total (incl. VAT):</strong> €{total:.2f}</p>
                <p><strong>VAT (21%):</strong> €{tax:.2f}</p>
            </div>
        </div>
    </body>
    </html>
    """)


@app.route('/mollie-webhook', methods=['POST'])
def mollie_webhook():
    mollie_api_key = os.getenv("MOLLIE_API_KEY")
    if not mollie_api_key:
        return "API key missing", 500

    from mollie.api.client import Client as MollieClient
    from urllib.parse import urlparse, unquote
    mollie_client = MollieClient()
    mollie_client.set_api_key(mollie_api_key)

    payment_id = request.form.get("id")
    if not payment_id:
        return "Missing payment ID", 400

    try:
        payment = mollie_client.payments.get(payment_id)
        if not payment.is_paid():
            return "Payment not completed", 200

        airtable_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
        headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
        formula = f"{{Mollie ID}}='{payment_id}'"
        response = requests.get(airtable_url, headers=headers, params={"filterByFormula": formula})
        records = response.json().get("records", [])
        if not records:
            return "Order not found", 404

        record = records[0]
        fields = record["fields"]
        sticker = fields.get("Twin Sticker")
        client_email = fields.get("Client Email")
        client_name = fields.get("Client Name", "Client")
        submitted_order = json.loads(fields.get("Print Order JSON", "[]"))

        # Mark as Paid
        update_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record['id']}"
        update_response = requests.patch(update_url, headers=headers, json={"fields": {"Print Order Paid": True}})

        # Calculate pricing breakdown
        type_counter = {}
        subtotal = 0.0
        count_10x15 = 0
        tax_rate = 0.21

        for item in submitted_order:
            size = item['size']
            paper = item['paper']
            price = 0.0
            if size == '10x15':
                price = 0.75
                count_10x15 += 1
            elif size == 'A6': price = 1.5
            elif size == 'A5': price = 3.0
            elif size == 'A4': price = 6.0
            elif size == 'A3': price = 12.0

            key = f"{size} - {paper}"
            type_counter[key] = type_counter.get(key, 0) + 1
            subtotal += price

        if count_10x15 >= 20:
            subtotal = min(subtotal, 15.0)

        tax = subtotal * tax_rate / (1 + tax_rate)
        total = subtotal

        # Compose customer email
        email_body = f"""
        <div style='text-align: center;'>
          <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' style='max-width: 200px; margin-bottom: 20px;'>
        </div>
        <div style='font-family: Helvetica, sans-serif; font-size: 16px;'>
        <p>Hi there,</p>
        <p>Thank you for your print order. Here’s a summary of what you selected for roll <strong>{sticker}</strong>:</p>
        <ul>
        """
        for item in submitted_order:
            email_body += f"<li><img src='{item['url']}' width='100'><br>{item['size']} – {item['paper']}, Include Scan Border: {item['border']}</li>"
        email_body += "</ul>"
        email_body += f"<p><strong>Delivery Method:</strong> {fields.get('Delivery Method', 'N/A')}</p>"
        email_body += "<p><strong>Order Breakdown:</strong></p><ul>"
        for type, count in type_counter.items():
            email_body += f"<li>{count} × {type}</li>"
        email_body += f"</ul><p>Subtotal (excl. VAT): €{subtotal - tax:.2f}<br>VAT (21%): €{tax:.2f}<br><strong>Total: €{total:.2f}</strong></p>"
        email_body += "<p>The order was successfully paid through Mollie.</p><p>We’ll start printing soon!<br>We'll notify you when your prints are ready for pickup at the lab or your drop-off point.</p></div>"

        msg = EmailMessage()
        msg["From"] = "Gil Plaquet FilmLab <filmlab@gilplaquet.com>"
        msg["To"] = client_email
        msg["Bcc"] = "filmlab@gilplaquet.com"
        msg["Subject"] = f"Print Order Confirmation – Roll {sticker}"
        msg.set_content("Your order is confirmed.")
        msg.add_alternative(f"<html><body>{email_body}</body></html>", subtype="html")

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        # Internal notification email
        internal_msg = EmailMessage()
        internal_msg["From"] = "Gil Plaquet FilmLab <filmlab@gilplaquet.com>"
        internal_msg["To"] = "filmlab@gilplaquet.com"
        internal_msg["Subject"] = f"A new print order for roll {sticker}"

        internal_body = f"<h3>Roll {sticker} – Print Order Summary</h3><ul>"
        for item in submitted_order:
            parsed_url = urlparse(item['url'])
            filename = unquote(parsed_url.path.split("/")[-1].split("?")[0])
            internal_body += (
                f"<li><strong>{filename}</strong><br>"
                f"{item['size']} – {item['paper']}, Border: {item['border']}<br>"
                f"<img src='{item['url']}' width='100'></li>"
            )
        internal_body += "</ul>"

        internal_msg.set_content("New print order received.")
        internal_msg.add_alternative(f"<html><body>{internal_body}</body></html>", subtype="html")

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(internal_msg)

        return "OK", 200

    except Exception as e:
        return f"Webhook error: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
