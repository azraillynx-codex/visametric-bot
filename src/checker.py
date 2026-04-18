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
from selenium.webdriver.common.action_chains import ActionChains
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
            "from_country":     "Ireland",
            "to_country":       "Germany",
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "city":             "Dublin",
            "office":           "Dublin",
            "service_type":     "NORMAL",
            "applicants":       "1 applicant",
        }
    },
]

# From the screenshot the actual entry URL after country selection lands here:
LANDING_URL     = "https://www.visametric.com"
APPOINTMENT_URL = "https://ie-appointment.visametric.com/en"

MAX_CAPTCHA_RETRIES = 5
SCREENSHOTS_DIR     = "debug_screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


# ── HELPERS ───────────────────────────────────────────────────────────────────
def hs(s=2):
    import random
    time.sleep(s + random.uniform(0.2, 0.8))

def ss(driver, label):
    ts = datetime.now().strftime("%H%M%S")
    p  = f"{SCREENSHOTS_DIR}/{ts}_{label}.png"
    try:
        driver.save_screenshot(p)
        log.debug(f"📸 {p}")
    except Exception as e:
        log.warning(f"Screenshot fail: {e}")
    return p

def page_info(driver, tag=""):
    log.info(f"[{tag}] title='{driver.title}' url={driver.current_url}")

def detect_block(driver):
    t = driver.title.lower()
    s = driver.page_source.lower()
    if len(s) < 200:                                      return "EMPTY_PAGE"
    if "just a moment" in t or "checking your" in s:     return "CLOUDFLARE"
    if "ray id" in s and "cloudflare" in s:               return "CLOUDFLARE_BLOCK"
    if "access denied" in t or "access denied" in s:     return "ACCESS_DENIED"
    if "403 forbidden" in s or t == "403":                return "403"
    return "none"


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────
def tg(chat_id, msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"TG error: {e}")

def tg_photo(chat_id, path, caption):
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": f}, timeout=15)
    except Exception as e:
        log.error(f"TG photo error: {e}")

def email(to, subject, body, attach=None):
    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_USER; msg["To"] = to; msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        if attach and os.path.exists(attach):
            with open(attach, "rb") as f:
                p = MIMEBase("application", "octet-stream"); p.set_payload(f.read())
            encoders.encode_base64(p)
            p.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attach)}")
            msg.attach(p)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, to, msg.as_string())
    except Exception as e:
        log.error(f"Email error: {e}")

def notify(user, subject, msg, screenshot=None):
    tg(user["telegram_id"], msg)
    email(user["email"], subject, msg, screenshot)
    tg(ADMIN_TELEGRAM_ID, f"[{user['name']}] {msg}")
    email(ADMIN_EMAIL, f"[{user['name']}] {subject}", msg, screenshot)
    if screenshot and os.path.exists(screenshot):
        tg_photo(ADMIN_TELEGRAM_ID, screenshot, f"[{user['name']}] {subject}")


# ── CAPTCHA SOLVER ────────────────────────────────────────────────────────────
def solve_captcha(driver):
    """
    From screenshot: captcha is bottom-right of homepage.
    It's inside a div with a noisy background image showing 4 digits.
    The input is labeled 'CONFIRM CODE'.
    """
    try:
        # Selectors based on what we saw in screenshot
        selectors = [
            "img[src*='captcha']", "img[src*='Captcha']",
            ".captcha img", "#captcha img",
            "img[id*='captcha']", "img[class*='captcha']",
            "img[alt*='captcha']",
            # The captcha in screenshot appears to be a div with background or an img
            ".col-md-4 img", "form img",
            # Last resort - any img near the confirm code input
        ]
        captcha_img = None
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                captcha_img = els[0]
                log.debug(f"Captcha img: {sel}")
                break

        if not captcha_img:
            # Log ALL images for diagnosis
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            log.warning(f"No captcha img found. {len(all_imgs)} images on page:")
            for i, img in enumerate(all_imgs):
                log.warning(f"  [{i}] src={str(img.get_attribute('src'))[:100]} "
                            f"id={img.get_attribute('id')} class={img.get_attribute('class')}")
            return ""

        src = captcha_img.get_attribute("src") or ""
        if not src:
            log.warning("Captcha img src is empty")
            return ""

        if src.startswith("data:image"):
            _, data = src.split(",", 1)
            img_bytes = base64.b64decode(data)
        else:
            img_bytes = requests.get(src, timeout=10).content

        # Save raw for debug
        raw = f"{SCREENSHOTS_DIR}/cap_raw_{datetime.now().strftime('%H%M%S')}.png"
        with open(raw, "wb") as f:
            f.write(img_bytes)

        # Preprocess
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
        log.info(f"OCR: '{result}' → '{digits}'")
        return digits

    except Exception as e:
        log.error(f"Captcha error: {e}")
        return ""


# ── DRIVER ────────────────────────────────────────────────────────────────────
def get_driver():
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-IE")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=opts)

def sel_dropdown(driver, wait, idx, value):
    sels = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "select")))
    s = Select(sels[idx])
    matched = False
    for opt in s.options:
        if opt.text.strip() == value:
            s.select_by_visible_text(opt.text); matched = True; break
    if not matched:
        for opt in s.options:
            if value.lower() in opt.text.lower():
                s.select_by_visible_text(opt.text); matched = True; break
    if not matched:
        log.error(f"Dropdown[{idx}] no match for '{value}'. Options: {[o.text for o in s.options]}")
    hs(1.5)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def check_for_user(user):
    driver = get_driver()
    wait   = WebDriverWait(driver, 25)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefs  = user["prefs"]

    try:
        # ── STEP 1: Load visametric.com ───────────────────────────────────────
        log.info("STEP 1 — Loading visametric.com")
        driver.get(LANDING_URL)
        hs(6)
        page_info(driver, "step1")
        ss(driver, "01_landing")

        blk = detect_block(driver)
        if blk != "none":
            s = ss(driver, "01_blocked")
            notify(user, f"🚫 Blocked: {blk}",
                   f"🚫 <b>Blocked at step 1</b>\n<code>{blk}</code>\nURL: {driver.current_url}\n{now}", s)
            return False

        # ── STEP 2: Select "Applying from" ────────────────────────────────────
        log.info("STEP 2 — Selecting countries")
        try:
            sels = driver.find_elements(By.TAG_NAME, "select")
            log.debug(f"Selects on landing: {len(sels)}")
            if sels:
                Select(sels[0]).select_by_visible_text(prefs["from_country"])
                log.info(f"From: {prefs['from_country']}")
                hs(3)
        except Exception as e:
            log.warning(f"From select: {e}")

        # ── STEP 3: Select "Going to" ─────────────────────────────────────────
        try:
            sels = driver.find_elements(By.TAG_NAME, "select")
            if len(sels) >= 2:
                Select(sels[1]).select_by_visible_text(prefs["to_country"])
                log.info(f"To: {prefs['to_country']}")
                hs(6)  # auto redirects
            else:
                log.warning(f"Only {len(sels)} selects found after from-select")
        except Exception as e:
            log.warning(f"To select: {e}")

        page_info(driver, "step3_after_redirect")
        ss(driver, "03_after_redirect")

        # ── STEP 4: Close the fingerprint popup ───────────────────────────────
        # From screenshot: popup has an X button at top right corner
        log.info("STEP 4 — Closing popup")
        hs(3)
        ss(driver, "04_before_popup_close")

        popup_closed = False
        # These selectors target the X button seen in the screenshot
        close_attempts = [
            (By.CSS_SELECTOR, "button.close"),
            (By.CSS_SELECTOR, ".close"),
            (By.CSS_SELECTOR, "[data-dismiss='modal']"),
            (By.CSS_SELECTOR, "button[aria-label='Close']"),
            (By.CSS_SELECTOR, ".modal-header .close"),
            (By.CSS_SELECTOR, ".popup .close"),
            # The × character button
            (By.XPATH, "//button[contains(text(),'×')]"),
            (By.XPATH, "//button[contains(text(),'✕')]"),
            (By.XPATH, "//button[contains(text(),'x')]"),
            (By.XPATH, "//*[contains(@class,'close')]"),
            # From screenshot the X appears to be a span/div top-right
            (By.XPATH, "//*[@id='myModal']//button[@class='close']"),
            (By.XPATH, "//div[contains(@class,'modal')]//button"),
        ]

        for by, sel in close_attempts:
            try:
                els = driver.find_elements(by, sel)
                for el in els:
                    if el.is_displayed():
                        el.click()
                        log.info(f"✅ Popup closed via: {sel}")
                        popup_closed = True
                        hs(2)
                        break
                if popup_closed:
                    break
            except Exception:
                continue

        if not popup_closed:
            log.warning("Could not find close button — trying ESC key")
            from selenium.webdriver.common.keys import Keys
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            hs(1)

        ss(driver, "04_after_popup_close")
        page_info(driver, "step4_popup_closed")

        # Save page HTML for analysis
        html_path = f"{SCREENSHOTS_DIR}/page_after_popup.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)

        # ── STEP 5: Solve captcha on homepage ─────────────────────────────────
        # From screenshot: captcha (1722) is bottom-right with "CONFIRM CODE" input
        log.info("STEP 5 — Solving captcha on homepage")

        # Log all inputs for diagnosis
        inputs = driver.find_elements(By.TAG_NAME, "input")
        log.debug(f"Inputs on page ({len(inputs)}):")
        for inp in inputs:
            log.debug(f"  type={inp.get_attribute('type')} name={inp.get_attribute('name')} "
                      f"id={inp.get_attribute('id')} placeholder={inp.get_attribute('placeholder')}")

        solved = False
        for attempt in range(MAX_CAPTCHA_RETRIES):
            log.info(f"--- Captcha attempt {attempt+1}/{MAX_CAPTCHA_RETRIES} ---")
            digits = solve_captcha(driver)
            log.info(f"Digits: '{digits}'")

            if len(digits) != 4:
                log.warning(f"Bad OCR '{digits}', refreshing captcha")
                # Try refreshing just the captcha image if there's a refresh link
                refresh_els = driver.find_elements(By.XPATH,
                    "//*[contains(@onclick,'captcha') or contains(@href,'captcha')]")
                if refresh_els:
                    refresh_els[0].click()
                    hs(1)
                else:
                    driver.refresh()
                    hs(4)
                    # Re-close popup after refresh
                    for by, sel in close_attempts:
                        try:
                            els = driver.find_elements(by, sel)
                            for el in els:
                                if el.is_displayed():
                                    el.click(); hs(1); break
                        except Exception:
                            continue
                continue

            # Find "CONFIRM CODE" input field
            captcha_input = None
            confirm_selectors = [
                "input[placeholder='CONFIRM CODE']",
                "input[placeholder*='CONFIRM']",
                "input[placeholder*='confirm']",
                "input[name*='captcha']",
                "input[id*='captcha']",
                "input[name*='code']",
                "input[id*='code']",
                "#confirmCode",
            ]
            for sel in confirm_selectors:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    captcha_input = els[0]
                    log.debug(f"Captcha input: {sel}")
                    break

            if not captcha_input:
                # Get all text inputs as fallback
                text_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])")
                log.warning(f"No confirm input found. Text inputs: {len(text_inputs)}")
                for ti in text_inputs:
                    log.warning(f"  {ti.get_attribute('outerHTML')[:150]}")
                ss(driver, f"no_captcha_input_{attempt+1}")
                break

            captcha_input.clear()
            hs(0.3)
            captcha_input.send_keys(digits)
            hs(0.5)
            ss(driver, f"captcha_filled_{attempt+1}")

            # Click "Book an Appointment" button (the green one visible in screenshot)
            book_btn = None
            book_xpaths = [
                "//a[contains(text(),'Book an Appointment')]",
                "//button[contains(text(),'Book an Appointment')]",
                "//a[contains(@class,'btn') and contains(text(),'Book')]",
                "//button[contains(@class,'btn') and contains(text(),'Book')]",
                "//input[@value='Book an Appointment']",
            ]
            for xpath in book_xpaths:
                els = driver.find_elements(By.XPATH, xpath)
                if els:
                    book_btn = els[0]
                    log.debug(f"Book btn: {xpath}")
                    break

            if not book_btn:
                log.error("Book an Appointment button not found!")
                # Log all buttons and links
                btns = driver.find_elements(By.TAG_NAME, "button")
                links = driver.find_elements(By.TAG_NAME, "a")
                log.error(f"Buttons: {[b.text[:40] for b in btns]}")
                log.error(f"Links: {[l.text[:40] for l in links if l.text.strip()][:20]}")
                ss(driver, f"no_book_btn_{attempt+1}")
                break

            book_btn.click()
            hs(6)
            ss(driver, f"after_book_click_{attempt+1}")
            page_info(driver, f"step5_after_book_{attempt+1}")

            blk = detect_block(driver)
            if blk != "none":
                s = ss(driver, f"blocked_{attempt+1}")
                notify(user, f"🚫 Blocked: {blk}",
                       f"🚫 Blocked after book click\n<code>{blk}</code>\n{driver.current_url}\n{now}", s)
                return False

            # Check if we reached the appointment form (dropdowns)
            if len(driver.find_elements(By.TAG_NAME, "select")) > 0:
                solved = True
                log.info(f"✅ Captcha solved on attempt {attempt+1}")
                break

            # If we went to ie-appointment URL with captcha, handle that too
            if "ie-appointment" in driver.current_url:
                log.info("Redirected to ie-appointment page — handling captcha there")
                hs(3)
                # Solve captcha on this page too
                ie_solved = False
                for ie_attempt in range(MAX_CAPTCHA_RETRIES):
                    ie_digits = solve_captcha(driver)
                    if len(ie_digits) != 4:
                        driver.refresh(); hs(3); continue

                    ie_input = None
                    for sel in ["input[placeholder='VERIFICATION CODE']",
                                "input[placeholder*='erification']", "input[type='text']"]:
                        els = driver.find_elements(By.CSS_SELECTOR, sel)
                        if els: ie_input = els[0]; break

                    if not ie_input: break

                    ie_input.clear(); ie_input.send_keys(ie_digits); hs(0.5)

                    ie_btn = None
                    for xpath in ["//button[contains(text(),'Get your appointment')]",
                                  "//a[contains(text(),'Get your appointment')]",
                                  "//button[contains(@class,'btn-danger')]"]:
                        els = driver.find_elements(By.XPATH, xpath)
                        if els: ie_btn = els[0]; break

                    if not ie_btn: break
                    ie_btn.click(); hs(5)

                    if len(driver.find_elements(By.TAG_NAME, "select")) > 0:
                        ie_solved = True; break
                    driver.refresh(); hs(3)

                solved = ie_solved
                if solved:
                    log.info("✅ ie-appointment captcha solved")
                break

            log.warning(f"Attempt {attempt+1} failed, retrying")
            driver.refresh()
            hs(4)
            for by, sel in close_attempts:
                try:
                    els = driver.find_elements(by, sel)
                    for el in els:
                        if el.is_displayed(): el.click(); hs(1); break
                except Exception:
                    continue

        if not solved:
            s = ss(driver, "all_failed")
            log.error(f"Title: {driver.title} | URL: {driver.current_url}")
            notify(user, "⚠️ Captcha Failed",
                   f"⚠️ <b>All captcha attempts failed</b>\n👤 {user['name']}\n🕒 {now}\n"
                   f"📄 {driver.title}\n🌐 {driver.current_url}", s)
            return False

        # ── STEP 6: Fill form ─────────────────────────────────────────────────
        log.info("STEP 6 — Filling form")
        hs(2)

        all_sels = driver.find_elements(By.TAG_NAME, "select")
        log.debug(f"Form dropdowns ({len(all_sels)}):")
        for i, s_ in enumerate(all_sels):
            opts = [o.text for o in Select(s_).options]
            log.debug(f"  [{i}]: {opts}")

        sel_dropdown(driver, wait, 0, prefs["application_type"])
        sel_dropdown(driver, wait, 1, prefs["from_country"])
        sel_dropdown(driver, wait, 2, prefs["city"])
        sel_dropdown(driver, wait, 3, prefs["office"])
        sel_dropdown(driver, wait, 4, prefs["service_type"])
        sel_dropdown(driver, wait, 5, prefs["applicants"])
        ss(driver, "06_form_filled")

        # ── STEP 7: NEXT ──────────────────────────────────────────────────────
        log.info("STEP 7 — NEXT")
        nxt = driver.find_element(By.XPATH,
            "//button[contains(text(),'NEXT')] | //a[contains(text(),'NEXT')] | "
            "//button[contains(@class,'btn-success')] | //input[@value='NEXT']")
        nxt.click()
        hs(6)
        ss(driver, "07_after_next")
        page_info(driver, "step7")

        # ── STEP 8: Check dates ───────────────────────────────────────────────
        log.info("STEP 8 — Checking dates")
        dates = re.findall(r'\d{2}-\d{2}-\d{4}', driver.page_source)
        log.info(f"Dates: {dates}")

        if dates:
            dates_str = ", ".join(set(dates))
            s = ss(driver, "08_slot_found")
            notify(user, "✅ Slot Available!",
                   f"🟢 <b>SLOT FOUND!</b>\n\n👤 {user['name']}\n📅 <b>{dates_str}</b>\n"
                   f"🕒 {now}\n\n👉 {APPOINTMENT_URL}", s)
            return True
        else:
            ss(driver, "08_no_slot")
            notify(user, "❌ No Slot",
                   f"🔴 No slot\n👤 {user['name']}\n🕒 {now}\nNext ~1hr.")
            return False

    except Exception as e:
        log.exception(f"Error: {e}")
        s = ss(driver, "error")
        notify(user, "Bot Error", f"⚠️ <code>{e}</code>\n{now}", s)
        return False
    finally:
        driver.quit()


if __name__ == "__main__":
    log.info(f"=== Started {datetime.now()} ===")
    for user in USERS:
        log.info(f"--- {user['name']} ---")
        check_for_user(user)
    log.info("=== Done ===")