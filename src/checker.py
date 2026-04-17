import os
import re
import time
import base64
import logging
import smtplib
import requests
import cv2
import numpy as np
import pytesseract
from PIL import Image
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
try:
    # selenium-wire handles authenticated HTTP proxy at Python level
    # (Chrome's --proxy-server flag silently drops credentials since Chrome 72)
    import seleniumwire.undetected_chromedriver as ucwire
    SELENIUMWIRE_AVAILABLE = True
except ImportError:
    SELENIUMWIRE_AVAILABLE = False
    log_import_warn = True

# ── LOGGING SETUP ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("visametric")

# ── ENV VARS ──────────────────────────────────────────────────────────────────
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ADMIN_EMAIL        = os.environ["ADMIN_EMAIL"]
ADMIN_TELEGRAM_ID  = os.environ["ADMIN_TELEGRAM_CHAT_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROXY_URL          = os.environ.get("PROXY_URL", "")  # Optional: e.g. http://user:pass@host:port

# ── USERS CONFIG ──────────────────────────────────────────────────────────────
USERS = [
    {
        "name": "sonn",
        "email": "exam.jimsonshajum@gmail.com",          # ← replace
        "telegram_id": "5682340418",            # ← replace
        "prefs": {
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "country": "Ireland",
            "city": "Dublin",
            "office": "Dublin",
            "service_type": "NORMAL",
            "applicants": "1 applicant",
        }
    },
]

TARGET_URL          = "https://ie-appointment.visametric.com/en"
MAX_CAPTCHA_RETRIES = 5
SCREENSHOTS_DIR     = "debug_screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


# ── SCREENSHOT ────────────────────────────────────────────────────────────────
def save_screenshot(driver, label: str) -> str:
    ts = datetime.now().strftime("%H%M%S")
    path = f"{SCREENSHOTS_DIR}/{ts}_{label}.png"
    try:
        driver.save_screenshot(path)
        log.debug(f"Screenshot: {path}")
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")
    return path


# ── BLOCK DETECTOR ────────────────────────────────────────────────────────────
def detect_block(driver) -> str:
    title  = driver.title.lower()
    source = driver.page_source.lower()
    url    = driver.current_url.lower()
    # Proxy errors: page fails to load entirely
    if "this site can\u2019t be reached" in source or "err_no_supported_proxies" in source:
        return "PROXY_ERROR"
    if "err_proxy" in source or "proxy" in title and "error" in title:
        return "PROXY_ERROR"
    # Cloudflare
    if "just a moment" in title or "just a moment" in source:
        return "CLOUDFLARE_CHALLENGE"
    if "attention required" in title or ("ray id" in source and "cloudflare" in source):
        return "CLOUDFLARE_BLOCK"
    if "access denied" in title or "access denied" in source:
        return "ACCESS_DENIED"
    if "403" in title or "403 forbidden" in source:
        return "403_FORBIDDEN"
    if "captcha" not in source and "verification" not in source and "appointment" not in source:
        return "UNEXPECTED_PAGE"
    return "none"


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        log.debug(f"Telegram {chat_id}: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send_telegram_photo(chat_id: str, photo_path: str, caption: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"photo": f}, timeout=15)
    except Exception as e:
        log.error(f"Telegram photo error: {e}")


# ── EMAIL ─────────────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, body: str, attachment_path: str = None):
    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_USER
        msg["To"]   = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attachment_path)}")
            msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, to, msg.as_string())
        log.info(f"Email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")


# ── NOTIFY ────────────────────────────────────────────────────────────────────
def notify(user: dict, subject: str, message: str, screenshot: str = None):
    send_telegram(user["telegram_id"], message)
    send_email(user["email"], subject, message, screenshot)
    send_telegram(ADMIN_TELEGRAM_ID, f"[{user['name']}] {message}")
    send_email(ADMIN_EMAIL, f"[{user['name']}] {subject}", message, screenshot)
    if screenshot and os.path.exists(screenshot):
        send_telegram_photo(ADMIN_TELEGRAM_ID, screenshot, f"[{user['name']}] {subject}")


# ── CAPTCHA SOLVER ────────────────────────────────────────────────────────────
def solve_captcha(driver) -> str:
    try:
        selectors = [
            "img[src*='captcha']", "img[src*='Captcha']",
            ".captcha-image img", ".captcha img",
            "#captcha img", "#captchaImg", "img#captcha",
            "img[alt*='captcha']", "img[alt*='Captcha']",
            "img[class*='captcha']", "img[id*='captcha']",
            ".col-md-4 img", "form img",
        ]
        captcha_img = None
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                captcha_img = els[0]
                log.debug(f"Captcha img found via: {sel}")
                break

        if captcha_img is None:
            # Last resort: grab ALL images and log them
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            log.warning(f"No captcha img found. All imgs on page ({len(all_imgs)}):")
            for i, img in enumerate(all_imgs):
                log.warning(f"  img[{i}] src={img.get_attribute('src', '')[:80]} "
                            f"class={img.get_attribute('class')} "
                            f"id={img.get_attribute('id')} "
                            f"alt={img.get_attribute('alt')}")
            return ""

        src = captcha_img.get_attribute("src") or ""
        log.debug(f"Captcha src: {'base64' if src.startswith('data:') else src[:80]}")

        if not src:
            log.warning("Captcha img has no src attribute")
            return ""

        if src.startswith("data:image"):
            _, data = src.split(",", 1)
            img_bytes = base64.b64decode(data)
        else:
            img_bytes = requests.get(src, timeout=10).content

        raw_path = f"{SCREENSHOTS_DIR}/captcha_raw_{datetime.now().strftime('%H%M%S')}.png"
        with open(raw_path, "wb") as f:
            f.write(img_bytes)
        log.debug(f"Raw captcha saved: {raw_path}")

        nparr   = np.frombuffer(img_bytes, np.uint8)
        img     = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            log.error("cv2 could not decode captcha image bytes")
            return ""
        log.debug(f"Captcha image shape: {img.shape}")

        gray    = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thr  = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
        den     = cv2.fastNlMeansDenoising(thr, h=30)
        scaled  = cv2.resize(den, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        # Save processed image for debugging
        proc_path = f"{SCREENSHOTS_DIR}/captcha_processed_{datetime.now().strftime('%H%M%S')}.png"
        cv2.imwrite(proc_path, scaled)
        log.debug(f"Processed captcha saved: {proc_path}")

        result = pytesseract.image_to_string(
            Image.fromarray(scaled),
            config="--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        ).strip()
        digits = "".join(filter(str.isdigit, result))
        log.info(f"OCR: raw='{result}' → digits='{digits}'")
        return digits

    except Exception as e:
        log.error(f"Captcha error: {e}")
        return ""


# ── DRIVER ────────────────────────────────────────────────────────────────────
def _get_chrome_major_version() -> int:
    """Detect the installed Chrome major version so we can pin ChromeDriver."""
    import subprocess
    for binary in ("/bin/google-chrome-stable", "/usr/bin/google-chrome",
                   "/usr/bin/chromium-browser", "/usr/bin/chromium"):
        try:
            out = subprocess.check_output(
                [binary, "--version"], stderr=subprocess.DEVNULL
            ).decode().strip()
            match = re.search(r"(\d+)\.", out)
            if match:
                version = int(match.group(1))
                log.debug(f"Detected Chrome {version} from {binary}")
                return version
        except Exception:
            continue
    log.warning("Could not detect Chrome version, defaulting to 146")
    return 146

def get_driver():
    chrome_version = _get_chrome_major_version()
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--accept-lang=en-US,en;q=0.9")
    options.add_argument(
        f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
    )

    # ── Proxy via selenium-wire (handles HTTP auth that Chrome can't do natively) ──
    if PROXY_URL and SELENIUMWIRE_AVAILABLE:
        host_part = PROXY_URL.split("@")[-1]
        log.info(f"Using selenium-wire proxy: {host_part}")
        sw_options = {
            "proxy": {
                "http":  PROXY_URL,
                "https": PROXY_URL,
                "no_proxy": "localhost,127.0.0.1"
            },
            "verify_ssl": False,
        }
        return ucwire.Chrome(options=options, seleniumwire_options=sw_options, version_main=chrome_version)
    elif PROXY_URL and not SELENIUMWIRE_AVAILABLE:
        log.error("selenium-wire not installed! Install it: pip install selenium-wire==5.1.0")
        log.warning("Falling back to no proxy — may be Cloudflare blocked")
    else:
        log.warning("No PROXY_URL set — GitHub Actions IP may be blocked by Cloudflare")

    return uc.Chrome(options=options, version_main=chrome_version)


# ── DROPDOWN ──────────────────────────────────────────────────────────────────
def select_dropdown(driver, wait, index: int, value: str):
    selects = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "select")))
    sel = Select(selects[index])
    try:
        sel.select_by_visible_text(value)
        log.debug(f"Dropdown[{index}] = '{value}'")
    except Exception:
        for opt in sel.options:
            if value.lower() in opt.text.lower():
                sel.select_by_visible_text(opt.text)
                log.debug(f"Dropdown[{index}] partial match = '{opt.text}'")
                break
    time.sleep(0.8)


# ── MAIN CHECK ────────────────────────────────────────────────────────────────
def check_for_user(user: dict) -> bool:
    driver = get_driver()
    wait   = WebDriverWait(driver, 20)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefs  = user["prefs"]

    try:
        log.info(f"GET {TARGET_URL}")
        driver.get(TARGET_URL)
        time.sleep(4)

        log.info(f"Title: '{driver.title}' | URL: {driver.current_url}")
        ss_init = save_screenshot(driver, "01_initial_load")

        # ── Block check ──
        block = detect_block(driver)
        if block != "none":
            log.error(f"BLOCKED: {block}")
            notify(user,
                f"🚫 Bot Blocked: {block}",
                f"🚫 <b>Bot is BLOCKED</b>\n\nType: <code>{block}</code>\nURL: {driver.current_url}\nTitle: {driver.title}\nTime: {now}\n\nSee screenshot.",
                ss_init)
            return False

        log.info("No block detected ✅")

        # ── Captcha loop ──
        solved = False
        for attempt in range(MAX_CAPTCHA_RETRIES):
            log.info(f"--- Captcha attempt {attempt+1}/{MAX_CAPTCHA_RETRIES} ---")

            # On first attempt: dump page HTML for diagnosis
            if attempt == 0:
                html_path = f"{SCREENSHOTS_DIR}/page_source_attempt1.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                log.debug(f"Page HTML dumped to {html_path}")
                log.debug(f"Page title: {driver.title}")
                log.debug(f"Page URL: {driver.current_url}")
                # Log all input fields on page
                inputs = driver.find_elements(By.TAG_NAME, "input")
                log.debug(f"Inputs on page ({len(inputs)}):")
                for inp in inputs:
                    log.debug(f"  input type={inp.get_attribute('type')} "
                              f"name={inp.get_attribute('name')} "
                              f"id={inp.get_attribute('id')} "
                              f"placeholder={inp.get_attribute('placeholder')}")

            captcha_text = solve_captcha(driver)

            if len(captcha_text) != 4:
                log.warning(f"Bad OCR result '{captcha_text}', refreshing")
                save_screenshot(driver, f"captcha_bad_{attempt+1}")
                driver.refresh()
                time.sleep(3)
                continue

            # Find input
            captcha_input = None
            for sel in ["input[placeholder='VERIFICATION CODE']", "input[placeholder*='verification']",
                        "input[placeholder*='captcha']", "input[name*='captcha']", ".captcha input"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    captcha_input = els[0]
                    break

            if not captcha_input:
                log.error("Captcha input field not found!")
                save_screenshot(driver, f"no_input_{attempt+1}")
                break

            captcha_input.clear()
            captcha_input.send_keys(captcha_text)
            time.sleep(0.5)

            # Find button
            btn = None
            for xpath in ["//button[contains(text(),'Get your appointment')]",
                          "//a[contains(text(),'Get your appointment')]",
                          "//button[contains(@class,'btn')]"]:
                els = driver.find_elements(By.XPATH, xpath)
                if els:
                    btn = els[0]
                    break

            if not btn:
                log.error("Appointment button not found!")
                save_screenshot(driver, f"no_btn_{attempt+1}")
                break

            btn.click()
            time.sleep(3)

            ss_after = save_screenshot(driver, f"captcha_after_{attempt+1}")
            log.info(f"Post-click → title: '{driver.title}' url: {driver.current_url}")

            block_after = detect_block(driver)
            if block_after != "none":
                log.error(f"Blocked after captcha: {block_after}")
                notify(user, f"🚫 Blocked: {block_after}",
                    f"🚫 Blocked after captcha\nType: <code>{block_after}</code>\nURL: {driver.current_url}\nTime: {now}",
                    ss_after)
                return False

            if len(driver.find_elements(By.TAG_NAME, "select")) > 0:
                solved = True
                log.info(f"✅ Captcha solved on attempt {attempt+1}")
                break

            log.warning(f"Captcha attempt {attempt+1} failed — no dropdowns found, retrying")
            driver.refresh()
            time.sleep(3)

        if not solved:
            ss_fail = save_screenshot(driver, "captcha_all_failed")
            log.error(f"All {MAX_CAPTCHA_RETRIES} captcha attempts failed")
            log.error(f"Final page title: {driver.title}")
            log.error(f"Final URL: {driver.current_url}")
            log.error(f"Page source snippet: {driver.page_source[:800]}")
            notify(user, "⚠️ Captcha Failed",
                f"⚠️ <b>Captcha bypass failed</b>\n\n"
                f"👤 {user['name']}\n🕒 {now}\n"
                f"📄 Title: {driver.title}\n🌐 URL: {driver.current_url}\n"
                f"🔁 Tried: {MAX_CAPTCHA_RETRIES}x\n\nSee screenshot.",
                ss_fail)
            return False

        # ── Fill form ──
        log.info("Filling form...")
        time.sleep(2)
        select_dropdown(driver, wait, 0, prefs["application_type"])
        select_dropdown(driver, wait, 1, prefs["country"])
        select_dropdown(driver, wait, 2, prefs["city"])
        select_dropdown(driver, wait, 3, prefs["office"])
        select_dropdown(driver, wait, 4, prefs["service_type"])
        select_dropdown(driver, wait, 5, prefs["applicants"])
        save_screenshot(driver, "02_form_filled")

        # ── Click NEXT ──
        log.info("Clicking NEXT...")
        next_btn = driver.find_element(By.XPATH,
            "//button[contains(text(),'NEXT')] | //a[contains(text(),'NEXT')]")
        next_btn.click()
        time.sleep(3)
        save_screenshot(driver, "03_after_next")

        # ── Check dates ──
        log.info("Checking for dates...")
        dates = re.findall(r'\d{2}-\d{2}-\d{4}', driver.page_source)
        log.info(f"Dates found: {dates}")

        if dates:
            dates_str = ", ".join(set(dates))
            ss = save_screenshot(driver, "04_slot_found")
            notify(user, "✅ Slot Available!",
                f"🟢 <b>SLOT FOUND!</b>\n\n👤 {user['name']}\n📅 <b>{dates_str}</b>\n🕒 {now}\n\n👉 {TARGET_URL}",
                ss)
            return True
        else:
            save_screenshot(driver, "04_no_slot")
            notify(user, "❌ No Slot", f"🔴 <b>No slot</b>\n👤 {user['name']}\n🕒 {now}\nNext check ~1hr.")
            return False

    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        ss = save_screenshot(driver, "error")
        notify(user, "Bot Error", f"⚠️ Error: <code>{e}</code>\nTime: {now}", ss)
        return False
    finally:
        driver.quit()


# ── ENTRY ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"=== VisaMetric Checker started {datetime.now()} ===")
    for user in USERS:
        log.info(f"\n{'='*50}\nUser: {user['name']}\n{'='*50}")
        check_for_user(user)
    log.info("=== Done ===")
