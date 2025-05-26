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

# === Airtable: Add to Print Orders Table ===
def create_print_order_record(sticker, client_email, submitted_order, mollie_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Print%20Orders"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    fields = {
        "Sticker": sticker,
        "Client Email": client_email,
        "Order JSON": json.dumps(submitted_order),
        "Mollie ID": mollie_id,
        "Paid": False
    }
    response = requests.post(url, headers=headers, json={"fields": fields})
    if response.status_code == 200:
        log("✅ Print order stored in Airtable.")
    else:
        log(f"❌ Failed to store print order: {response.text}")

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
    <div style='font-family: sans-serif;'>{body_html}</div>
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

Prints can be ordered through this link:

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
            <form method="POST">
              <input type="password" name="password" placeholder="Password" required>
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
    zip_url = generate_signed_url(f"{prefix}Archive.zip")

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
        }
        .container {
          max-width: 960px;
          margin: 0 auto;
          padding: 40px 20px;
          text-align: center;
        }
        h1 {
          font-size: 2em;
          margin-bottom: 0.5em;
        }
        .gallery {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 10px;
          margin-top: 30px;
        }
        .gallery img {
          width: 100%;
          height: auto;
          display: block;
          border-radius: 8px;
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
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
      </style>
    </head>
    <body>
      <div class="container">
        <div>
          <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; height: auto; margin-bottom: 20px;">
        </div>
        <a class="download" href="{{ zip_url }}">Download All (ZIP)</a>
        <a class="download" href="/roll/{{ sticker }}/order">Order Prints</a>
        <h1>Roll {{ sticker }}</h1>
        <div class="gallery">
          {% for url in image_urls %}
            <img src="{{ url }}" alt="Scan {{ loop.index }}">
          {% endfor %}
        </div>
        <footer>
          &copy; {{ current_year }} Gil Plaquet
        </footer>
      </div>
    </body>
    </html>
    """, sticker=sticker, image_urls=image_urls, zip_url=zip_url, current_year=datetime.now().year)

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
        return render_template_string("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Enter Password – Roll {{ sticker }}</title>
<style>
body { font-family: Helvetica, sans-serif; background: #fff; color: #333; }
.container { max-width: 400px; margin: 100px auto; padding: 20px; text-align: center; border: 1px solid #ddd; border-radius: 8px; }
input[type="password"] { width: 100%; padding: 10px; margin-bottom: 1em; border: 1px solid #ccc; border-radius: 4px; }
button { padding: 10px 20px; border: 2px solid #333; border-radius: 4px; background: #fff; color: #333; cursor: pointer; }
button:hover { background: #333; color: #fff; }
</style>
</head><body>
<div class="container">
  <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width:200px; margin-bottom:20px;">
  <h2>Enter password to access Roll {{ sticker }}</h2>
  <form method="POST">
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">Submit</button>
  </form>
</div>
</body></html>""", sticker=sticker)

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

    show_whole_roll_buttons = record['fields'].get("Size") == "35mm"

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Select Prints – Roll {{ sticker }}</title>
  <style>
    body {
      font-family: Helvetica, sans-serif;
      background-color: #ffffff;
      color: #333;
      margin: 0;
      padding: 0;
    }
    .container {
      max-width: 1280px;
      margin: 0 auto;
      padding: 40px 20px;
      text-align: center;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 12px;
    }
    .grid-item {
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 8px;
    }
    .grid-item img {
      height: 150px;
      width: auto;
      display: block;
      margin: 0 auto 8px auto;
      object-fit: contain;
    }
    button {
      margin: 10px;
      padding: 10px 18px;
      font-size: 0.95em;
      border: 2px solid #333;
      background: #fff;
      color: #333;
      cursor: pointer;
      border-radius: 4px;
    }
    button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    button:hover:enabled {
      background: #333;
      color: #fff;
    }
    .note {
      font-size: 0.95em;
      margin-top: 10px;
      color: #666;
    }
    .download {
      display: inline-block;
      margin-bottom: 20px;
      padding: 10px 16px;
      border: 2px solid #333;
      border-radius: 4px;
      text-decoration: none;
      color: #333;
    }
    .download:hover {
      background-color: #333;
      color: #fff;
    }
  </style>
  <script>
    function submitWholeRoll(paperType) {
      if (!confirm(`This will print the entire roll on 10x15 ${paperType} paper. Each print normally costs €0.75. As you've selected 20 or more prints, the total is capped at €15. Continue?`)) {
        return;
      }
      const form = document.createElement('form');
      form.method = 'POST';
      form.action = `/roll/{{ sticker }}/submit-order`;

      document.querySelectorAll('input[name="selected_images"]').forEach((checkbox, index) => {
        const url = checkbox.value;
        form.innerHTML += `
          <input type="hidden" name="order[${index}][url]" value="${url}">
          <input type="hidden" name="order[${index}][size]" value="10x15">
          <input type="hidden" name="order[${index}][paper]" value="${paperType}">
          <input type="hidden" name="order[${index}][border]" value="No">
        `;
      });

      document.body.appendChild(form);
      form.submit();
    }

    function updateSubmitState() {
      const checked = document.querySelectorAll('input[name="selected_images"]:checked').length;
      document.getElementById('nextButton').disabled = checked === 0;
    }

    document.addEventListener('DOMContentLoaded', () => {
      document.querySelectorAll('input[name="selected_images"]').forEach(input => {
        input.addEventListener('change', updateSubmitState);
      });
      updateSubmitState();
    });
  </script>
</head>
<body>
  <div class="container">
    <div>
      <img src="https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png" alt="Logo" style="max-width: 200px; margin-bottom: 20px;">
    </div>
    <a class="download" href="/roll/{{ sticker }}">← Back to Gallery</a>
    <h1>Select Photos for Print – Roll {{ sticker }}</h1>
    {% if show_whole_roll_buttons %}
      <button onclick="submitWholeRoll('Matte')">Print Whole Roll on 10x15 Matte</button>
      <button onclick="submitWholeRoll('Glossy')">Print Whole Roll on 10x15 Glossy</button>
      <button onclick="submitWholeRoll('Luster')">Print Whole Roll on 10x15 Luster</button>
    {% endif %}
    <button onclick="document.querySelector('form').submit();">Order Selected Prints</button>
    <p class="note">Or select specific pictures to print below</p>

    <form method="POST" action="/roll/{{ sticker }}/submit-order">
      <div class="grid">
        {% for url in image_urls %}
          <div class="grid-item">
            <img src="{{ url }}" alt="Scan {{ loop.index }}">
            <input type="checkbox" name="selected_images" value="{{ url }}">
          </div>
        {% endfor %}
      </div>
      <button id="nextButton" type="submit">Order Selected Prints</button>
    </form>
  </div>
</body>
</html>
""", sticker=sticker, image_urls=image_urls, show_whole_roll_buttons=show_whole_roll_buttons)

@app.route('/roll/<sticker>/submit-order', methods=['POST'])
def submit_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    submitted_order = []
    if 'order[0][url]' in request.form:
        # Structured order form (whole roll or detailed post)
        for key in request.form:
            if key.startswith('order[') and key.endswith('][url]'):
                index = key.split('[')[1].split(']')[0]
                submitted_order.append({
                    'url': request.form.get(f'order[{index}][url]'),
                    'size': request.form.get(f'order[{index}][size]', '10x15'),
                    'paper': request.form.get(f'order[{index}][paper]', 'Glossy'),
                    'border': request.form.get(f'order[{index}][border]', 'No')
                })
    else:
        # Fallback: only selected_images
        urls = request.form.getlist("selected_images")
        for url in urls:
            submitted_order.append({
                'url': url,
                'size': '10x15',
                'paper': 'Glossy',
                'border': 'No'
            })

    if not submitted_order:
        return "No images selected.", 400

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Confirm Order – Roll {{ sticker }}</title>
      <style>
        body { font-family: Helvetica, sans-serif; background: #fff; color: #333; margin: 0; padding: 0; }
        .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; text-align: center; }
        h1 { margin-bottom: 1em; }
        .controls { margin-bottom: 30px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
        .grid-item { border: 1px solid #ccc; border-radius: 6px; padding: 12px; text-align: center; }
        .grid-item img { max-height: 180px; width: auto; margin-bottom: 10px; }
        select, input[type="text"] { width: 100%; padding: 6px; margin-top: 6px; margin-bottom: 10px; }
        button { margin-top: 20px; padding: 10px 20px; font-size: 1em; border: 2px solid #333; border-radius: 4px; background: #fff; cursor: pointer; }
        button:hover { background: #333; color: #fff; }
      </style>
      <script>
        function applyToAll() {
          const size = document.getElementById('applySize').value;
          const paper = document.getElementById('applyPaper').value;
          const border = document.getElementById('applyBorder').value;

          document.querySelectorAll('[data-row]').forEach(row => {
            row.querySelector('.size').value = size;
            row.querySelector('.paper').value = paper;
            row.querySelector('.border').value = border;
          });
        }
      </script>
    </head>
    <body>
      <div class="container">
        <h1>Confirm Your Print Order – Roll {{ sticker }}</h1>
        <div class="controls">
          <label>Size: <select id="applySize">
            <option>10x15</option>
            <option>A6</option>
            <option>A5</option>
            <option>A4</option>
            <option>A3</option>
          </select></label>
          <label>Paper: <select id="applyPaper">
            <option>Glossy</option>
            <option>Matte</option>
            <option>Luster</option>
          </select></label>
          <label>Include Scan Border: <select id="applyBorder">
            <option>No</option>
            <option>Yes</option>
          </select></label>
          <button onclick="applyToAll()">Apply to All</button>
        </div>
        <form method="POST" action="/roll/{{ sticker }}/finalize-order">
          <div class="grid">
            {% for item in submitted_order %}
              <div class="grid-item" data-row>
                <img src="{{ item.url }}">
                <input type="hidden" name="order[{{ loop.index0 }}][url]" value="{{ item.url }}">
                <label>Size:
                  <select name="order[{{ loop.index0 }}][size]" class="size">
                    <option {% if item.size == '10x15' %}selected{% endif %}>10x15</option>
                    <option {% if item.size == 'A6' %}selected{% endif %}>A6</option>
                    <option {% if item.size == 'A5' %}selected{% endif %}>A5</option>
                    <option {% if item.size == 'A4' %}selected{% endif %}>A4</option>
                    <option {% if item.size == 'A3' %}selected{% endif %}>A3</option>
                  </select>
                </label>
                <label>Paper:
                  <select name="order[{{ loop.index0 }}][paper]" class="paper">
                    <option {% if item.paper == 'Glossy' %}selected{% endif %}>Glossy</option>
                    <option {% if item.paper == 'Matte' %}selected{% endif %}>Matte</option>
                    <option {% if item.paper == 'Luster' %}selected{% endif %}>Luster</option>
                  </select>
                </label>
                <label>Border:
                  <select name="order[{{ loop.index0 }}][border]" class="border">
                    <option {% if item.border == 'No' %}selected{% endif %}>No</option>
                    <option {% if item.border == 'Yes' %}selected{% endif %}>Yes</option>
                  </select>
                </label>
              </div>
            {% endfor %}
          </div>
          <button type="submit">Next</button>
        </form>
      </div>
    </body>
    </html>
    """, sticker=sticker, submitted_order=submitted_order)

@app.route('/roll/<sticker>/finalize-order', methods=['POST'])
def finalize_order(sticker):
    import mollie
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
    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            url = request.form.get(f'order[{index}][url]')
            size = request.form.get(f'order[{index}][size]', '10x15')
            paper = request.form.get(f'order[{index}][paper]', 'Glossy')
            border = request.form.get(f'order[{index}][border]', 'No')

            submitted_order.append({
                'url': url,
                'size': size,
                'paper': paper,
                'border': border
            })

            if size == '10x15':
                count_10x15 += 1
                total += 0.75
            elif size == 'A6':
                total += 1.5
            elif size == 'A5':
                total += 3.0
            elif size == 'A4':
                total += 6.0
            elif size == 'A3':
                total += 12.0

    capped_total = min(total, 15.0) if count_10x15 >= 20 else total

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

        client_email = record['fields'].get("Client Email")
        create_print_order_record(sticker, client_email, submitted_order, payment.id)

        return redirect(payment.checkout_url)

    except Exception as e:
        return f"Payment creation failed: {e}", 500

@app.route('/roll/<sticker>/thank-you')
def thank_you(sticker):
    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Thank You – Roll {sticker}</title>
        <style>
            body {{
                font-family: Helvetica, sans-serif;
                background-color: #f9f9f9;
                color: #333;
                text-align: center;
                padding: 100px 20px;
            }}
            h1 {{
                font-size: 2em;
                margin-bottom: 20px;
            }}
            p {{
                font-size: 1.2em;
            }}
        </style>
    </head>
    <body>
        <h1>Thank you for your order!</h1>
        <p>Your payment was successful and your print order for roll <strong>{sticker}</strong> has been received.</p>
        <p>You’ll receive a confirmation email shortly.</p>
    </body>
    </html>
    """)

@app.route('/mollie-webhook', methods=['POST'])
def mollie_webhook():
    mollie_api_key = os.getenv("MOLLIE_API_KEY")
    if not mollie_api_key:
        return "API key missing", 500

    mollie_client = __import__('mollie').api.client.Client()
    mollie_client.set_api_key(mollie_api_key)

    payment_id = request.form.get("id")
    if not payment_id:
        return "Missing payment ID", 400

    try:
        payment = mollie_client.payments.get(payment_id)
        if not payment.is_paid():
            return "Payment not completed", 200

        # Look up Print Order record from Airtable by Mollie ID
        airtable_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Print%20Orders"
        headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
        formula = f"{{Mollie ID}}='{payment_id}'"
        response = requests.get(airtable_url, headers=headers, params={"filterByFormula": formula})
        records = response.json().get("records", [])
        if not records:
            return "Print order not found", 404

        order_record = records[0]
        fields = order_record["fields"]
        sticker = fields.get("Sticker")
        client_email = fields.get("Client Email")
        client_name = fields.get("Client Name", "Client")
        submitted_order = json.loads(fields.get("Order JSON", "[]"))

        # Mark as Paid
        update_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Print%20Orders/{order_record['id']}"
        update_response = requests.patch(update_url, headers=headers, json={"fields": {"Paid": True}})

        # Send confirmation email
        email_body = f"<p>Hi {client_name},</p><p>Thank you for your print order. Here’s a summary of what you selected for roll <strong>{sticker}</strong>:</p><ul>"
        for item in submitted_order:
            email_body += f"<li><img src='{item['url']}' width='100'><br>Size: {item['size']}, Paper: {item['paper']}, Include Scan Border: {item['border']}</li>"
        email_body += "</ul><p>We’ll start printing soon!</p>"

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

        return "OK", 200

    except Exception as e:
        return f"Webhook error: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
