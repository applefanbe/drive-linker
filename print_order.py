from flask import Blueprint, request, render_template_string, redirect, url_for
import os
import glob
import json
import smtplib
import requests
from mimetypes import guess_type
from email.utils import make_msgid
from email.message import EmailMessage

print_order_blueprint = Blueprint("print_order", __name__)

order_cache = {}

MOLLIE_API_KEY = os.getenv("MOLLIE_API_KEY")
MOLLIE_REDIRECT_URL = os.getenv("MOLLIE_REDIRECT_URL", "http://localhost:5000/order/payment-success")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

# === Utility ===
def get_thumbnails(sticker):
    folder = f"static/{sticker}"
    return sorted(glob.glob(os.path.join(folder, '*.jpg')))

@print_order_blueprint.route('/<sticker>', methods=['GET', 'POST'])
def order_select(sticker):
    thumbnails = get_thumbnails(sticker)
    return render_template_string(""" ... """, sticker=sticker, thumbnails=thumbnails)

@print_order_blueprint.route('/details/<sticker>', methods=['POST'])
def order_details(sticker):
    selected = request.form.getlist('selected')
    order_cache[sticker] = {"selected": selected}
    return render_template_string(""" ... """, sticker=sticker, selected=selected)

@print_order_blueprint.route('/checkout/<sticker>', methods=['POST'])
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
        "redirectUrl": url_for('print_order.payment_success', sticker=sticker, _external=True),
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

@print_order_blueprint.route('/payment-success')
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
