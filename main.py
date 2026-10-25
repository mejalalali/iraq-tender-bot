```python
import os
import json
import hashlib
import requests

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

BASE_URL = "https://itp.iq"
TENDERS_URL = (
    "https://itp.iq/BUSINESS/tenders"
    "?count=100&page=0&statuses=all"
)

STATE_FILE = Path("seen_tenders.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ACTION_WORDS = {
    "ابدء اهتمام",
    "ابدأ اهتمام",
    "اهتمام",
    "التفاصيل",
    "عرض التفاصيل",
    "تفاصيل",
    "المزيد",
    "عرض",
}

STATUS_TRANSLATIONS = {
    "active": "فعال",
    "open": "فعال",
    "published": "منتشر / معلن",
    "new": "جدید",
    "closed": "بسته‌شده",
    "expired": "منقضی‌شده",
    "cancelled": "لغوشده",
    "canceled": "لغوشده",
    "completed": "تکمیل‌شده",
    "under review": "در حال بررسی",
    "pending": "در انتظار",
    "فعال": "فعال",
    "منتهية": "منقضی‌شده",
    "منتهي": "منقضی‌شده",
    "ملغاة": "لغوشده",
    "ملغي": "لغوشده",
    "مغلقة": "بسته‌شده",
    "مفتوحة": "فعال",
    "قيد المراجعة": "در حال بررسی",
    "جديد": "جدید",
}


def clean(value):
    return " ".join((value or "").split()).strip()


def translate_status(status):
    status = clean(status)
    return STATUS_TRANSLATIONS.get(
        status.lower(),
        STATUS_TRANSLATIONS.get(status, status or "نامشخص"),
    )


def translate_arabic_to_persian(text):
    """Translate Arabic text to Persian; fall back to original on failure."""
    text = clean(text)
    if not text:
        return "نامشخص"

    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "ar",
                "tl": "fa",
                "dt": "t",
                "q": text,
            },
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()

        translated = "".join(
            part[0]
            for part in data[0]
            if part and part[0]
        )
        return clean(translated) or text

    except Exception as exc:
        print(f"Translation unavailable: {exc}")
        return text


def stable_key(tender):
    identity = "|".join([
        tender.get("number", ""),
        tender.get("title", ""),
        tender.get("entity", ""),
        tender.get("published", ""),
    ])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def send_telegram_chunks(messages):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets."
        )

    for message in messages:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError(
                f"Telegram API error: {response.text[:500]}"
            )


def split_messages(header, entries, footer, limit=3800):
    """Split at tender boundaries so individual records stay together."""
    messages = []
    current = header

    for entry in entries:
        candidate = current + "\n\n" + entry

        if len(candidate) > limit and current != header:
            messages.append(current)
            current = header + "\n\n" + entry
        else:
            current = candidate

    if current.strip():
        messages.append(current)

    if messages:
        if len(messages[-1]) + len(footer) + 2 <= limit:
            messages[-1] += "\n\n" + footer
        else:
            messages.append(footer)
    else:
        messages = [header + "\n\n" + footer]

    return messages


def extract_tenders(page):
    page.wait_for_timeout(8000)

    print("Rendered title:", page.title())
    print("Rendered URL:", page.url)
    print("Rendered rows:", page.locator("tr").count())

    tenders = {}
    rows = page.locator("tr")

    for i in range(rows.count()):
        row = rows.nth(i)
        cells = row.locator("td")
        raw_values = cells.all_text_contents()
        values = [clean(v) for v in raw_values]

        if len(values) < 6:
            continue

        row_text = clean(row.inner_text())
        if not row_text:
            continue

        # Remove action-button text that can shift the column positions.
        values = [
            value for value in values
            if value and value not in ACTION_WORDS
        ]

        if len(values) < 6:
            continue

        # Look for an individual tender detail link.
        detail_url = ""
        links = row.locator("a[href]")

        for j in range(links.count()):
            href = links.nth(j).get_attribute("href") or ""
            full_url = urljoin(BASE_URL, href)

            if (
                full_url.startswith(BASE_URL)
                and full_url != TENDERS_URL
                and (
                    "/tenders/" in full_url.lower()
                    or "/iraqi-tenders-prd/" in full_url.lower()
                )
            ):
                detail_url = full_url
                break

        # Expected columns after removing action-only cells:
        # number, title, organization, submission location,
        # publication date, closing date, remaining days, status.
        number = values[0]
        title = values[1]
        entity = values[2] if len(values) > 2 else ""
        location = values[3] if len(values) > 3 else ""
        published = values[4] if len(values) > 4 else ""
        deadline = values[5] if len(values) > 5 else ""
        status = values[7] if len(values) > 7 else ""

        if not title:
            continue

        if title in (
            "عنوان المناقصة او الفرصة الاستثمارية",
            "عنوان المناقصة",
            "عنوان المناقصة أو الفرصة الاستثمارية",
        ):
            continue

        tender = {
            "number": number or "نامشخص",
            "title": title,
            "title_fa": translate_arabic_to_persian(title),
            "entity": entity or "نامشخص",
            "location": location or "نامشخص",
            "published": published or "نامشخص",
            "deadline": deadline or "نامشخص",
            "status": status or "نامشخص",
            "url": detail_url,
        }

        key = stable_key(tender)
        tenders[key] = tender

    print("Extracted tenders:", len(tenders))

    if tenders:
        print("Sample extracted tender:")
        print(json.dumps(
            next(iter(tenders.values())),
            ensure_ascii=False,
            indent=2,
        ))

    return tenders


def load_history():
    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def make_report(now, tenders, previous):
    entries = []
    new_count = 0
    changed_count = 0

    for index, (key, tender) in enumerate(tenders.items(), start=1):
        old = previous.get(key)

        if old is None:
            label = "🆕 مورد جدید"
            new_count += 1
        elif (
            old.get("deadline") != tender.get("deadline")
            or old.get("status") != tender.get("status")
        ):
            label = "⚠️ تغییر احتمالی"
            changed_count += 1
        else:
            label = "📌 مناقصه"

        lines = [
            f"{index}. {label}",
            f"عنوان فارسی: {tender.get('title_fa', 'نامشخص')}",
            f"عنوان اصلی: {tender.get('title', 'نامشخص')}",
            f"سازمان برگزارکننده: {tender.get('entity', 'نامشخص')}",
            f"شماره مناقصه: {tender.get('number', 'نامشخص')}",
            f"محل ارائه پیشنهاد: {tender.get('location', 'نامشخص')}",
            f"تاریخ انتشار: {tender.get('published', 'نامشخص')}",
            f"آخرین مهلت: {tender.get('deadline', 'نامشخص')}",
            f"وضعیت: {translate_status(tender.get('status', ''))}",
        ]

        if tender.get("url"):
            lines.append(f"لینک جزئیات: {tender['url']}")
        else:
            lines.append(
                "لینک جزئیات: در جدول پیدا نشد؛ "
                "از فهرست رسمی ITP بررسی کنید."
            )

        entries.append("\n".join(lines))

    header = "\n".join([
        "🇮🇶 گزارش روزانه مناقصات عراق",
        f"زمان بغداد: {now:%Y-%m-%d %H:%M}",
        f"تعداد کل موارد استخراج‌شده: {len(tenders)}",
        f"موارد جدید: {new_count}",
        f"موارد دارای تغییر احتمالی: {changed_count}",
        "فهرست زیر شامل تمام موارد استخراج‌شده است.",
    ])

    footer = "\n".join([
        "منبع: ITP عراق",
        "توجه: وضعیت و مهلت را در منبع رسمی بررسی کنید.",
        "این گزارش تضمین‌کننده پوشش تمام نهادهای دولتی نیست.",
        TENDERS_URL,
    ])

    return split_messages(header, entries, footer)


def main():
    now = datetime.now(ZoneInfo("Asia/Baghdad"))

    print("Starting ITP browser monitor")
    print("Baghdad time:", now.isoformat())
    print("Target:", TENDERS_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ar-IQ")

        try:
            response = page.goto(
                TENDERS_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print(
                "Initial navigation status:",
                response.status if response else "No response",
            )

            tenders = extract_tenders(page)

            if not tenders:
                print("No tender rows could be extracted.")
                print(page.locator("body").inner_text()[:5000])
                raise RuntimeError(
                    "No tender records extracted from table rows."
                )
        finally:
            browser.close()

    previous = load_history()
    messages = make_report(now, tenders, previous)

    # Send the report before updating history.
    send_telegram_chunks(messages)

    STATE_FILE.write_text(
        json.dumps(tenders, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Report sent. Extracted {len(tenders)} records.")


if __name__ == "__main__":
    main()
```
