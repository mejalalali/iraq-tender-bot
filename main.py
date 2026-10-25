
import os
import json
import requests

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
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


def is_tender_url(href):
    if not href:
        return False

    full_url = urljoin(BASE_URL, href)
    parsed = urlparse(full_url)

    if parsed.netloc.lower() not in ("itp.iq", "www.itp.iq"):
        return False

    path = parsed.path.lower()

    return (
        "/business/tenders/" in path
        or "/iraqi-tenders-prd/" in path
    )


def extract_tenders(page):
    # Inspect rendered page, not only the original HTML.
    page.wait_for_timeout(8000)

    print("Rendered title:", page.title())
    print("Rendered URL:", page.url)
    print("Rendered HTML length:", len(page.content()))
    print("Rendered links:", page.locator("a[href]").count())
    print("Rendered rows:", page.locator("tr").count())

    tenders = {}

    # First try table rows.
    rows = page.locator("tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        links = row.locator("a[href]")
        values = [
            clean(x)
            for x in row.locator("td").all_text_contents()
        ]

        for j in range(links.count()):
            link = links.nth(j)
            href = link.get_attribute("href") or ""

            if not is_tender_url(href):
                continue

            url = urljoin(BASE_URL, href)
            key = url.rstrip("/").split("/")[-1]
            title = clean(link.inner_text())

            if not key or not title:
                continue

            tenders[key] = {
                "number": values[0] if values else "نامشخص",
                "title": values[1] if len(values) > 1 else title,
                "entity": values[2] if len(values) > 2 else "نامشخص",
                "published": values[4] if len(values) > 4 else "نامشخص",
                "deadline": values[5] if len(values) > 5 else "نامشخص",
                "url": url,
            }

    # Fallback: rendered detail links outside tables.
    if not tenders:
        links = page.locator("a[href]")
        print("Searching rendered detail links...")

        for i in range(links.count()):
            link = links.nth(i)
            href = link.get_attribute("href") or ""

            if not is_tender_url(href):
                continue

            title = clean(link.inner_text())
            if not title:
                continue

            url = urljoin(BASE_URL, href)
            key = url.rstrip("/").split("/")[-1]

            if not key:
                continue

            parent_text = ""
            try:
                parent_text = clean(
                    link.locator("xpath=..").inner_text()
                )
            except Exception:
                pass

            tenders[key] = {
                "number": "نامشخص",
                "title": title,
                "entity": "نامشخص",
                "published": "نامشخص",
                "deadline": "نامشخص",
                "url": url,
                "listing_text": parent_text[:500],
            }

    print("Extracted tenders:", len(tenders))
    return tenders


def load_history():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
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

    if not previous:
        changes = [
            ("📌 آگهی موجود در فهرست", tender)
            for tender in tenders.values()
        ]

    lines = [
        "گزارش پایش مناقصات عراق",
        f"زمان بغداد: {now:%Y-%m-%d %H:%M}",
        f"تعداد موارد استخراج‌شده: {len(tenders)}",
        f"موارد جدید یا تغییر مهلت: {len(changes)}",
        "",
    ]

    if not changes:
        lines.append("مورد جدید یا تغییر مهلت شناسایی نشد.")

    for label, tender in changes[:20]:
        lines.extend([
            label,
            f"عنوان: {tender.get('title', 'نامشخص')}",
            f"سازمان: {tender.get('entity', 'نامشخص')}",
            f"شماره: {tender.get('number', 'نامشخص')}",
            f"انتشار: {tender.get('published', 'نامشخص')}",
            f"مهلت: {tender.get('deadline', 'نامشخص')}",
            f"لینک: {tender.get('url', '')}",
            "",
        ])

    lines.extend([
        "منبع: ITP عراق",
        "توجه: فعال‌بودن و مهلت مناقصه باید در منبع رسمی تأیید شود.",
        "این گزارش تضمین‌کننده پوشش تمام نهادهای دولتی نیست.",
    ])

    return "\n".join(lines)


def main():
    now = datetime.now(ZoneInfo("Asia/Baghdad"))
    network_urls = []

    print("Starting ITP browser monitor")
    print("Baghdad time:", now.isoformat())
    print("Target:", TENDERS_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ar-IQ",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )

        def record_request(request):
            url = request.url
            low = url.lower()

            if any(term in low for term in (
                "api", "tender", "graphql", "business"
            )):
                if len(network_urls) < 100:
                    network_urls.append(url)

        page.on("request", record_request)

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
                print("No tender records extracted.")
                print("Relevant network requests:")
                for url in network_urls:
                    print("NETWORK:", url)

                print("Rendered page text sample:")
                print(page.locator("body").inner_text()[:3000])

                raise RuntimeError(
                    "No tenders extracted. See rendered page and "
                    "network diagnostics above."
                )

        finally:
            browser.close()

    previous = load_history()
    report = make_report(now, tenders, previous)

    # Save history only after Telegram confirms successful delivery.
    send_telegram(report)

    STATE_FILE.write_text(
        json.dumps(tenders, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Report sent. Extracted {len(tenders)} records.")


if __name__ == "__main__":
    main()
