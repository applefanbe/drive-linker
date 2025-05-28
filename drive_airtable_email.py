
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
from flask import flash, Flask, make_response, request, render_template_string, session, redirect, url_for
import boto3
from botocore.client import Config
from urllib.parse import quote
from mollie.api.client import Client as MollieClient

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
MOLLIE_API_KEY = os.getenv("MOLLIE_API_KEY")

# === Initialize Clients ===
s3 = boto3.client(
    's3',
    aws_access_key_id=S3_ACCESS_KEY_ID,
    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    endpoint_url=S3_ENDPOINT_URL,
    config=Config(signature_version='s3v4')
)

smtp = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
smtp.starttls()
smtp.login(SMTP_USER, SMTP_PASS)

mollie_client = MollieClient()
mollie_client.set_api_key(MOLLIE_API_KEY)

# === Processed Cache ===
try:
    with open(STATE_FILE, "r") adef log(message):
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
        lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("password.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, sticker=sticker)

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

    lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("gallery.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, 
    sticker=sticker, 
    image_urls=image_urls, 
    zip_url=zip_url, 
    current_year=datetime.now().year,
    record=record  # ✅ this is required for roll-info to work
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
        lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("password_1.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, sticker=sticker)

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
    show_whole_roll_buttons = film_size == "35mm" and len(image_urls) >= 20
    show_select_all_button = film_size != "35mm"
    allow_border_option = "Hires" in scan_type

    lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("gallery_1.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, sticker=sticker, image_urls=image_urls,
       show_whole_roll_buttons=show_whole_roll_buttons,
       show_select_all_button=show_select_all_button,
       allow_border_option=allow_border_option)

@app.route('/roll/<sticker>/submit-order', methods=['POST'])
def submit_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    scan_level = record['fields'].get('Scan', '')
    allow_border_option = 'Hires' in scan_level

    submitted_order = []
    if 'order[0][url]' in request.form:
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

    lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("order.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, sticker=sticker, submitted_order=submitted_order, allow_border_option=allow_border_option)

@app.route('/roll/<sticker>/review-order', methods=['POST'])
def review_order(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    scan_quality = record['fields'].get('Scan', '')
    allow_border = 'Hires' in scan_quality

    submitted_order = []
    type_counter = {}
    subtotal = 0.0
    tax_rate = 0.21
    count_10x15 = 0
    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            url = request.form.get(f'order[{index}][url]')
            size = request.form.get(f'order[{index}][size]', '10x15')
            paper = request.form.get(f'order[{index}][paper]', 'Glossy')
            border = request.form.get(f'order[{index}][border]', 'No')

            if size == '10x15':
                count_10x15 += 1
                price = 0.75
            elif size == 'A6':
                price = 1.5
            elif size == 'A5':
                price = 3.0
            elif size == 'A4':
                price = 6.0
            elif size == 'A3':
                price = 12.0
            else:
                price = 0.0

            key_type = f"{size} - {paper}"
            type_counter[key_type] = type_counter.get(key_type, 0) + 1
            submitted_order.append({'url': url, 'size': size, 'paper': paper, 'border': border, 'price': price})
            subtotal += price

    if count_10x15 >= 20:
        subtotal = min(subtotal, 15.0)

    tax = subtotal * tax_rate / (1 + tax_rate)
    total = subtotal

    lang = request.args.get("lang") or request.cookies.get("lang", "en")
    t = get_translation(lang)
    resp = make_response(render_template("order_1.html", lang=lang, t=t))
    resp.set_cookie("lang", lang)
    return resp, sticker=sticker, submitted_order=submitted_order, total=total, tax=tax, type_counter=type_counter, allow_border=allow_border)

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

    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
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
                max-width: 600px;
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
            img {{
                max-width: 200px;
                margin-bottom: 30px;
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
        </style>
    </head>
    <body>
        <div class="wrapper">
            <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo'>
            <h1>Thank you for your order!</h1>
            <p>Your payment was successful and your print order for roll <strong>{sticker}</strong> has been received.</p>
            <p>You’ll receive a confirmation email shortly at <strong>{email}</strong>.</p>
            <a class="button" href='/roll/{sticker}'>← Back to Gallery</a>
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
