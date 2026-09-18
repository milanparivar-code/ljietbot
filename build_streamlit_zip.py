"""Build a safe GitHub/Streamlit Cloud deployment archive."""

import glob
import os
import zipfile


ZIP_FILENAME = "streamlit_upload.zip"

FILES = [
    ".streamlit/config.toml",
    "streamlit_app.py",
    "app.py",
    "telegram_bot.py",
    "pdf_generator.py",
    "shift_pdf_generator.py",
    "portal_api.py",
    "timetable_engine.py",
    "timezone_utils.py",
    "requirements.txt",
    ".env.example",
    "leaves_database.json",
    "README_TELEGRAM.md",
    "STREAMLIT_DEPLOYMENT.md",
    "SHIFT CHANGE_biometric number_Date_Name of Faculty_Dept name (2).pdf",
]


def main() -> None:
    files_to_include = list(FILES)
    for xlsx_file in glob.glob("*.xlsx"):
        name = os.path.basename(xlsx_file)
        if not name.startswith("~$") and name not in files_to_include:
            files_to_include.append(name)

    with zipfile.ZipFile(ZIP_FILENAME, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in files_to_include:
            if os.path.isfile(filename):
                archive.write(filename, filename)
                print(f"Added file: {filename}")
            else:
                print(f"Skipped missing file: {filename}")

        if os.path.isdir("fonts"):
            for root, _, filenames in os.walk("fonts"):
                for filename in filenames:
                    path = os.path.join(root, filename)
                    archive_name = os.path.relpath(path, ".")
                    archive.write(path, archive_name)
                    print(f"Added font: {archive_name}")

    print(f"\nSuccessfully created {ZIP_FILENAME} ({os.path.getsize(ZIP_FILENAME)} bytes).")


if __name__ == "__main__":
    main()
