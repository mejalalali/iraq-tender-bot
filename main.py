
import os
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

BASE_URL = "https://itp.iq"
TENDERS_URL = "https://itp.iq/BUSINESS/tenders?count=100&page=0&statuses=all"
STATE_FILE = Path("seen_tenders.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing. Check GitHub Actions secrets."
        )

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def main():
    now = datetime.now(ZoneInfo("Asia/Baghdad"))
    response = requests.get(
        TENDERS_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=40,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    tenders = {}

    for row in soup.select("tr"):
        cells = row.find_all("td")
        link = row.select_one('a[href*="/BUSINESS/tenders/"]')

        if len(cells) < 2 or not link:
            continue

        values = [
            " ".join(cell.get_text(" ", strip=True).split())
            for cell in cells
        ]
        url = urljoin(BASE_URL, link.get("href", ""))
        key = url.rstrip("/").split("/")[-1]

        tenders[key] = {
            "number": values[0],
            "title": values[1],
            "entity": values[2] if len(values) > 2 else "نامشخص",
            "published": values[4] if len(values) > 4 else "نامشخص",
            "deadline": values[5] if len(values) > 5 else "نامشخص",
            "url": url,
        }

    if not tenders:
        raise RuntimeError(
            "No tenders parsed from ITP. The page may require JavaScript "
            "or its layout may have changed. No report was sent."
        )

    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    changes = []
    for key, tender in tenders.items():
        old = previous.get(key)
        if old is None:
            changes.append(("🆕 مناقصه جدید", tender))
        elif old.get("deadline") != tender["deadline"]:
            changes.append(("⚠️ تغییر مهلت", tender))

    lines = [
        "گزارش مناقصات دولتی عراق",
        f"زمان بغداد: {now:%Y-%m-%d %H:%M}",
        f"تعداد آگهی‌های قابل‌خواندن: {len(tenders)}",
        f"موارد جدید یا تغییر مهلت: {len(changes)}",
        "",
    ]

    if not previous:
        lines.append("این گزارش اولیه است؛ موارد زیر برای ثبت سابقه بررسی شده‌اند.")
        changes = [("📌 آگهی موجود", t) for t in tenders.values()]

    if not changes:
        lines.append("مورد جدید یا تغییر مهلت قابل‌تشخیصی پیدا نشد.")

    for label, tender in changes[:20]:
        lines.extend([
            label,
            f"عنوان: {tender['title']}",
            f"سازمان: {tender['entity']}",
            f"شماره: {tender['number']}",
            f"تاریخ انتشار: {tender['published']}",
            f"مهلت: {tender['deadline']}",
            f"لینک: {tender['url']}",
            "",
        ])

    lines.append(
        "منبع: ITP عراق. پوشش همه وزارتخانه‌ها تضمین نشده است."
    )

    send_telegram("\n".join(lines))
    STATE_FILE.write_text(
        json.dumps(tenders, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Report sent. Parsed {len(tenders)} tenders.")


if __name__ == "__main__":
    main()
