import os
import requests
import logging
from flask import Flask, request

# === Config ===
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")

# === Logger ===
logging.basicConfig(level=logging.INFO)
def log(message):
    print(message)
    logging.info(message)

# === App ===
app = Flask(__name__)

# === Clean Airtable helper ===
def get_airtable_records():
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    sticker = '123456'
    formula = f"{{Twin Sticker}}='{sticker}'"
    response = requests.get(url, headers=headers, params={"filterByFormula": formula})
    records = response.json().get("records", [])
    if not records:
        log(f"❌ No matching roll found for sticker {sticker}")
        return
    return records


# === Backblaze S3 Client Setup ===
import boto3
from botocore.client import Config

B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APP_KEY = os.getenv("B2_APP_KEY")
B2_BUCKET = os.getenv("B2_BUCKET")
B2_REGION = os.getenv("B2_REGION", "eu-central-003")
B2_ENDPOINT = f"https://s3.{B2_REGION}.backblazeb2.com"

s3_client = boto3.client(
    's3',
    endpoint_url=B2_ENDPOINT,
    aws_access_key_id=B2_KEY_ID,
    aws_secret_access_key=B2_APP_KEY,
    config=Config(signature_version='s3v4')
)

# === Generate Signed S3 URL ===
def generate_s3_signed_url(key, expires=3600):
    return s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': B2_BUCKET, 'Key': key},
        ExpiresIn=expires
    )

from flask import render_template_string, session, redirect, url_for
from urllib.parse import quote
import datetime
from pathlib import Path

# === Gallery Template ===
GALLERY_TEMPLATE = '''
<html><head><title>Scan Gallery</title></head><body>
<h2>Roll {{ sticker }}</h2>
{% for url in image_urls %}<img src='{{ url }}' width='300'><br>{% endfor %}
</body></html>
'''

# === Roll Route ===
@app.route('/roll/<sticker>', methods=['GET', 'POST'])
def roll_gallery(sticker):
    session.permanent = True
    from flask import flash
    record = get_airtable_record(sticker)
    if not record:
        return "Roll not found", 404
    expected_pw = record['fields'].get("Password")
    if request.method == 'POST':
        if request.form.get("password") != expected_pw:
            flash("Incorrect password.")
            return redirect(request.url)
        session[sticker] = True
        return redirect(url_for("roll_gallery", sticker=sticker))
    if not session.get(sticker):
        return '''<form method='post'><input name='password' type='password'><input type='submit'></form>'''
    folder = record['fields'].get("Folder")
    if not folder:
        return "No folder linked."
    prefix = f"{folder}/JPEG 4 BASE/"
    response = s3_client.list_objects_v2(Bucket=B2_BUCKET, Prefix=prefix)
    objects = response.get("Contents", [])
    image_urls = [generate_s3_signed_url(obj['Key']) for obj in objects if obj['Key'].lower().endswith('.jpg')]
    return render_template_string(GALLERY_TEMPLATE, image_urls=image_urls, sticker=sticker)

# === Print Order Template ===
ORDER_TEMPLATE = '''
<html><head><title>Order Prints</title></head><body>
<h2>Print Order for Roll {{ sticker }}</h2>
<form method='post' action='/roll/{{ sticker }}/submit-order'>
{% for url in image_urls %}
<div style='display:inline-block; text-align:center;'>
  <img src='{{ url }}' width='150'><br>
  <input type='checkbox' name='selected[{{ loop.index0 }}]' value='1'> Select<br>
  <input type='hidden' name='order[{{ loop.index0 }}][url]' value='{{ url }}'>
  Size: <select name='order[{{ loop.index0 }}][size]'>
    <option value='10x15'>10x15</option>
    <option value='A5'>A5</option>
    <option value='A4'>A4</option>
    <option value='A3'>A3</option>
  </select><br>
  Paper: <select name='order[{{ loop.index0 }}][paper]'>
    <option value='Matte'>Matte</option>
    <option value='Luster Semigloss'>Luster Semigloss</option>
  </select><br>
  Border: <select name='order[{{ loop.index0 }}][border]'>
    <option value='no'>No</option>
    <option value='yes'>Yes</option>
  </select>
</div>
{% endfor %}
<br><input type='submit' value='Next'>
</form>
</body></html>
'''

# === Print Order Route ===
@app.route('/roll/<sticker>/order', methods=['GET'])
def order_page(sticker):
    if not session.get(sticker):
        return redirect(url_for("roll_gallery", sticker=sticker))
    record = get_airtable_record(sticker)
    if not record:
        return "Roll not found", 404
    folder = record['fields'].get("Folder")
    if not folder:
        return "No folder set."
    prefix = f"{folder}/JPEG 4 BASE/"
    response = s3_client.list_objects_v2(Bucket=B2_BUCKET, Prefix=prefix)
    objects = response.get("Contents", [])
    image_urls = [generate_s3_signed_url(obj['Key']) for obj in objects if obj['Key'].lower().endswith('.jpg')]
    return render_template_string(ORDER_TEMPLATE, image_urls=image_urls, sticker=sticker)

# === Submit Order ===
@app.route('/roll/<sticker>/submit-order', methods=['POST'])
def submit_order(sticker):
    if not session.get(sticker):
        return redirect(url_for("roll_gallery", sticker=sticker))
    submitted = []
    total = 0.0
    count_10x15 = 0
    for key in request.form:
        if key.startswith('order[') and key.endswith('][url]'):
            index = key.split('[')[1].split(']')[0]
            if not request.form.get(f'selected[{index}]'):
                continue
            url = request.form.get(f'order[{index}][url]')
            size = request.form.get(f'order[{index}][size]', '10x15')
            paper = request.form.get(f'order[{index}][paper]', 'Matte')
            border = request.form.get(f'order[{index}][border]', 'no')
            price = 0
            if size == '10x15':
                price = 0.75
                count_10x15 += 1
            elif size == 'A5':
                price = 3
            elif size == 'A4':
                price = 6
            elif size == 'A3':
                price = 12
            submitted.append({
                'url': url, 'size': size, 'paper': paper, 'border': border, 'price': price
            })
            total += price
    if count_10x15 >= 20:
        cap = 15.0
        capped_total = sum(item['price'] for item in submitted if item['size'] != '10x15') + min(15.0, sum(item['price'] for item in submitted if item['size'] == '10x15'))
        total = capped_total
    session['submitted_order'] = submitted
    session['total'] = round(total, 2)
    return redirect(url_for('review_order', sticker=sticker))

# === Review Order Template ===
REVIEW_TEMPLATE = '''
<html><head><title>Review Order</title></head><body>
<h2>Review Order for Roll {{ sticker }}</h2>
<form method='post' action='/roll/{{ sticker }}/finalize-order'>
{% for item in submitted_order %}
<div style='display:inline-block; text-align:center;'>
  <img src='{{ item.url }}' width='150'><br>
  Size: {{ item.size }}<br>
  Paper: {{ item.paper }}<br>
  Border: {{ item.border }}<br>
  € {{ item.price }}
</div>
{% endfor %}
<h3>Total: € {{ total }}</h3>
<input type='submit' value='Pay with Mollie'>
</form>
</body></html>
'''

# === Review Order Route ===
@app.route('/roll/<sticker>/review-order', methods=['GET'])
def review_order(sticker):
    if not session.get(sticker):
        return redirect(url_for("roll_gallery", sticker=sticker))
    submitted_order = session.get('submitted_order', [])
    total = session.get('total', 0.0)
    return render_template_string(REVIEW_TEMPLATE, submitted_order=submitted_order, total=total, sticker=sticker)

# === Mollie Setup ===
from mollie.api.client import Client as MollieClient
MOLLIE_API_KEY = os.getenv("MOLLIE_API_KEY")
mollie_client = MollieClient()
mollie_client.set_api_key(MOLLIE_API_KEY)

# === Finalize Order: Create Mollie Payment ===
@app.route('/roll/<sticker>/finalize-order', methods=['POST'])
def finalize_order(sticker):
    submitted_order = session.get('submitted_order', [])
    total = session.get('total', 0.0)
    if not submitted_order or not total:
        return "Nothing to submit.", 400
    payment = mollie_client.payments.create({
        'amount': {
            'currency': 'EUR',
            'value': f"{total:.2f}"
        },
        'description': f"Print order for roll {sticker}",
        'redirectUrl': url_for('thank_you', sticker=sticker, _external=True),
        'webhookUrl': url_for('mollie_webhook', _external=True),
        'metadata': {
            'sticker': sticker
        }
    })
    return redirect(payment.get('checkout_url'))

# === Mollie Webhook ===
@app.route('/mollie-webhook', methods=['POST'])
def mollie_webhook():
    payment_id = request.form.get('id')
    payment = mollie_client.payments.get(payment_id)
    sticker = payment.metadata.get('sticker')
    if payment.is_paid():
        log(f"✅ Payment received for roll {sticker}")
        # Send confirmation or update Airtable here if needed
    return "OK"

# === Thank You Page ===
@app.route('/roll/<sticker>/thank-you', methods=['GET'])
def thank_you(sticker):
    return f"<html><body><h2>Thank you for your payment for roll {sticker}!</h2></body></html>"

# === Email Setup ===
import smtplib
from email.message import EmailMessage
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_BCC = "filmlab@gilplaquet.com"

email_client = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
email_client.starttls()
email_client.login(SMTP_USER, SMTP_PASS)

# === Send Confirmation Email ===
def send_order_email(sticker, order):
    msg = EmailMessage()
    msg['Subject'] = f"Order received for roll {sticker}"
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_FROM
    msg['Bcc'] = EMAIL_BCC
    html = f"<h2>Print Order Summary – Roll {sticker}</h2>"
    for item in order:
        html += f"<div><img src='{item['url']}' width='150'><br>Size: {item['size']} – Paper: {item['paper']} – Border: {item['border']} – €{item['price']}</div><br>"
    html += f"<br><strong>Total: €{session.get('total', 0.0):.2f}</strong>"
    msg.set_content("Your order has been received.")
    msg.add_alternative(html, subtype='html')
    email_client.send_message(msg)

# === Call email after webhook payment ===
# (replace comment in webhook)
    if payment.is_paid():
        log(f"✅ Payment received for roll {sticker}")
        send_order_email(sticker, session.get('submitted_order', []))

# === Password Expiry ===
# Store timestamp when password is used
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=7)

@app.before_request
def make_session_permanent():
    session.permanent = True
@app.route('/')
def index():
    return 'App is running'

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
