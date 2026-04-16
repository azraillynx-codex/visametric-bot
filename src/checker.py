import os
import time
import smtplib
import requests
import cv2
import numpy as np
import pytesseract
from PIL import Image
from io import BytesIO
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import undetected_chromedriver as uc

# ── ENV VARS (set as GitHub Secrets) ─────────────────────────────────────────
GMAIL_USER            = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD    = os.environ["GMAIL_APP_PASSWORD"]
ADMIN_EMAIL           = os.environ["ADMIN_EMAIL"]
ADMIN_TELEGRAM_ID     = os.environ["ADMIN_TELEGRAM_CHAT_ID"]
TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]

# ── USERS CONFIG ─────────────────────────────────────────────────────────────
# Add each user here: name, email, telegram_chat_id, and their form preferences
USERS = [
    {
        "name": "User1",
        "email": "user1@example.com",          # ← replace
        "telegram_id": "123456789",            # ← replace
        "prefs": {
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "country": "Ireland",
            "city": "Dublin",
            "office": "Dublin",
            "service_type": "NORMAL",
            "applicants": "1 applicant",
        }
    },
    # Add more users here same way
]

TARGET_URL = "https://ie-appointment.visametric.com/en"
MAX_CAPTCHA_RETRIES = 5


# ── TELEGRAM ALERT ────────────────────────────────────────────────────────────
def send_telegram(chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


# ── EMAIL ALERT ───────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, body: str):
    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_USER
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to, msg.as_string())
        print(f"Email sent to {to}")
    except Exception as e:
        print(f"Email error: {e}")


# ── NOTIFY BOTH CHANNELS ──────────────────────────────────────────────────────
def notify(user: dict, subject: str, message: str):
    send_telegram(user["telegram_id"], message)
    send_email(user["email"], subject, message)
    # Also notify admin
    send_telegram(ADMIN_TELEGRAM_ID, f"[{user['name']}] {message}")
    send_email(ADMIN_EMAIL, f"[{user['name']}] {subject}", message)


# ── CAPTCHA SOLVER ────────────────────────────────────────────────────────────
def solve_captcha(driver) -> str:
    """
    Finds the captcha image, preprocesses with OpenCV, reads with Tesseract.
    Returns the 4-digit string or empty string on failure.
    """
    try:
        captcha_img = driver.find_element(By.CSS_SELECTOR, "img[src*='captcha'], .captcha img, #captcha img")
        src = captcha_img.get_attribute("src")

        if src.startswith("data:image"):
            # Base64 image
            import base64
            header, data = src.split(",", 1)
            img_bytes = base64.b64decode(data)
        else:
            # URL image
            resp = requests.get(src, timeout=10)
            img_bytes = resp.content

        # OpenCV preprocessing
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Threshold to remove noise
        _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
        # Denoise
        denoised = cv2.fastNlMeansDenoising(thresh, h=30)
        # Scale up for better OCR
        scaled = cv2.resize(denoised, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        pil_img = Image.fromarray(scaled)
        # Tesseract config: only digits, single line
        result = pytesseract.image_to_string(
            pil_img,
            config="--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        ).strip()

        # Clean result - keep only digits
        digits = "".join(filter(str.isdigit, result))
        print(f"Captcha solved: '{digits}'")
        return digits

    except Exception as e:
        print(f"Captcha solve error: {e}")
        return ""


# ── SETUP DRIVER ──────────────────────────────────────────────────────────────
def get_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = uc.Chrome(options=options)
    return driver


# ── FILL DROPDOWN ─────────────────────────────────────────────────────────────
def select_dropdown(driver, wait, label_or_index: int, value: str):
    """Select by visible text in the Nth select element (0-indexed)."""
    selects = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "select")))
    sel = Select(selects[label_or_index])
    try:
        sel.select_by_visible_text(value)
    except Exception:
        # Try partial match
        for option in sel.options:
            if value.lower() in option.text.lower():
                sel.select_by_visible_text(option.text)
                break
    time.sleep(0.8)


# ── MAIN CHECK FOR ONE USER ───────────────────────────────────────────────────
def check_for_user(user: dict) -> bool:
    """
    Returns True if a slot was found, False otherwise.
    """
    driver = get_driver()
    wait = WebDriverWait(driver, 20)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefs = user["prefs"]

    try:
        driver.get(TARGET_URL)
        time.sleep(3)

        # ── STEP 1: Solve captcha and click "Get your appointment" ──
        solved = False
        for attempt in range(MAX_CAPTCHA_RETRIES):
            captcha_text = solve_captcha(driver)
            if len(captcha_text) == 4:
                # Enter captcha
                captcha_input = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='VERIFICATION CODE'], input[name*='captcha'], #captcha_input"))
                )
                captcha_input.clear()
                captcha_input.send_keys(captcha_text)
                time.sleep(0.5)

                # Click "Get your appointment"
                btn = driver.find_element(By.XPATH, "//button[contains(text(),'Get your appointment')] | //a[contains(text(),'Get your appointment')]")
                btn.click()
                time.sleep(3)

                # Check if we moved to next page (form page)
                if "appointment" in driver.current_url or len(driver.find_elements(By.TAG_NAME, "select")) > 0:
                    solved = True
                    print(f"Captcha passed on attempt {attempt+1}")
                    break
                else:
                    print(f"Captcha attempt {attempt+1} failed, retrying...")
                    driver.refresh()
                    time.sleep(2)
            else:
                print(f"OCR returned '{captcha_text}', retrying...")
                driver.refresh()
                time.sleep(2)

        if not solved:
            notify(user, "VisaMetric Check Failed", f"⚠️ <b>Captcha bypass failed</b> after {MAX_CAPTCHA_RETRIES} attempts at {now}. Will retry next scheduled run.")
            return False

        # ── STEP 2: Fill the form ──
        time.sleep(2)
        select_dropdown(driver, wait, 0, prefs["application_type"])
        select_dropdown(driver, wait, 1, prefs["country"])
        select_dropdown(driver, wait, 2, prefs["city"])
        select_dropdown(driver, wait, 3, prefs["office"])
        select_dropdown(driver, wait, 4, prefs["service_type"])
        select_dropdown(driver, wait, 5, prefs["applicants"])
        time.sleep(1)

        # ── STEP 3: Click NEXT ──
        next_btn = driver.find_element(By.XPATH, "//button[contains(text(),'NEXT')] | //a[contains(text(),'NEXT')]")
        next_btn.click()
        time.sleep(3)

        # ── STEP 4: Check for available dates ──
        page_source = driver.page_source.lower()

        # Look for date patterns like "15-04-2026" or a calendar with available slots
        import re
        date_pattern = re.findall(r'\d{2}-\d{2}-\d{4}', driver.page_source)
        
        # Check for "no appointment" or "not available" messages
        no_slot_keywords = ["no appointment", "no available", "no slot", "currently no", "not available"]
        slot_found = any(kw not in page_source for kw in no_slot_keywords)

        if date_pattern:
            dates_str = ", ".join(set(date_pattern))
            message = (
                f"🟢 <b>SLOT FOUND!</b>\n\n"
                f"👤 User: {user['name']}\n"
                f"📅 Available date(s): <b>{dates_str}</b>\n"
                f"🕒 Checked at: {now}\n\n"
                f"👉 Book now: {TARGET_URL}"
            )
            notify(user, "✅ VisaMetric Slot Available!", message)
            print(f"SLOT FOUND for {user['name']}: {dates_str}")
            return True
        else:
            message = (
                f"🔴 <b>No slot found</b>\n\n"
                f"👤 User: {user['name']}\n"
                f"🕒 Checked at: {now}\n"
                f"ℹ️ Next check in ~1 hour."
            )
            notify(user, "❌ No VisaMetric Slot", message)
            print(f"No slot for {user['name']} at {now}")
            return False

    except Exception as e:
        error_msg = f"⚠️ <b>Error during check</b> for {user['name']} at {now}:\n<code>{str(e)}</code>"
        notify(user, "VisaMetric Bot Error", error_msg)
        print(f"Error: {e}")
        return False

    finally:
        driver.quit()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"=== VisaMetric Checker started at {datetime.now()} ===")
    for user in USERS:
        print(f"\nChecking for {user['name']}...")
        check_for_user(user)
    print("\n=== Done ===")
