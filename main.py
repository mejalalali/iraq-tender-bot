
import os
import json
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


def clean(value):
    return " ".join((value or "").split())


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets."
        )

    chunks = [
        message[i:i + 3900]
        for i in range(0, len(message), 3900)
    ]

    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError(
                f"Telegram API error: {response.text[:500]}"
            )


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
        values = [
            clean(value)
            for value in cells.all_text_contents()
        ]

        # Ignore table headers and empty rows.
        values = [value for value in values]

        if len(values) < 6:
            continue

        # Join the row's text to identify rows with actual tender data.
        row_text = clean(row.inner_text())
        if not row_text:
            continue

        # Look for a detail link if the page provides one.
        detail_url = TENDERS_URL
        links = row.locator("a[href]")

        for j in range(links.count()):
            href = links.nth(j).get_attribute("href") or ""
            full_url = urljoin(BASE_URL, href)

            if (
                full_url.startswith(BASE_URL)
                and (
                    "/tenders/" in full_url.lower()
                    or "/iraqi-tenders-prd/" in full_url.lower()
                )
            ):
                detail_url = full_url
                break

        # Expected visible columns:
        # number, title, organization, submission location,
        # publication date, closing date, remaining days, status, details.
        number = values[0] if len(values) > 0 else ""
        title = values[1] if len(values) > 1 else ""
        entity = values[2] if len(values) > 2 else ""
        location = values[3] if len(values) > 3 else ""
        published = values[4] if len(values) > 4 else ""
        deadline = values[5] if len(values) > 5 else ""
        status = values[7] if len(values) > 7 else ""

        # Skip likely header or action-only rows.
        if not title or title in (
            "عنوان المناقصة او الفرصة الاستثمارية",
            "عنوان المناقصة",
        ):
            continue

        # Stable key even when no detail URL is exposed.
        identity = "|".join([
            number,
            title,
            entity,
            published,
            deadline,
        ])
        key = str(abs(hash(identity)))

        tenders[key] = {
            "number": number or "نامشخص",
            "title": title,
            "entity": entity or "نامشخص",
            "location": location or "نامشخص",
            "published": published or "نامشخص",
            "deadline": deadline or "نامشخص",
            "status": status or "نامشخص",
            "url": detail_url,
        }

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
    changes = []

    for key, tender in tenders.items():
        old = previous.get(key)

        if old is None:
            changes.append(("🆕 آگهی جدید در فهرست", tender))
        elif old.get("deadline") != tender.get("deadline"):
            changes.append(("⚠️ تغییر احتمالی مهلت", tender))
        elif old.get("status") != tender.get("status"):
            changes.append(("⚠️ تغییر احتمالی وضعیت", tender))

    if not previous:
        changes = [
            ("📌 آگهی موجود در فهرست", tender)
            for tender in tenders.values()
        ]

    lines = [
        "گزارش پایش مناقصات عراق",
        f"زمان بغداد: {now:%Y-%m-%d %H:%M}",
        f"تعداد موارد استخراج‌شده: {len(tenders)}",
        f"موارد جدید یا تغییرات شناسایی‌شده: {len(changes)}",
        "",
    ]

    if not changes:
        lines.append("مورد جدید یا تغییر قابل تشخیص شناسایی نشد.")

    for label, tender in changes[:20]:
        lines.extend([
            label,
            f"عنوان: {tender.get('title', 'نامشخص')}",
            f"سازمان: {tender.get('entity', 'نامشخص')}",
            f"شماره: {tender.get('number', 'نامشخص')}",
            f"محل ارائه: {tender.get('location', 'نامشخص')}",
            f"انتشار: {tender.get('published', 'نامشخص')}",
            f"مهلت: {tender.get('deadline', 'نامشخص')}",
            f"وضعیت: {tender.get('status', 'نامشخص')}",
            f"لینک: {tender.get('url', '')}",
            "",
        ])

    lines.extend([
        "منبع: ITP عراق",
        "توجه: فعال‌بودن و مهلت مناقصه را در منبع رسمی بررسی کنید.",
        "این گزارش تضمین‌کننده پوشش تمام نهادهای دولتی نیست.",
    ])

    return "\n".join(lines)


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
                print("Page text sample:")
                print(page.locator("body").inner_text()[:5000])

                raise RuntimeError(
                    "No tender records extracted from table rows."
                )

        finally:
            browser.close()

    previous = load_history()
    report = make_report(now, tenders, previous)

    # Send successfully before updating history.
    send_telegram(report)

    STATE_FILE.write_text(
        json.dumps(tenders, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Report sent. Extracted {len(tenders)} records.")


if __name__ == "__main__":
    main()
