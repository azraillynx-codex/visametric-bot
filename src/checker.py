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

# ── LOGGING ───────────────────────────────────────────────────────────────────
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

# ── USERS CONFIG ──────────────────────────────────────────────────────────────
USERS = [
    {
        "name": "sonn",
        "email": "exam.jimsonshajum@gmail.com",
        "telegram_id": "5682340418",
        "prefs": {
            "from_country": "Ireland",
            "to_country":   "Germany",
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "city":         "Dublin",
            "office":       "Dublin",
            "service_type": "NORMAL",
            "applicants":   "1 applicant",
        }
    },
    # Add more users here
]

# ── FLOW URLS ─────────────────────────────────────────────────────────────────
LANDING_URL    = "https://www.visametric.com"          # Step 1: country selector
APPOINTMENT_URL = "https://ie-appointment.visametric.com/en"  # captcha page

MAX_CAPTCHA_RETRIES = 5
SCREENSHOTS_DIR     = "debug_screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Human-like delays (seconds)
DELAY_SHORT  = 2
DELAY_MEDIUM = 4
DELAY_LONG   = 7


# ── HELPERS ───────────────────────────────────────────────────────────────────
def human_sleep(seconds: float):
    """Sleep with slight randomness to appear human."""
    import random
    actual = seconds + random.uniform(-0.3, 0.8)
    time.sleep(max(0.5, actual))

def save_screenshot(driver, label: str) -> str:
    ts   = datetime.now().strftime("%H%M%S")
    path = f"{SCREENSHOTS_DIR}/{ts}_{label}.png"
    try:
        driver.save_screenshot(path)
        log.debug(f"📸 Screenshot: {path}")
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")
    return path

def dump_page_info(driver, label: str):
    log.info(f"[{label}] title='{driver.title}' url={driver.current_url}")
    source = driver.page_source.lower()
    log.debug(f"[{label}] page length={len(source)} chars")

def detect_block(driver) -> str:
    title  = driver.title.lower()
    source = driver.page_source.lower()
    if not source.strip() or len(source) < 200:
        return "EMPTY_PAGE"
    if "just a moment" in title or ("checking your browser" in source):
        return "CLOUDFLARE_CHALLENGE"
    if "ray id" in source and "cloudflare" in source:
        return "CLOUDFLARE_BLOCK"
    if "access denied" in title or "access denied" in source:
        return "ACCESS_DENIED"
    if "403 forbidden" in source or title == "403":
        return "403_FORBIDDEN"
    return "none"


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────
def send_telegram(chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        log.debug(f"Telegram → {chat_id}: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send_telegram_photo(chat_id: str, photo_path: str, caption: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            requests.post(url, data={"chat_id": chat_id, "caption": caption[:1000]}, files={"photo": f}, timeout=15)
    except Exception as e:
        log.error(f"Telegram photo error: {e}")

def send_email(to: str, subject: str, body: str, attachment: str = None):
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        if attachment and os.path.exists(attachment):
            with open(attachment, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attachment)}")
            msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, to, msg.as_string())
        log.info(f"✉️ Email → {to}")
    except Exception as e:
        log.error(f"Email error: {e}")

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
            "img[alt*='captcha']", "img[class*='captcha']",
            "img[id*='captcha']", ".col-md-4 img", "form img",
        ]
        captcha_img = None
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                captcha_img = els[0]
                log.debug(f"Captcha img found: {sel}")
                break

        if captcha_img is None:
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            log.warning(f"No captcha img found. All {len(all_imgs)} images on page:")
            for i, img in enumerate(all_imgs):
                log.warning(f"  img[{i}] src={str(img.get_attribute('src'))[:80]} "
                            f"class={img.get_attribute('class')} id={img.get_attribute('id')}")
            return ""

        src = captcha_img.get_attribute("src") or ""
        if not src:
            log.warning("Captcha img has empty src")
            return ""

        log.debug(f"Captcha src type: {'base64' if src.startswith('data:') else 'url'}")

        if src.startswith("data:image"):
            _, data = src.split(",", 1)
            img_bytes = base64.b64decode(data)
        else:
            img_bytes = requests.get(src, timeout=10).content

        # Save raw captcha
        raw_path = f"{SCREENSHOTS_DIR}/captcha_raw_{datetime.now().strftime('%H%M%S')}.png"
        with open(raw_path, "wb") as f:
            f.write(img_bytes)

        # OpenCV preprocessing
        nparr  = np.frombuffer(img_bytes, np.uint8)
        img    = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
        den    = cv2.fastNlMeansDenoising(thr, h=30)
        scaled = cv2.resize(den, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        result = pytesseract.image_to_string(
            Image.fromarray(scaled),
            config="--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        ).strip()
        digits = "".join(filter(str.isdigit, result))
        log.info(f"OCR result: '{result}' → digits: '{digits}'")
        return digits

    except Exception as e:
        log.error(f"Captcha solve error: {e}")
        return ""


# ── DRIVER ────────────────────────────────────────────────────────────────────
def get_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1366,768")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-IE")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    # NO PROXY — GitHub Actions IPs are cleaner than free datacenter proxies
    # Webshare free tier datacenter IPs are instantly blocked by Cloudflare
    driver = uc.Chrome(options=options)
    return driver


# ── DROPDOWN HELPER ───────────────────────────────────────────────────────────
def select_dropdown_by_index(driver, wait, index: int, value: str):
    selects = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "select")))
    sel = Select(selects[index])
    matched = False
    # Exact match first
    for opt in sel.options:
        if opt.text.strip() == value:
            sel.select_by_visible_text(opt.text)
            log.debug(f"Dropdown[{index}] exact = '{opt.text}'")
            matched = True
            break
    # Partial match fallback
    if not matched:
        for opt in sel.options:
            if value.lower() in opt.text.lower():
                sel.select_by_visible_text(opt.text)
                log.debug(f"Dropdown[{index}] partial = '{opt.text}'")
                matched = True
                break
    if not matched:
        available = [o.text for o in sel.options]
        log.error(f"Dropdown[{index}]: could not match '{value}'. Options: {available}")
    human_sleep(DELAY_SHORT)


# ── CLOSE POPUP ───────────────────────────────────────────────────────────────
def close_popup_if_present(driver):
    """Close the fingerprint notice popup if it appears."""
    try:
        # Look for close button (X) on the popup
        close_selectors = [
            "button.close", ".modal .close", "[data-dismiss='modal']",
            "button[aria-label='Close']", ".popup-close", ".modal-header .close",
            "//button[contains(@class,'close')]",
        ]
        for sel in close_selectors:
            if sel.startswith("//"):
                els = driver.find_elements(By.XPATH, sel)
            else:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                els[0].click()
                log.info("✅ Popup closed")
                human_sleep(DELAY_SHORT)
                return True

        # Try clicking outside the modal
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_by_offset(10, 10).click().perform()
        human_sleep(1)
        log.debug("Clicked outside popup area")
        return True

    except Exception as e:
        log.debug(f"Popup close attempt: {e}")
        return False


# ── FULL FLOW ─────────────────────────────────────────────────────────────────
def check_for_user(user: dict) -> bool:
    driver = get_driver()
    wait   = WebDriverWait(driver, 25)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefs  = user["prefs"]

    try:
        # ════════════════════════════════════════════════════════════════
        # STEP 1: Go to visametric.com — select "Ireland" and "Germany"
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 1 — Loading visametric.com landing page")
        driver.get(LANDING_URL)
        human_sleep(DELAY_LONG)
        dump_page_info(driver, "step1_landing")
        ss = save_screenshot(driver, "01_landing")

        block = detect_block(driver)
        if block != "none":
            log.error(f"Blocked at landing: {block}")
            notify(user, f"🚫 Blocked at landing: {block}",
                f"🚫 <b>Blocked at step 1</b>\nType: <code>{block}</code>\nURL: {driver.current_url}\nTime: {now}", ss)
            return False

        # Select "I'm applying from" = Ireland
        log.info("Selecting 'Applying from: Ireland'")
        try:
            from_selects = driver.find_elements(By.TAG_NAME, "select")
            log.debug(f"Found {len(from_selects)} selects on landing page")
            if from_selects:
                from_sel = Select(from_selects[0])
                from_sel.select_by_visible_text(prefs["from_country"])
                log.info(f"✅ Selected from: {prefs['from_country']}")
                human_sleep(DELAY_MEDIUM)
        except Exception as e:
            log.warning(f"Could not select 'from' country: {e}")

        save_screenshot(driver, "01b_from_selected")

        # Select "I'm going to" = Germany (second dropdown loads after first)
        log.info("Selecting 'Going to: Germany'")
        try:
            # Wait for second dropdown to appear
            human_sleep(DELAY_SHORT)
            all_selects = driver.find_elements(By.TAG_NAME, "select")
            if len(all_selects) >= 2:
                to_sel = Select(all_selects[1])
                to_sel.select_by_visible_text(prefs["to_country"])
                log.info(f"✅ Selected to: {prefs['to_country']}")
                human_sleep(DELAY_LONG)  # Page redirects automatically
            else:
                log.warning(f"Only {len(all_selects)} select(s) found, expected 2")
        except Exception as e:
            log.warning(f"Could not select 'to' country: {e}")

        dump_page_info(driver, "step1_after_country_select")
        save_screenshot(driver, "01c_country_selected")

        # ════════════════════════════════════════════════════════════════
        # STEP 2: Handle fingerprint popup — close it
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 2 — Checking for popup")
        human_sleep(DELAY_MEDIUM)

        # Check if modal/popup is visible
        popup_selectors = [
            ".modal.show", ".modal.in", "[role='dialog']",
            ".popup", ".overlay", ".modal-dialog",
        ]
        popup_found = False
        for sel in popup_selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].is_displayed():
                log.info(f"Popup detected: {sel}")
                popup_found = True
                break

        if popup_found:
            save_screenshot(driver, "02_popup_visible")
            close_popup_if_present(driver)
            save_screenshot(driver, "02b_popup_closed")
        else:
            log.info("No popup found, continuing")

        dump_page_info(driver, "step2_after_popup")

        # ════════════════════════════════════════════════════════════════
        # STEP 3: Click "Book an Appointment" button/link
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 3 — Finding 'Book an Appointment' button")
        human_sleep(DELAY_SHORT)
        save_screenshot(driver, "03_before_book_btn")

        book_btn = None
        book_xpaths = [
            "//a[contains(text(),'Book an Appointment')]",
            "//button[contains(text(),'Book an Appointment')]",
            "//a[contains(@href,'appointment')]",
            "//a[contains(text(),'Book')]",
        ]
        for xpath in book_xpaths:
            els = driver.find_elements(By.XPATH, xpath)
            if els:
                book_btn = els[0]
                log.info(f"Book button found: {xpath}")
                break

        if not book_btn:
            log.error("'Book an Appointment' button NOT found!")
            # Log all links on page for diagnosis
            links = driver.find_elements(By.TAG_NAME, "a")
            log.error(f"All links ({len(links)}):")
            for lnk in links[:20]:
                log.error(f"  <a href='{lnk.get_attribute('href')}' text='{lnk.text[:50]}'>")
            ss = save_screenshot(driver, "03_no_book_btn")
            notify(user, "⚠️ Book button not found",
                f"⚠️ Could not find 'Book an Appointment'\nURL: {driver.current_url}\nTime: {now}", ss)
            return False

        book_btn.click()
        human_sleep(DELAY_LONG)
        dump_page_info(driver, "step3_after_book_click")
        save_screenshot(driver, "03b_after_book_click")

        # ════════════════════════════════════════════════════════════════
        # STEP 4: Click the green "Book an Appointment" button (second one)
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 4 — Looking for green 'Book an Appointment' button")
        human_sleep(DELAY_SHORT)

        green_btn = None
        green_xpaths = [
            "//a[contains(@class,'btn-success') and contains(text(),'Book')]",
            "//button[contains(@class,'btn-success')]",
            "//a[contains(@class,'btn-primary') and contains(text(),'Book')]",
            "//a[contains(@class,'green') and contains(text(),'Book')]",
            "//button[contains(text(),'Book an Appointment')]",
            "//a[contains(text(),'Book an Appointment')]",
        ]
        for xpath in green_xpaths:
            els = driver.find_elements(By.XPATH, xpath)
            if els:
                green_btn = els[0]
                log.info(f"Green button found: {xpath}")
                break

        if green_btn:
            green_btn.click()
            human_sleep(DELAY_LONG)
            dump_page_info(driver, "step4_after_green_btn")
            save_screenshot(driver, "04_after_green_btn")
        else:
            log.info("No separate green button — may have gone directly to captcha page")

        # ════════════════════════════════════════════════════════════════
        # STEP 5: Now on captcha page — solve and submit
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 5 — Captcha page")

        # If we're not on the appointment URL yet, navigate there directly
        if "ie-appointment.visametric.com" not in driver.current_url:
            log.info(f"Not on appointment URL yet ({driver.current_url}), navigating directly")
            driver.get(APPOINTMENT_URL)
            human_sleep(DELAY_LONG)

        dump_page_info(driver, "step5_captcha_page")
        save_screenshot(driver, "05_captcha_page")

        block = detect_block(driver)
        if block != "none":
            log.error(f"Blocked at captcha page: {block}")
            ss = save_screenshot(driver, "05_blocked")
            notify(user, f"🚫 Blocked at captcha: {block}",
                f"🚫 <b>Blocked at captcha step</b>\nType: <code>{block}</code>\nURL: {driver.current_url}\nTime: {now}", ss)
            return False

        # Log all inputs for diagnosis
        inputs = driver.find_elements(By.TAG_NAME, "input")
        log.debug(f"Inputs on captcha page ({len(inputs)}):")
        for inp in inputs:
            log.debug(f"  type={inp.get_attribute('type')} name={inp.get_attribute('name')} "
                      f"id={inp.get_attribute('id')} placeholder={inp.get_attribute('placeholder')}")

        # Captcha solve loop
        solved = False
        for attempt in range(MAX_CAPTCHA_RETRIES):
            log.info(f"--- Captcha attempt {attempt+1}/{MAX_CAPTCHA_RETRIES} ---")

            if attempt == 0:
                html_path = f"{SCREENSHOTS_DIR}/captcha_page_source.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                log.debug(f"Captcha page HTML saved: {html_path}")

            captcha_text = solve_captcha(driver)
            log.info(f"OCR digits: '{captcha_text}' (len={len(captcha_text)})")

            if len(captcha_text) != 4:
                log.warning(f"Bad OCR '{captcha_text}', refreshing")
                save_screenshot(driver, f"captcha_bad_{attempt+1}")
                driver.refresh()
                human_sleep(DELAY_MEDIUM)
                continue

            # Enter captcha
            captcha_input = None
            for sel in [
                "input[placeholder='VERIFICATION CODE']",
                "input[placeholder*='erification']",
                "input[placeholder*='aptcha']",
                "input[name*='captcha']", "input[id*='captcha']",
                ".captcha input", "input[type='text']",
            ]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    captcha_input = els[0]
                    log.debug(f"Captcha input: {sel}")
                    break

            if not captcha_input:
                log.error("Captcha input field not found!")
                save_screenshot(driver, f"no_input_{attempt+1}")
                break

            captcha_input.clear()
            human_sleep(0.3)
            captcha_input.send_keys(captcha_text)
            human_sleep(0.5)

            # Click button
            btn = None
            for xpath in [
                "//button[contains(text(),'Get your appointment')]",
                "//a[contains(text(),'Get your appointment')]",
                "//input[@value='Get your appointment']",
                "//button[contains(@class,'btn-danger')]",
                "//button[contains(@class,'btn-primary')]",
            ]:
                els = driver.find_elements(By.XPATH, xpath)
                if els:
                    btn = els[0]
                    log.debug(f"Submit btn: {xpath}")
                    break

            if not btn:
                log.error("Submit button not found!")
                save_screenshot(driver, f"no_btn_{attempt+1}")
                break

            btn.click()
            human_sleep(DELAY_LONG)

            ss_after = save_screenshot(driver, f"captcha_after_{attempt+1}")
            dump_page_info(driver, f"step5_after_captcha_{attempt+1}")

            block_after = detect_block(driver)
            if block_after != "none":
                log.error(f"Blocked after captcha: {block_after}")
                notify(user, f"🚫 Blocked: {block_after}",
                    f"🚫 Blocked after captcha\n<code>{block_after}</code>\nURL: {driver.current_url}\nTime: {now}", ss_after)
                return False

            selects_found = driver.find_elements(By.TAG_NAME, "select")
            if len(selects_found) > 0:
                solved = True
                log.info(f"✅ Captcha SOLVED on attempt {attempt+1} — {len(selects_found)} dropdowns found")
                break

            log.warning(f"Captcha attempt {attempt+1} failed — no dropdowns. Refreshing...")
            driver.refresh()
            human_sleep(DELAY_MEDIUM)

        if not solved:
            ss_fail = save_screenshot(driver, "captcha_all_failed")
            log.error(f"All {MAX_CAPTCHA_RETRIES} captcha attempts failed")
            log.error(f"Page title: {driver.title} | URL: {driver.current_url}")
            notify(user, "⚠️ Captcha Failed",
                f"⚠️ <b>Captcha failed {MAX_CAPTCHA_RETRIES}x</b>\n\n"
                f"👤 {user['name']}\n🕒 {now}\n📄 {driver.title}\n🌐 {driver.current_url}\nSee screenshot.", ss_fail)
            return False

        # ════════════════════════════════════════════════════════════════
        # STEP 6: Fill form dropdowns
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 6 — Filling form")
        human_sleep(DELAY_SHORT)

        # Log all dropdown options for diagnosis
        all_selects = driver.find_elements(By.TAG_NAME, "select")
        log.debug(f"Form has {len(all_selects)} dropdowns:")
        for i, s in enumerate(all_selects):
            opts = [o.text for o in Select(s).options]
            log.debug(f"  select[{i}] options: {opts}")

        select_dropdown_by_index(driver, wait, 0, prefs["application_type"])
        select_dropdown_by_index(driver, wait, 1, prefs["from_country"])   # Country of visit = Ireland context
        select_dropdown_by_index(driver, wait, 2, prefs["city"])
        select_dropdown_by_index(driver, wait, 3, prefs["office"])
        select_dropdown_by_index(driver, wait, 4, prefs["service_type"])
        select_dropdown_by_index(driver, wait, 5, prefs["applicants"])
        save_screenshot(driver, "06_form_filled")

        # ════════════════════════════════════════════════════════════════
        # STEP 7: Click NEXT
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 7 — Clicking NEXT")
        next_btn = driver.find_element(By.XPATH,
            "//button[contains(text(),'NEXT')] | //a[contains(text(),'NEXT')] | "
            "//button[contains(@class,'btn-success')] | //input[@value='NEXT']")
        next_btn.click()
        human_sleep(DELAY_LONG)
        save_screenshot(driver, "07_after_next")
        dump_page_info(driver, "step7_results")

        # ════════════════════════════════════════════════════════════════
        # STEP 8: Check for available dates
        # ════════════════════════════════════════════════════════════════
        log.info("STEP 8 — Checking for available dates")
        dates = re.findall(r'\d{2}-\d{2}-\d{4}', driver.page_source)
        log.info(f"Dates found: {dates}")

        if dates:
            dates_str = ", ".join(set(dates))
            ss = save_screenshot(driver, "08_slot_found")
            msg = (f"🟢 <b>SLOT FOUND!</b>\n\n"
                   f"👤 {user['name']}\n📅 <b>{dates_str}</b>\n"
                   f"🕒 {now}\n\n👉 Book now: {APPOINTMENT_URL}")
            notify(user, "✅ VisaMetric Slot Available!", msg, ss)
            log.info(f"🎉 SLOT FOUND for {user['name']}: {dates_str}")
            return True
        else:
            save_screenshot(driver, "08_no_slot")
            notify(user, "❌ No Slot",
                f"🔴 <b>No slot found</b>\n👤 {user['name']}\n🕒 {now}\nNext check ~1hr.")
            log.info(f"No slot for {user['name']} at {now}")
            return False

    except Exception as e:
        log.exception(f"Unhandled error for {user['name']}: {e}")
        ss = save_screenshot(driver, "error")
        notify(user, "Bot Error", f"⚠️ <code>{e}</code>\nTime: {now}", ss)
        return False
    finally:
        driver.quit()


# ── ENTRY ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"=== VisaMetric Checker started {datetime.now()} ===")
    for user in USERS:
        log.info(f"\n{'='*50}\nUser: {user['name']}\n{'='*50}")
        check_for_user(user)
    log.info("=== All done ===")