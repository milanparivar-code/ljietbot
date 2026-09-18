"""Streamlit Cloud entry point for the Telegram leave bot.

Streamlit Cloud runs this file.  The Telegram bot itself remains the user
interface and is started once in a cached background worker so Streamlit
reruns do not create duplicate polling processes.
"""

import os
import threading

import streamlit as st
from dotenv import load_dotenv


load_dotenv()


_SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "PORTAL_USERNAME",
    "PORTAL_PASSWORD",
    "LOGIN_YEAR",
    "EMP_NAME",
    "DEPARTMENT",
    "POSITION",
    "ALLOWED_USER_IDS",
    "ADMIN_PASSWORD",
    "USE_BACKEND_API",
    "BACKEND_BASE_URL",
)


def _load_streamlit_secrets() -> None:
    """Copy flat Streamlit Secrets into the environment before importing app."""
    for key in _SECRET_KEYS:
        if os.getenv(key, "").strip():
            continue
        try:
            value = st.secrets[key]
        except (KeyError, FileNotFoundError):
            continue
        if value is not None and str(value).strip():
            os.environ[key] = str(value)


_load_streamlit_secrets()


@st.cache_resource(show_spinner=False)
def _start_telegram_worker():
    """Start one long-lived Telegram polling worker for this Streamlit app."""
    import app as leave_app

    worker = threading.Thread(
        target=leave_app.run_telegram_bot,
        name="telegram-leave-bot",
        daemon=True,
    )
    worker.start()
    return worker


st.set_page_config(
    page_title="LJIET Telegram Leave Bot",
    page_icon="📚",
    layout="centered",
)

st.title("📚 LJIET Telegram Leave Bot")
st.write("Streamlit Cloud host for the Telegram leave-management bot.")

if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
    st.error("TELEGRAM_BOT_TOKEN is not configured.")
    st.markdown(
        "Add the required values in **Streamlit Cloud → Settings → Secrets**. "
        "Do not commit `.env` or real credentials to GitHub."
    )
    st.code(
        "TELEGRAM_BOT_TOKEN = \"123456:AA...\"\n"
        "PORTAL_USERNAME = \"00000365\"\n"
        "PORTAL_PASSWORD = \"your-portal-password\"\n"
        "LOGIN_YEAR = \"01/07/2026LJIET\"\n"
        "EMP_NAME = \"MILAN PATEL\"\n"
        "DEPARTMENT = \"FY1\"\n"
        "POSITION = \"AP\"",
        language="toml",
    )
else:
    try:
        worker = _start_telegram_worker()
        st.success("Telegram bot worker is running.")
        st.caption(f"Worker: {worker.name} | The bot is ready in Telegram.")
    except Exception as exc:  # noqa: BLE001 - show deployment diagnostics in UI
        st.error("The Telegram bot could not be started.")
        st.exception(exc)

with st.expander("Deployment notes"):
    st.markdown(
        "- Select `streamlit_app.py` as the **Main file path** in Streamlit Cloud.\n"
        "- Add the values from `.env.example` to Streamlit Secrets.\n"
        "- The timetable spreadsheets and PDF fonts are bundled in this archive.\n"
        "- Streamlit Cloud may pause inactive apps; enable an external uptime monitor if continuous polling is required."
    )
