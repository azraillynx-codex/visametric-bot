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
from selenium.webdriver.common.keys import Keys
import undetected_chromedriver as uc

logging.basicConfig(level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("visametric")

GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ADMIN_EMAIL        = os.environ["ADMIN_EMAIL"]
ADMIN_TELEGRAM_ID  = os.environ["ADMIN_TELEGRAM_CHAT_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

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

LANDING_URL     = "https://www.visametric.com"
APPOINTMENT_URL = "https://ie-appointment.visametric.com/en"
MAX_CAPTCHA_RETRIES = 5
SCREENSHOTS_DIR = "debug_screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

def hs(s=2):
    import random; time.sleep(s + random.uniform(0.2, 0.7))

def ss(driver, label):
    ts = datetime.now().strftime("%H%M%S")
    p = f"{SCREENSHOTS_DIR}/{ts}_{label}.png"
    try: driver.save_screenshot(p); log.debug(f"📸 {p}")
    except Exception as e: log.warning(f"ss fail: {e}")
    return p

def pi(driver, tag=""):
    log.info(f"[{tag}] title='{driver.title}' url={driver.current_url}")

def detect_block(driver):
    t = driver.title.lower(); s = driver.page_source.lower()
    url = driver.current_url.lower()
    # Never flag visametric pages as blocked
    if "visametric.com" in url:                        return "none"
    if len(s) < 200:                                   return "EMPTY_PAGE"
    if "just a moment" in t or "checking your" in s:  return "CLOUDFLARE"
    if "ray id" in s and "cloudflare" in s:            return "CLOUDFLARE_BLOCK"
    if "access denied" in t:                           return "ACCESS_DENIED"
    return "none"

def tg(chat_id, msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: log.error(f"TG: {e}")

def tg_photo(chat_id, path, caption):
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": f}, timeout=15)
    except Exception as e: log.error(f"TG photo: {e}")

def mail(to, subject, body, attach=None):
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
    except Exception as e: log.error(f"Mail: {e}")

def notify(user, subject, msg, screenshot=None):
    tg(user["telegram_id"], msg); mail(user["email"], subject, msg, screenshot)
    tg(ADMIN_TELEGRAM_ID, f"[{user['name']}] {msg}")
    mail(ADMIN_EMAIL, f"[{user['name']}] {subject}", msg, screenshot)
    if screenshot and os.path.exists(screenshot):
        tg_photo(ADMIN_TELEGRAM_ID, screenshot, f"[{user['name']}] {subject}")

def solve_captcha_img(driver):
    """Solve the 4-digit image captcha on ie-appointment page."""
    try:
        selectors = [
            "img[src*='captcha']", "img[src*='Captcha']",
            ".captcha img", "#captcha img",
            "img[id*='captcha']", "img[class*='captcha']",
        ]
        captcha_img = None
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els: captcha_img = els[0]; log.debug(f"Captcha img: {sel}"); break

        if not captcha_img:
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            log.warning(f"No captcha img. All imgs ({len(all_imgs)}):")
            for i, img in enumerate(all_imgs):
                log.warning(f"  [{i}] src={str(img.get_attribute('src'))[:100]} "
                            f"id={img.get_attribute('id')} class={img.get_attribute('class')}")
            return ""

        src = captcha_img.get_attribute("src") or ""
        if not src: return ""

        if src.startswith("data:image"):
            _, data = src.split(",", 1); img_bytes = base64.b64decode(data)
        else:
            img_bytes = requests.get(src, timeout=10).content

        raw = f"{SCREENSHOTS_DIR}/cap_raw_{datetime.now().strftime('%H%M%S')}.png"
        with open(raw, "wb") as f: f.write(img_bytes)

        nparr = np.frombuffer(img_bytes, np.uint8)
        img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
        den   = cv2.fastNlMeansDenoising(thr, h=30)
        scaled = cv2.resize(den, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        result = pytesseract.image_to_string(
            Image.fromarray(scaled),
            config="--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        ).strip()
        digits = "".join(filter(str.isdigit, result))
        log.info(f"OCR: '{result}' → '{digits}'")
        return digits
    except Exception as e:
        log.error(f"Captcha OCR error: {e}"); return ""

def get_driver():
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-IE")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    return uc.Chrome(options=opts)

def sel_drop(driver, wait, idx, value):
    sels = wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "select")))
    s = Select(sels[idx])
    matched = False
    for opt in s.options:
        if opt.text.strip() == value: s.select_by_visible_text(opt.text); matched=True; break
    if not matched:
        for opt in s.options:
            if value.lower() in opt.text.lower(): s.select_by_visible_text(opt.text); matched=True; break
    if not matched:
        log.error(f"Dropdown[{idx}] no match '{value}'. Options: {[o.text for o in s.options]}")
    hs(1.5)

def check_for_user(user):
    driver = get_driver()
    wait   = WebDriverWait(driver, 25)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefs  = user["prefs"]

    try:
        # ── STEP 1: visametric.com ────────────────────────────────────────────
        log.info("STEP 1 — visametric.com")
        driver.get(LANDING_URL)
        hs(6)
        pi(driver, "step1")
        ss(driver, "01_landing")

        if detect_block(driver) != "none":
            s = ss(driver, "blocked")
            notify(user, "🚫 Blocked", f"🚫 Blocked at landing\n{driver.current_url}\n{now}", s)
            return False

        # ── STEP 2: Select Ireland ────────────────────────────────────────────
        log.info("STEP 2 — Select Ireland")
        try:
            sels = driver.find_elements(By.TAG_NAME, "select")
            log.debug(f"Selects: {len(sels)}")
            if sels:
                Select(sels[0]).select_by_visible_text(prefs["from_country"])
                log.info(f"✅ From: {prefs['from_country']}")
                hs(3)
        except Exception as e: log.warning(f"From select: {e}")

        # ── STEP 3: Select Germany → auto redirect ────────────────────────────
        log.info("STEP 3 — Select Germany")
        try:
            sels = driver.find_elements(By.TAG_NAME, "select")
            if len(sels) >= 2:
                Select(sels[1]).select_by_visible_text(prefs["to_country"])
                log.info(f"✅ To: {prefs['to_country']}")
                hs(7)
            else:
                log.warning(f"Only {len(sels)} selects, expected 2")
        except Exception as e: log.warning(f"To select: {e}")

        pi(driver, "step3"); ss(driver, "03_redirected")

        # ── STEP 4: Close fingerprint popup ───────────────────────────────────
        log.info("STEP 4 — Close popup")
        hs(3)
        ss(driver, "04_before_close")

        closed = False
        for by, sel in [
            (By.CSS_SELECTOR, "button.close"),
            (By.CSS_SELECTOR, ".close"),
            (By.CSS_SELECTOR, "[data-dismiss='modal']"),
            (By.XPATH, "//button[normalize-space()='×']"),
            (By.XPATH, "//button[contains(@class,'close')]"),
            (By.XPATH, "//div[contains(@class,'modal')]//button"),
            (By.XPATH, "//*[@id='myModal']//button"),
        ]:
            try:
                els = driver.find_elements(by, sel)
                for el in els:
                    if el.is_displayed():
                        el.click(); log.info(f"✅ Popup closed: {sel}"); closed=True; hs(2); break
                if closed: break
            except: continue

        if not closed:
            log.warning("No close btn — trying ESC")
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            hs(2)

        ss(driver, "04_popup_closed")

        # ── STEP 5: Go to Book an Appointment page via working URL ───────────────
        # The working URL we confirmed: /Ireland/Germany/en/p/book-an-appointment-national-visa
        # From that page, click "Schengen Visa" in the left sidebar
        # OR directly go to the Schengen page via sidebar link
        log.info("STEP 5 — Navigating to Book an Appointment info page")
        hs(2)

        working_url = "https://www.visametric.com/Ireland/Germany/en/p/book-an-appointment-national-visa"
        driver.get(working_url)
        hs(5)
        pi(driver, "step5_book_page"); ss(driver, "05_book_page")

        # From the screenshot we saw: left sidebar has "Schengen Visa" link
        # Click that to get to the Schengen booking page with the green button
        log.info("STEP 5b — Clicking 'Schengen Visa' in sidebar")
        schengen_clicked = False
        for xpath in [
            "//a[normalize-space()='Schengen Visa']",
            "//a[contains(text(),'Schengen Visa')]",
            "//li/a[contains(text(),'Schengen')]",
            "//ul//a[contains(text(),'Schengen')]",
        ]:
            els = driver.find_elements(By.XPATH, xpath)
            for el in els:
                if el.is_displayed():
                    log.info(f"Schengen link: {el.get_attribute('href')}")
                    el.click(); schengen_clicked = True; hs(5); break
            if schengen_clicked: break

        if not schengen_clicked:
            log.warning("Schengen sidebar link not found — staying on current page")

        pi(driver, "step5b_schengen"); ss(driver, "05b_schengen_page")

        # ── STEP 6: Click the GREEN "Book an Appointment" button ──────────────
        # After clicking the section, a new page loads with info text
        # and a prominent GREEN button "Book an Appointment" — click that
        log.info("STEP 6 — Clicking green 'Book an Appointment' button")
        hs(3)
        ss(driver, "06_before_green_btn")

        green_clicked = False
        green_xpaths = [
            # Green/teal button — exact text match first
            "//a[normalize-space()='Book an Appointment']",
            "//button[normalize-space()='Book an Appointment']",
            # Button with btn class (Bootstrap green = btn-success, teal = btn-info etc)
            "//a[contains(@class,'btn') and contains(text(),'Book an Appointment')]",
            "//button[contains(@class,'btn') and contains(text(),'Book')]",
            "//a[contains(@class,'btn-success')]",
            "//a[contains(@class,'btn-info')]",
            "//a[contains(@class,'btn-primary') and contains(text(),'Book')]",
            # Fallback — any Book an Appointment link/button
            "//a[contains(text(),'Book an Appointment')]",
            "//button[contains(text(),'Book an Appointment')]",
        ]
        for xpath in green_xpaths:
            try:
                els = driver.find_elements(By.XPATH, xpath)
                for el in els:
                    if el.is_displayed():
                        log.info(f"✅ Green btn: {xpath} text='{el.text[:50]}'")
                        el.click(); green_clicked = True; hs(6); break
                if green_clicked: break
            except Exception as e:
                log.debug(f"Green btn {xpath}: {e}")

        if not green_clicked:
            log.warning("Green button not found — may have gone directly to captcha page")
            # Log all buttons/links for diagnosis
            links = driver.find_elements(By.TAG_NAME, "a")
            btns  = driver.find_elements(By.TAG_NAME, "button")
            log.warning(f"Links: {[l.text[:40] for l in links if l.text.strip()][:15]}")
            log.warning(f"Buttons: {[b.text[:40] for b in btns if b.text.strip()]}")
            ss(driver, "06_no_green_btn")

        pi(driver, "step6"); ss(driver, "06_after_green_btn")

        # ── STEP 7: Now on ie-appointment captcha page ────────────────────────
        if "ie-appointment.visametric.com" not in driver.current_url:
            log.info(f"Not on ie-appointment yet ({driver.current_url}), navigating directly")
            driver.get(APPOINTMENT_URL)
            hs(6)

        pi(driver, "step7_captcha_page"); ss(driver, "07_captcha_page")

        # Log inputs for diagnosis
        inputs = driver.find_elements(By.TAG_NAME, "input")
        log.debug(f"Inputs ({len(inputs)}):")
        for inp in inputs:
            log.debug(f"  type={inp.get_attribute('type')} name={inp.get_attribute('name')} "
                      f"id={inp.get_attribute('id')} placeholder={inp.get_attribute('placeholder')}")

        # Save HTML
        with open(f"{SCREENSHOTS_DIR}/captcha_page.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)

        # ── STEP 7: Solve captcha + submit ────────────────────────────────────
        log.info("STEP 7 — Solving captcha")
        solved = False
        for attempt in range(MAX_CAPTCHA_RETRIES):
            log.info(f"--- Captcha attempt {attempt+1}/{MAX_CAPTCHA_RETRIES} ---")
            digits = solve_captcha_img(driver)

            if len(digits) != 4:
                log.warning(f"Bad OCR '{digits}', refreshing")
                ss(driver, f"cap_bad_{attempt+1}")
                driver.refresh(); hs(4); continue

            # Find VERIFICATION CODE input
            cap_input = None
            for sel in [
                "input[placeholder='VERIFICATION CODE']",
                "input[placeholder*='VERIFICATION']",
                "input[placeholder*='erification']",
                "input[name*='captcha']", "input[id*='captcha']",
                "input[type='text']",
            ]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els: cap_input = els[0]; log.debug(f"Cap input: {sel}"); break

            if not cap_input:
                log.error("VERIFICATION CODE input not found!")
                ss(driver, f"no_input_{attempt+1}"); break

            cap_input.clear(); hs(0.3)
            cap_input.send_keys(digits); hs(0.5)

            # Click "Get your appointment"
            btn = None
            for xpath in [
                "//button[contains(text(),'Get your appointment')]",
                "//a[contains(text(),'Get your appointment')]",
                "//button[contains(@class,'btn-danger')]",
                "//button[contains(@class,'btn-primary')]",
                "//input[@type='submit']",
            ]:
                els = driver.find_elements(By.XPATH, xpath)
                if els: btn = els[0]; log.debug(f"Submit btn: {xpath}"); break

            if not btn:
                log.error("Get your appointment button not found!")
                btns = driver.find_elements(By.TAG_NAME, "button")
                log.error(f"Buttons: {[b.text for b in btns]}")
                ss(driver, f"no_btn_{attempt+1}"); break

            btn.click(); hs(6)
            ss(driver, f"cap_after_{attempt+1}"); pi(driver, f"cap_{attempt+1}")

            if detect_block(driver) != "none":
                s = ss(driver, "blocked_after_cap")
                notify(user, "🚫 Blocked", f"🚫 Blocked after captcha\n{driver.current_url}\n{now}", s)
                return False

            if len(driver.find_elements(By.TAG_NAME, "select")) > 0:
                solved = True
                log.info(f"✅ Captcha solved attempt {attempt+1}")
                break

            log.warning(f"Attempt {attempt+1} failed, retry")
            driver.refresh(); hs(4)

        if not solved:
            s = ss(driver, "all_failed")
            notify(user, "⚠️ Captcha Failed",
                f"⚠️ <b>Captcha failed {MAX_CAPTCHA_RETRIES}x</b>\n"
                f"👤 {user['name']}\n🕒 {now}\n📄 {driver.title}\n🌐 {driver.current_url}", s)
            return False

        # ── STEP 8: Fill form ─────────────────────────────────────────────────
        log.info("STEP 8 — Filling form")
        hs(2)
        all_sels = driver.find_elements(By.TAG_NAME, "select")
        log.debug(f"Form has {len(all_sels)} dropdowns:")
        for i, s_ in enumerate(all_sels):
            log.debug(f"  [{i}]: {[o.text for o in Select(s_).options]}")

        sel_drop(driver, wait, 0, prefs["application_type"])
        sel_drop(driver, wait, 1, prefs["from_country"])
        sel_drop(driver, wait, 2, prefs["city"])
        sel_drop(driver, wait, 3, prefs["office"])
        sel_drop(driver, wait, 4, prefs["service_type"])
        sel_drop(driver, wait, 5, prefs["applicants"])
        ss(driver, "08_form_filled")

        # ── STEP 9: NEXT ──────────────────────────────────────────────────────
        log.info("STEP 9 — NEXT")
        nxt = driver.find_element(By.XPATH,
            "//button[contains(text(),'NEXT')] | //a[contains(text(),'NEXT')] | "
            "//input[@value='NEXT'] | //button[contains(@class,'btn-success')]")
        nxt.click(); hs(6)
        ss(driver, "09_after_next"); pi(driver, "step9")

        # ── STEP 10: Check dates ──────────────────────────────────────────────
        log.info("STEP 10 — Checking dates")
        dates = re.findall(r'\d{2}-\d{2}-\d{4}', driver.page_source)
        log.info(f"Dates: {dates}")

        if dates:
            dates_str = ", ".join(set(dates))
            s = ss(driver, "10_slot_found")
            notify(user, "✅ Slot Available!",
                f"🟢 <b>SLOT FOUND!</b>\n\n👤 {user['name']}\n📅 <b>{dates_str}</b>\n"
                f"🕒 {now}\n\n👉 {APPOINTMENT_URL}", s)
            return True
        else:
            ss(driver, "10_no_slot")
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