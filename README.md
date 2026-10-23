# iraq-tender-bot
Iraq Government Tender Alert

import os
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

BASE_URL = "https://itp.iq"
TENDERS_URL = (
    "https://itp.iq/BUSINESS/tenders"
    "?count=100&page=0&statuses=all"
)
STATE_FILE = Path("seen_tenders.json")
BAGHDAD = ZoneInfo("Asia/Baghdad")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def get_page(url):
    response = requests.get(
        url,
        timeout=40,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()
    return response.text


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def clean(value):
    return " ".join(value.split())


def read_tenders():
    html = get_page(TENDERS_URL)
    soup = BeautifulSoup(html, "html.parser")
    tenders = {}

    for row in soup.select("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        link = row.select_one('a[href*="/BUSINESS/tenders/"]')
        if not link:
            continue

        href = urljoin(BASE_URL, link.get("href", ""))
        values = [clean(cell.get_text(" ", strip=True))
                  for cell in cells]

        tender = {
            "number": values[0],
            "title": values[1],
            "entity": values[2],
            "location": values[3],
            "published": values[4],
            "deadline": values[5],
            "status": values[7] if len(values) > 7 else "",
            "url": href,
        }

        key = href.rstrip("/").split("/")[-1]
        tenders[key] = tender

    return tenders


def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing"
        )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # Telegram messages have a length limit.
    chunks = []
    while len(message) > 3500:
        split_at = message.rfind("\n", 0, 3500)
        if split_at < 1000:
            split_at = 3500
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip()

    chunks.append(message)

    for chunk in chunks:
        response = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram rejected the message")


def main():
    now = datetime.now(BAGHDAD)
    state = load_state()
    current = read_tenders()

    if not current:
        raise RuntimeError(
            "No tenders were parsed. Website layout may have changed "
            "or the listing may require JavaScript."
        )

    new_state = {}
    updates = []

    for key, tender in current.items():
        previous = state.get(key)

        if previous is None:
            updates.append(("🆕 مناقصه جدید", tender))
        elif previous.get("deadline") != tender["deadline"]:
            updates.append(("⚠️ تغییر مهلت مناقصه", tender))
        elif previous.get("status") != tender["status"]:
            updates.append(("🔔 تغییر وضعیت مناقصه", tender))

        new_state[key] = tender

    if not state:
        heading = "گزارش اولیه مناقصات دولتی عراق"
        # On the first run, list the available tenders as a baseline.
        updates = [("📌 مناقصه موجود در پورتال", t)
                   for t in current.values()]
    else:
        heading = "گزارش روزانه مناقصات دولتی عراق"

    lines = [
        heading,
        f"تاریخ و ساعت بغداد: {now:%Y-%m-%d %H:%M}",
        f"تعداد آگهی‌های قابل‌خواندن: {len(current)}",
        f"موارد جدید یا تغییرکرده: {len(updates)}",
        "",
    ]

    if not updates:
        lines.append("امروز مورد جدید یا تغییر قابل‌تشخیصی پیدا نشد.")
    else:
        for label, t in updates:
            lines.extend([
                label,
                f"عنوان: {t['title']}",
                f"سازمان: {t['entity']}",
                f"شماره: {t['number']}",
                f"تاریخ انتشار: {t['published']}",
                f"مهلت: {t['deadline']}",
                f"وضعیت: {t['status']}",
                f"محل تحویل: {t['location']}",
                f"لینک رسمی: {t['url']}",
                "",
            ])

    lines.extend([
        "منبع: پورتال رسمی ITP عراق",
        "توجه: این نسخه فقط آگهی‌های قابل‌خواندن از فهرست را بررسی می‌کند؛ "
        "پوشش همه وزارتخانه‌ها و پیوست‌های مناقصه هنوز تضمین‌شده نیست.",
    ])

    telegram_send("\n".join(lines))
    save_state(new_state)
    print(f"Sent report. Read {len(current)} tenders.")


if __name__ == "__main__":
    main()
