import os
import smtplib
import requests
import base64
import glob
import json
from mimetypes import guess_type
from email.utils import make_msgid
from email.message import EmailMessage
from flask import Flask, request, redirect, url_for, render_template_string

app = Flask(__name__)

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
MOLLIE_REDIRECT_URL = os.getenv("MOLLIE_REDIRECT_URL", "http://localhost:5000/payment-success")

order_cache = {}

# === Utility: Load thumbnails ===
def get_thumbnails(sticker):
    folder = f"static/{sticker}"
    return sorted(glob.glob(os.path.join(folder, '*.jpg')))

# === Routes ===
@app.route('/roll/<sticker>')
def gallery(sticker):
    thumbnails = get_thumbnails(sticker)
    return render_template_string("""
    <html><head><title>Gallery {{sticker}}</title>
    <style>
    body { font-family: sans-serif; max-width: 1200px; margin: auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
    img { width: 100%; height: auto; display: block; }
    .top-bar { margin: 20px 0; text-align: right; }
    </style></head><body>
    <div class='top-bar'><a href='/order/{{sticker}}'><button>Order Prints</button></a></div>
    <div class='grid'>
    {% for img in thumbnails %}<img src='/{{img}}'>{% endfor %}
    </div></body></html>
    """, sticker=sticker, thumbnails=thumbnails)

@app.route('/order/<sticker>', methods=['GET', 'POST'])
def order_select(sticker):
    thumbnails = get_thumbnails(sticker)
    return render_template_string("""
    <html><head><title>Select Prints</title>
    <style>
    body { font-family: sans-serif; max-width: 1200px; margin: auto; }
    .grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
    .thumb { position: relative; }
    .thumb input { position: absolute; top: 10px; left: 10px; transform: scale(1.5); }
    img { width: 100%; height: auto; display: block; }
    .bar { margin: 20px 0; text-align: center; }
    </style></head><body>
    <div class='bar'><h2>Select the pictures you want to print</h2></div>
    <form method='POST' action='/order-details/{{sticker}}'>
    <div class='bar'><button type='submit'>Set print details</button></div>
    <div class='grid'>
    {% for img in thumbnails %}
    <div class='thumb'>
        <input type='checkbox' name='selected' value='{{img}}'>
        <img src='/{{img}}'>
    </div>
    {% endfor %}
    </div>
    <div class='bar'><button type='submit'>Set print details</button></div>
    </form></body></html>
    """, sticker=sticker, thumbnails=thumbnails)

@app.route('/order-details/<sticker>', methods=['POST'])
def order_details(sticker):
    selected = request.form.getlist('selected')
    order_cache[sticker] = {"selected": selected}
    return render_template_string("""
    <html><head><title>Set Print Details</title>
    <style>
    body { font-family: sans-serif; max-width: 1200px; margin: auto; }
    .grid { display: grid; grid-template-columns: repeat(1, 1fr); gap: 20px; }
    .entry { border: 1px solid #ccc; padding: 10px; }
    img { max-width: 150px; height: auto; display: block; }
    </style>
    <script>
    function updatePrices() {
      let total = 0;
      document.querySelectorAll('.price-per-line').forEach(function(p) {
        total += parseFloat(p.innerText);
      });
      document.getElementById('total').innerText = total.toFixed(2);
    }
    function recalcPrice(lineId) {
      const line = document.getElementById(lineId);
      const qty = parseInt(line.querySelector('[name="qty"]').value);
      let price = 1.5 * qty;
      line.querySelector('.price-per-line').innerText = price.toFixed(2);
      updatePrices();
    }
    </script></head><body onload="updatePrices()">
    <h2>Set print details</h2>
    <form method='POST' action='/checkout/{{sticker}}'>
    <div class='grid'>
    {% for img in selected %}
    <div class='entry' id='line-{{loop.index}}'>
        <img src='/{{img}}'><input type='hidden' name='image' value='{{img}}'>
        Size:<select name='size' onchange='recalcPrice("line-{{loop.index}}")'>
            <option value='10x15'>10x15</option>
            <option value='A4'>A4</option>
        </select>
        Paper:<select name='paper' onchange='recalcPrice("line-{{loop.index}}")'>
            <option value='Luster'>Luster</option>
            <option value='Matte'>Matte</option>
        </select>
        Print scan border: <input type='checkbox' name='border'>
        Quantity: <input type='number' name='qty' value='1' min='1' onchange='recalcPrice("line-{{loop.index}}")'>
        <p>Price: €<span class='price-per-line'>0.00</span></p>
    </div>
    {% endfor %}
    </div>
    <h3>Total: €<span id='total'>0.00</span></h3>
    <button type='submit'>Proceed to Payment</button>
    </form></body></html>
    """, sticker=sticker, selected=selected)

@app.route('/checkout/<sticker>', methods=['POST'])
def checkout(sticker):
    images = request.form.getlist("image")
    sizes = request.form.getlist("size")
    papers = request.form.getlist("paper")
    borders = request.form.getlist("border")
    quantities = request.form.getlist("qty")

    total_amount = 0
    lines = []
    for i in range(len(images)):
        qty = int(quantities[i])
        total_amount += 1.5 * qty
        lines.append({
            "image": images[i],
            "size": sizes[i],
            "paper": papers[i],
            "border": 'Yes' if i < len(borders) else 'No',
            "qty": qty
        })

    order_cache[sticker] = {"lines": lines, "total": total_amount}

    data = {
        "amount": {"currency": "EUR", "value": f"{total_amount:.2f}"},
        "description": f"Print order for roll {sticker}",
        "redirectUrl": url_for('payment_success', sticker=sticker, _external=True),
        "webhookUrl": "https://example.com/mollie-webhook"
    }

    headers = {
        "Authorization": f"Bearer {MOLLIE_API_KEY}",
        "Content-Type": "application/json"
    }

    response = requests.post("https://api.mollie.com/v2/payments", headers=headers, json=data)
    if response.status_code == 201:
        return redirect(response.json()["_links"]["checkout"]["href"])
    return f"Failed to create Mollie payment: {response.text}", 500

@app.route('/payment-success')
def payment_success():
    sticker = request.args.get('sticker')
    order = order_cache.get(sticker)
    if not order:
        return "No order found."

    msg = EmailMessage()
    msg['Subject'] = f"New print order for roll {sticker}"
    msg['From'] = SMTP_USER
    msg['To'] = 'filmlab@gilplaquet.com'

    body = [f"<h2>Print order for roll {sticker}</h2>", "<ul>"]
    for line in order["lines"]:
        cid = make_msgid(domain="gilplaquet.com")[1:-1]
        image_path = line["image"]
        maintype, subtype = (guess_type(image_path)[0] or 'image/jpeg').split('/')
        with open(image_path, 'rb') as img:
            msg.add_related(img.read(), maintype=maintype, subtype=subtype, cid=f"<{cid}>", filename=os.path.basename(image_path))
        body.append(f"<li><img src='cid:{cid}' width='150'><br><strong>{os.path.basename(image_path)}</strong><br>Size: {line['size']}, Paper: {line['paper']}, Border: {line['border']}, Qty: {line['qty']}</li>")
    body.append("</ul>")
    body.append(f"<p><strong>Total: €{order['total']:.2f}</strong></p>")

    msg.set_content("Print order for roll " + sticker)
    msg.add_alternative("".join(body), subtype='html')

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return "Payment successful! Confirmation email with thumbnails sent."
    except Exception as e:
        return f"Payment succeeded but failed to send email: {str(e)}"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
