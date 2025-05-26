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
    else:
        session[f"access_{sticker}"] = True

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

...

@app.route('/roll/<sticker>/order', methods=['GET', 'POST'])
def order_page(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    expected_password = record['fields'].get("Password")

    if session.get(f"access_{sticker}"):
        password_ok = True
    elif request.method == "POST" and request.form.get("password") == expected_password:
        session[f"access_{sticker}"] = True
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

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Select Prints – Roll {{ sticker }}</title>
      <style>
        body {
          font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
          background-color: #ffffff;
          color: #333333;
          margin: 0;
          padding: 0;
        }
        .container {
          max-width: 1280px;
          margin: 0 auto;
          padding: 40px 20px;
          text-align: center;
        }
        h1 {
          font-size: 2em;
          margin: 1em 0;
        }
        form {
          margin-top: 30px;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
          gap: 12px;
        }
        .grid-item {
          border: 1px solid #eee;
          border-radius: 6px;
          padding: 8px;
        }
        .grid-item img {
          width: 100%;
          height: auto;
          display: block;
          margin-bottom: 8px;
          border-radius: 4px;
        }
        button {
          margin-top: 30px;
          padding: 12px 24px;
          font-size: 1em;
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
      </style>
      <script>
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
        <h1>Select Photos for Print – Roll {{ sticker }}</h1>
        <form method="POST" action="/roll/{{ sticker }}/submit-order">
          <div class="grid">
            {% for url in image_urls %}
              <div class="grid-item">
                <img src="{{ url }}" alt="Scan {{ loop.index }}">
                <input type="checkbox" name="selected_images" value="{{ url }}">
              </div>
            {% endfor %}
          </div>
          <button id="nextButton" type="submit">Next</button>
        </form>
      </div>
    </body>
    </html>
    """, sticker=sticker, image_urls=image_urls)

@app.route('/roll/<sticker>/submit-order', methods=['POST'])
def submit_order(sticker):
    selected_images = request.form.getlist("selected_images")
    if not selected_images:
        return "No images selected.", 400

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <title>Print Options – Roll {{ sticker }}</title>
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
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 20px;
        }
        .card {
          border: 1px solid #ddd;
          border-radius: 8px;
          padding: 10px;
        }
        .card img {
          width: 100%;
          height: auto;
          border-radius: 4px;
          margin-bottom: 10px;
        }
        select, label {
          display: block;
          margin: 5px auto;
        }
        button {
          margin-top: 30px;
          padding: 12px 24px;
          font-size: 1em;
          border: 2px solid #333;
          background: #fff;
          color: #333;
          cursor: pointer;
          border-radius: 4px;
        }
        button:hover {
          background: #333;
          color: #fff;
        }
      </style>
      <script>
        function updatePrice() {
          const cards = document.querySelectorAll('.card');
          let total = 0;
          cards.forEach(card => {
            const size = card.querySelector('[name$="[size]"]').value;
            let price = 0;

            if (size === "10x15") price = 0.5;
            else if (size === "A6") price = 1.5;
            else if (size === "A5") price = 3;
            else if (size === "A4") price = 6;
            else if (size === "A3") price = 12;

            total += price;
          });
          document.getElementById('totalPrice').textContent = "Total: €" + total.toFixed(2);
        }

        function updatePaperOptions(selectElement) {
          const size = selectElement.value;
          const paperSelect = selectElement.closest('.card').querySelector('.paper-select');

          if (size === "10x15") {
            paperSelect.innerHTML = `
              <option value="Glossy">Glossy</option>
              <option value="Matte">Matte</option>
              <option value="Luster">Luster</option>
            `;
          } else {
            paperSelect.innerHTML = `
              <option value="Luster Semigloss">Luster Semigloss</option>
              <option value="Matte">Matte</option>
            `;
          }

          updatePrice();
        }

        document.addEventListener("DOMContentLoaded", () => {
          document.querySelectorAll('.card').forEach(card => {
            updatePaperOptions(card.querySelector('[name$="[size]"]'));
          });
          document.querySelectorAll('select').forEach(select => {
            select.addEventListener('change', updatePrice);
          });
          updatePrice();
        });
      </script>
    </head>
    <body>
      <div class="container">
        <h1>Choose Print Options</h1>
        <form method="POST" action="/roll/{{ sticker }}/finalize-order">
          <div class="grid">
            {% for url in selected_images %}
              <div class="card">
                <img src="{{ url }}" alt="Photo {{ loop.index }}">
                <input type="hidden" name="order[{{ loop.index0 }}][url]" value="{{ url }}">

                <label>Size:
                  <select name="order[{{ loop.index0 }}][size]" onchange="updatePaperOptions(this)">
                    <option value="10x15">10x15</option>
                    <option value="A6">A6</option>
                    <option value="A5">A5</option>
                    <option value="A4">A4</option>
                    <option value="A3">A3</option>
                  </select>
                </label>

                <label>Paper:
                  <select name="order[{{ loop.index0 }}][paper]" class="paper-select">
                    <option value="Glossy">Glossy</option>
                    <option value="Matte">Matte</option>
                    <option value="Luster">Luster</option>
                  </select>
                </label>

                <label>Print Scan Border:
                  <select name="order[{{ loop.index0 }}][border]">
                    <option value="No" selected>No</option>
                    <option value="Yes">Yes</option>
                  </select>
                </label>
              </div>
            {% endfor %}
          </div>

          <p id="totalPrice" style="margin-top: 20px; font-weight: bold;"></p>
          <button type="submit">Send Order</button>
        </form>
      </div>
    </body>
    </html>
    """, sticker=sticker, selected_images=selected_images)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
