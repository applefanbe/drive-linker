import os
import smtplib
import requests
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, request, render_template_string, redirect
import boto3
from botocore.client import Config

app = Flask(__name__)

# === Configuration ===
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME")

# === Utilities ===
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

def find_airtable_record(sticker):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    formula = f"{{Twin Sticker}}='{sticker}'"
    response = requests.get(url, headers=headers, params={"filterByFormula": formula})
    if response.status_code != 200:
        return None
    records = response.json().get("records", [])
    return records[0] if records else None

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
    except Exception as e:
        print(f"❌ Email failed: {e}")

@app.route('/print-order/<sticker>', methods=['GET', 'POST'])
def print_order_select(sticker):
    record = find_airtable_record(sticker)
    if not record:
        return "Roll not found.", 404

    folder = None
    for name in list_roll_folders():
        if name.endswith(sticker.zfill(6)):
            folder = name
            break
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
    image_urls = [generate_signed_url(key) for key in image_files]

    if request.method == 'POST':
        selected_photos = request.form.getlist('photos')
        if not selected_photos:
            return "Please select at least one photo."
        return print_order_details(sticker, selected_photos)

    return render_template_string("""
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
      <meta charset=\"UTF-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
      <title>Print Order – Roll {{ sticker }}</title>
      <style>
        body { font-family: Helvetica, Arial, sans-serif; background: #fff; color: #333; margin: 0; padding: 0; }
        .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; text-align: center; }
        h1 { font-size: 2em; margin-bottom: 0.5em; }
        .gallery { display: grid; grid-template-columns: repeat(auto-fill, 120px); gap: 10px; margin-top: 20px; justify-content: center; }
        .gallery label { position: relative; display: block; width: 120px; height: 120px; border: 1px solid #ccc; border-radius: 4px; overflow: hidden; }
        .gallery img { width: 100%; height: 100%; object-fit: contain; background: #f9f9f9; }
        .gallery input[type=checkbox] { position: absolute; top: 5px; left: 5px; transform: scale(1.2); }
        button { padding: 12px 24px; border: 2px solid #333; border-radius: 4px; background: #fff; color: #333; font-weight: bold; cursor: pointer; margin-top: 20px; }
        button:hover { background: #333; color: #fff; }
      </style>
    </head>
    <body>
      <div class=\"container\">
        <img src=\"https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png\" alt=\"Logo\" style=\"max-width: 200px; margin-bottom: 20px;\" />
        <h1>Roll {{ sticker }}</h1>
        <p>Select photos to print, then click <strong>“Set Print Size and Paper”</strong>.</p>
        <form id=\"selection-form\" method=\"POST\">
          <div class=\"gallery\">
            {% for url, key in image_list %}
              <label>
                <input type=\"checkbox\" name=\"photos\" value=\"{{ key|e }}\" />
                <img src=\"{{ url }}\" alt=\"Photo {{ loop.index }}\" />
              </label>
            {% endfor %}
          </div>
          <button type=\"submit\">Set Print Size and Paper</button>
        </form>
      </div>
      <script>
        document.getElementById('selection-form').addEventListener('submit', function(e) {
          if (!document.querySelector('input[name="photos"]:checked')) {
            e.preventDefault();
            alert("Please select at least one photo to proceed.");
          }
        });
      </script>
    </body>
    </html>
    """, sticker=sticker, image_list=list(zip(image_urls, image_files)))

@app.route('/print-order/<sticker>/details', methods=['GET', 'POST'])
def print_order_details(sticker, selected_photos=None):
    if request.method == 'GET':
        return redirect(f"/print-order/{sticker}")

    if selected_photos is None:
        selected_photos = request.form.getlist('photos')
    if not selected_photos:
        return redirect(f"/print-order/{sticker}")

    image_urls = [generate_signed_url(key) for key in selected_photos]
    size_options = ["10x15", "13x18", "20x30"]
    paper_options = ["Budget", "Premium"]
    default_price = 0.5
    total_price = len(selected_photos) * default_price

    return render_template_string("""
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
      <meta charset=\"UTF-8\">
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
      <title>Print Order Details – Roll {{ sticker }}</title>
      <style>
        body { font-family: Helvetica, Arial, sans-serif; background: #fff; color: #333; margin: 0; padding: 0; }
        .container { max-width: 960px; margin: 0 auto; padding: 40px 20px; }
        h2 { text-align: center; font-size: 1.8em; margin-bottom: 20px; }
        .table-container { width: 100%; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; min-width: 600px; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #f0f0f0; }
        td img { max-width: 80px; border-radius: 4px; }
        select { padding: 4px; border: 1px solid #ccc; border-radius: 4px; }
        button { padding: 10px 20px; border: 2px solid #333; border-radius: 4px; background: #fff; color: #333; font-weight: bold; cursor: pointer; }
        button:hover { background: #333; color: #fff; }
        .apply-all { text-align: right; margin: 15px 0; }
        .total { text-align: right; font-size: 1.1em; margin-top: 10px; font-weight: bold; }
      </style>
    </head>
    <body>
      <div class=\"container\">
        <h2>Set Print Options for Selected Photos (Roll {{ sticker }})</h2>
        <div class=\"table-container\">
          <table id=\"order-table\">
            <thead>
              <tr><th>Photo</th><th>Print Size</th><th>Paper Type</th><th>Price Each</th></tr>
            </thead>
            <tbody>
              {% for url in image_urls %}
              <tr>
                <td><img src=\"{{ url }}\" alt=\"Photo {{ loop.index }}\" /></td>
                <td>
                  <select name=\"size\" class=\"size-select\">
                    {% for size in size_options %}
                    <option value=\"{{ size }}\" {% if size == '10x15' %}selected{% endif %}>{{ size }}</option>
                    {% endfor %}
                  </select>
                </td>
                <td>
                  <select name=\"paper\" class=\"paper-select\">
                    {% for paper in paper_options %}
                    <option value=\"{{ paper }}\" {% if paper == 'Budget' %}selected{% endif %}>{{ paper }}</option>
                    {% endfor %}
                  </select>
                </td>
                <td class=\"price\">€0.50</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        <div class=\"apply-all\">
          Apply to all:
          <select id=\"global-size\">
            {% for size in size_options %}<option value=\"{{ size }}\">{{ size }}</option>{% endfor %}
          </select>
          <select id=\"global-paper\">
            {% for paper in paper_options %}<option value=\"{{ paper }}\">{{ paper }}</option>{% endfor %}
          </select>
          <button type=\"button\" id=\"apply-all-btn\">Apply</button>
        </div>
        <div class=\"total\">Total: <span id=\"total-price\">€{{ '%.2f' % total_price }}</span></div>
      </div>
      <script>
        function updatePrices() {
          let total = 0;
          document.querySelectorAll('#order-table tbody tr').forEach(function(row) {
            const size = row.querySelector('.size-select').value;
            const paper = row.querySelector('.paper-select').value;
            const price = (size === '10x15' && paper === 'Budget') ? 0.5 : 1.5;
            row.querySelector('.price').textContent = '€' + price.toFixed(2);
            total += price;
          });
          document.getElementById('total-price').textContent = '€' + total.toFixed(2);
        }
        document.querySelectorAll('.size-select, .paper-select').forEach(function(select) {
          select.addEventListener('change', updatePrices);
        });
        document.getElementById('apply-all-btn').addEventListener('click', function() {
          const sizeVal = document.getElementById('global-size').value;
          const paperVal = document.getElementById('global-paper').value;
          document.querySelectorAll('.size-select').forEach(sel => sel.value = sizeVal);
          document.querySelectorAll('.paper-select').forEach(sel => sel.value = paperVal);
          updatePrices();
        });
      </script>
    </body>
    </html>
    """, sticker=sticker, image_urls=image_urls, size_options=size_options, paper_options=paper_options, total_price=total_price)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
