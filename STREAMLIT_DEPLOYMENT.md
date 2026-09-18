# Streamlit Cloud deployment

This archive contains `streamlit_app.py`, which starts the Telegram bot once
inside a cached background worker. The Streamlit page is a small status page;
faculty continue using the bot in Telegram.

## GitHub / Streamlit Cloud

1. Upload the extracted archive contents to a GitHub repository. Keep the
   repository private if the bundled timetable and faculty files are private.
2. In Streamlit Cloud, select `streamlit_app.py` as the Main file path.
3. In **Settings → Secrets**, add the values from `.env.example`, especially:
   `TELEGRAM_BOT_TOKEN`, `PORTAL_USERNAME`, and `PORTAL_PASSWORD`.
4. Deploy or reboot the app and confirm the page shows **Telegram bot worker is
   running**.

Never upload the local `.env` file or real credentials to GitHub. The
`streamlit_upload.zip` archive intentionally excludes `.env` and the local
`faculty_store.json` credential store. Faculty members can register through
`/register` after the bot starts.

Streamlit Cloud storage is not a permanent database. If registered profiles
must survive app restarts, move the faculty store to a persistent database
before production use.

## Local test

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The timetable spreadsheets and `fonts/` directory are included because the
bot needs them for load adjustments and printable PDF generation.
