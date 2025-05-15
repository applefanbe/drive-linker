
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

        prefix = f"rolls/{folder}/THUMB/"
        s3 = boto3.client(
            's3',
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            endpoint_url=S3_ENDPOINT_URL,
            config=Config(signature_version='s3v4')
        )
        result = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
        image_files = [obj["Key"] for obj in result.get("Contents", []) if obj["Key"].lower().endswith(('.jpg', '.jpeg', '.png'))]
        thumb_urls = [f"https://cdn.gilplaquet.com/{file}" for file in image_files]
        zip_url = f"https://cdn.gilplaquet.com/rolls/{folder}/Archive.zip"

        from datetime import datetime
        return render_template_string("""
        <!DOCTYPE html>
        <html lang='en'>
        <head>
          <meta charset='UTF-8'>
          <meta name='viewport' content='width=device-width, initial-scale=1.0'>
          <title>Roll {{ sticker }} – Gil Plaquet FilmLab</title>
          <style>
            body {
              font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
              background-color: #fff;
              color: #333;
              margin: 0;
              padding: 0;
            }
            .container {
              max-width: 960px;
              margin: 0 auto;
              padding: 40px 20px;
              text-align: center;
            }
            .logo {
              max-width: 200px;
              height: auto;
              margin-bottom: 30px;
            }
            h1 {
              font-size: 2em;
              margin-bottom: 1em;
            }
            .grid {
              display: flex;
              flex-wrap: wrap;
              justify-content: center;
              gap: 10px;
            }
            .grid img {
              max-width: 220px;
              height: auto;
              display: block;
              border-radius: 4px;
              box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            .download {
              display: inline-block;
              margin-bottom: 20px;
              font-weight: bold;
              text-decoration: none;
              border: 2px solid #333;
              padding: 10px 20px;
              border-radius: 4px;
              color: #333;
            }
            .download:hover {
              background-color: #333;
              color: #fff;
            }
          </style>
        </head>
        <body>
          <div class='container'>
            <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' class='logo'>
            <a class='download' href='{{ zip_url }}'>Download All (ZIP)</a>
            <h1>Roll {{ sticker }}</h1>
            <div class='grid'>
              {% for url in thumb_urls %}
                <img src='{{ url }}' alt='Scan {{ loop.index }}'>
              {% endfor %}
            </div>
          </div>
        </body>
        </html>
        """, sticker=sticker, thumb_urls=thumb_urls, zip_url=zip_url)

    return render_template_string("""
    <!DOCTYPE html>
    <html lang='en'>
    <head>
      <meta charset='UTF-8'>
      <meta name='viewport' content='width=device-width, initial-scale=1.0'>
      <title>Enter Password – Roll {{ sticker }}</title>
      <style>
        body {
          font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
          background-color: #fff;
          color: #333;
          margin: 0;
          padding: 0;
        }
        .container {
          max-width: 400px;
          margin: 100px auto;
          text-align: center;
        }
        .logo {
          max-width: 200px;
          height: auto;
          margin-bottom: 20px;
        }
        input[type='password'] {
          width: 100%;
          padding: 10px;
          margin-top: 20px;
          margin-bottom: 20px;
          font-size: 1em;
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
        }
        button:hover {
          background-color: #333;
          color: #fff;
        }
      </style>
    </head>
    <body>
      <div class='container'>
        <img src='https://cdn.sumup.store/shops/06666267/settings/th480/b23c5cae-b59a-41f7-a55e-1b145f750153.png' alt='Logo' class='logo'>
        <h2>Enter password to access Roll {{ sticker }}</h2>
        <form method='POST'>
          <input type='password' name='password' placeholder='Password' required>
          <button type='submit'>Submit</button>
        </form>
      </div>
    </body>
    </html>
    """, sticker=sticker)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
