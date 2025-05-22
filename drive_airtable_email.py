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
def send_email(to_address, subject, body):
    msg = EmailMessage()
    msg["From"] = "Gil Plaquet FilmLab <filmlab@gilplaquet.com>"
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log("✅ Email sent successfully.")
    except Exception as e:
        log(f"❌ Email failed: {e}")

# === Folder Utilities ===
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
        image_urls = [generate_signed_url(f) for f in image_files]
        zip_url = generate_signed_url(f"{prefix}Archive.zip")

        return render_template_string("""
        <!DOCTYPE html>
        <html lang='en'>
        <head>
          <meta charset='UTF-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1.0'>
          <title>Roll {{ sticker }} – Gil Plaquet FilmLab</title>
          <style>
            body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #fff; color: #333; margin: 0; padding: 0; }
            .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; text-align: center; }
            .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-top: 30px; }
            .gallery img { width: 100%; height: auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            .download, .print-order { display: inline-block; margin-bottom: 20px; padding: 12px 24px; border: 2px solid #333; border-radius: 4px; text-decoration: none; color: #333; font-weight: bold; margin-right: 10px; transition: background 0.3s, color 0.3s; }
            .download:hover, .print-order:hover { background-color: #333; color: #fff; }
            footer { margin-top: 60px; font-size: 0.9em; color: #888; }
          </style>
        </head>
        <body>
          <div class='container'>
            <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' style='max-width:200px;margin-bottom:20px;'>
            <a class='download' href='{{ zip_url }}'>Download All (ZIP)</a>
            <a class='print-order' href='/print-order/{{ sticker }}'>Order Prints</a>
            <h1>Roll {{ sticker }}</h1>
            <div class='gallery'>
              {% for url in image_urls %}<img src='{{ url }}' alt='Scan {{ loop.index }}'>{% endfor %}
            </div>
            <footer>&copy; {{ current_year }} Gil Plaquet</footer>
          </div>
        </body>
        </html>
        """, sticker=sticker, image_urls=image_urls, zip_url=zip_url, current_year=datetime.now().year)

    return render_template_string("""
    <!DOCTYPE html>
    <html lang='en'>
    <head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>Enter Password – Roll {{ sticker }}</title>
    <style>body { font-family: Helvetica Neue, sans-serif; background: #fff; color: #333; margin: 0; padding: 0; }
    .container { max-width: 400px; margin: 100px auto; padding: 20px; border: 1px solid #ddd; border-radius: 8px; text-align: center; }
    img { max-width: 200px; margin-bottom: 20px; }
    h2 { font-size: 1.5em; margin-bottom: 1em; }
    input[type='password'] { width: 100%; padding: 10px; font-size: 1em; margin-bottom: 1em; border: 1px solid #ccc; border-radius: 4px; }
    button { padding: 10px 20px; font-size: 1em; border: 2px solid #333; border-radius: 4px; background: #fff; color: #333; cursor: pointer; transition: all 0.3s; }
    button:hover { background: #333; color: #fff; }
    </style>
    </head>
    <body><div class='container'>
    <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo'>
    <h2>Enter password to access Roll {{ sticker }}</h2>
    <form method='POST'>
      <input type='password' name='password' placeholder='Password' required>
      <button type='submit'>Submit</button>
    </form></div></body></html>
    """, sticker=sticker)


@app.route('/print-order/<sticker>', methods=['GET', 'POST'])
def print_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

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
    s3 = boto3.client('s3', aws_access_key_id=S3_ACCESS_KEY_ID, aws_secret_access_key=S3_SECRET_ACCESS_KEY, endpoint_url=S3_ENDPOINT_URL, config=Config(signature_version='s3v4'))
    result = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
    image_files = [obj["Key"] for obj in result.get("Contents", []) if obj["Key"].lower().endswith(('.jpg', '.jpeg', '.png'))]
    image_urls = [(f, generate_signed_url(f)) for f in image_files]

    if request.method == 'POST':
        total_price = 0
        selections = []

        for filename, url in image_urls:
            if request.form.get(f"select_{filename}"):
                size = request.form.get(f"size_{filename}")
                paper = request.form.get(f"paper_{filename}")
                border = request.form.get(f"border_{filename}")

                if size == "10x15" and paper == "Budget Semigloss":
                    price = 0.5
                elif size in ["10x15", "A6"]:
                    price = 1.5
                elif size == "A5":
                    price = 3
                elif size == "A4":
                    price = 6
                elif size == "A3":
                    price = 12
                else:
                    price = 0

                total_price += price
                selections.append({
                    "filename": filename,
                    "size": size,
                    "paper": paper,
                    "border": border,
                    "price": price
                })

        client_name = record['fields'].get('Client Name', 'Unknown')
        client_email = record['fields'].get('Client Email', 'Unknown')

        body = f"Client: {client_name}
Email: {client_email}
Roll Number: {sticker}

Order:
"
        for item in selections:
            body += f"- {item['filename']} | {item['size']} | {item['paper']} | Border: {item['border']} | €{item['price']}
"
        body += f"
Total: €{total_price}"

        send_email("filmlab@gilplaquet.com", f"Print Order for Roll {sticker}", body)

        return f"Order submitted successfully. Total: €{total_price}"

    return render_template_string("""...full HTML form omitted for brevity...""", sticker=sticker, image_urls=image_urls)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
