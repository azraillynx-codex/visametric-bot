# VisaMetric Slot Checker Bot

Automated appointment slot checker for ie-appointment.visametric.com
Runs every hour via GitHub Actions. Notifies via Telegram + Email.

---

## STEP 1 — Create GitHub Repository

1. Go to https://github.com/new
2. Name it: `visametric-bot`
3. Set to **Private** (important — keeps your code safe)
4. Click **Create repository**

---

## STEP 2 — Upload the files

Upload these files to your repo in this structure:

```
visametric-bot/
├── .github/
│   └── workflows/
│       └── checker.yml
├── src/
│   └── checker.py
├── requirements.txt
└── README.md
```

You can do this by:
- Click **Add file → Upload files** on GitHub
- Or use Git on your laptop:

```bash
git clone https://github.com/YOUR_USERNAME/visametric-bot
cd visametric-bot
# paste all files here
git add .
git commit -m "initial"
git push
```

---

## STEP 3 — Add Secrets to GitHub

**Never put passwords in the code!**

1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** for each:

| Secret Name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password (NOT your real password) |
| `ADMIN_EMAIL` | admin alert email |
| `ADMIN_TELEGRAM_CHAT_ID` | your Telegram chat ID |
| `TELEGRAM_BOT_TOKEN` | your bot token from @BotFather |

---

## STEP 4 — Add Your Users

Open `src/checker.py` and edit the `USERS` list:

```python
USERS = [
    {
        "name": "Jimson",
        "email": "jimson@example.com",
        "telegram_id": "111111111",   # get from @userinfobot on Telegram
        "prefs": {
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "country": "Ireland",
            "city": "Dublin",
            "office": "Dublin",
            "service_type": "NORMAL",
            "applicants": "1 applicant",
        }
    },
    {
        "name": "Friend2",
        "email": "friend2@example.com",
        "telegram_id": "222222222",
        "prefs": {
            "application_type": "Schengen - Tourism/Family&Friend Visit",
            "country": "Ireland",
            "city": "Dublin",
            "office": "Dublin",
            "service_type": "NORMAL",
            "applicants": "1 applicant",
        }
    },
    # Add up to 5 friends same way
]
```

To get a user's Telegram ID:
- Ask them to message @userinfobot on Telegram
- It replies with their chat ID

---

## STEP 5 — Enable GitHub Actions

1. Go to your repo → **Actions** tab
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. You'll see **VisaMetric Slot Checker** in the list

---

## STEP 6 — Test it manually

1. Go to **Actions** → **VisaMetric Slot Checker**
2. Click **Run workflow** → **Run workflow**
3. Watch the logs — you should see it running
4. Check your Telegram for a message

---

## STEP 7 — It runs automatically

The bot now runs **every hour automatically** via the cron schedule.

Each run:
- Opens VisaMetric site
- Solves the captcha with OCR
- Fills the form with your preferences
- Checks for available dates
- Sends Telegram + Email to each user AND admin

---

## Telegram Notifications

**Slot found:**
```
🟢 SLOT FOUND!
👤 User: Jimson
📅 Available date(s): 15-04-2026
🕒 Checked at: 2026-04-16 10:00
👉 Book now: https://ie-appointment.visametric.com/en
```

**No slot:**
```
🔴 No slot found
👤 User: Jimson
🕒 Checked at: 2026-04-16 10:00
ℹ️ Next check in ~1 hour.
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Captcha keeps failing | The site may have updated the captcha — open an issue |
| Workflow not running | Check Actions tab is enabled |
| No Telegram message | Verify bot token and chat ID are correct |
| Gmail not sending | Make sure you used App Password, not real password |

---

## GitHub Actions Free Limits

- Free: 2,000 minutes/month for private repos
- Each run ≈ 2-3 minutes
- 24 runs/day × 30 days = 720 runs × 3 min = **2,160 min/month**
- Slightly over limit for private repos
- **Fix: Make repo public** (2,000 min/month unlimited for public)
- Or reduce to every 2 hours: change `"0 * * * *"` to `"0 */2 * * *"`
