
import os
import json
import re
import requests

from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse


# =========================
# Configuration
# =========================

BASE_URL = "https://itp.iq"
TENDERS_URL = (
    "https://itp.iq/BUSINESS/tenders"
    "?count=100&page=0&statuses=all"
)

STATE_FILE = Path("seen_tenders.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


# =========================
# Telegram
# =========================

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing. "
            "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "in GitHub repository secrets."
        )

    api_url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    # Telegram messages have a maximum length.
    chunks = [
        message[i:i + 3900]
        for i in range(0, len(message), 3900)
    ]

    for chunk in chunks:
        response = requests.post(
            api_url,
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )

        print("Telegram HTTP status:", response.status_code)
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram API error: {result}"
            )


# =========================
# Helpers
# =========================

def clean_text(value):
    return " ".join((value or "").split())


def get_cell_values(row):
    return [
        clean_text(cell.get_text(" ", strip=True))
        for cell in row.find_all(["td", "th"])
    ]


def is_tender_link(href):
    if not href:
        return False

    full_url = urljoin(BASE_URL, href)
    parsed = urlparse(full_url)

    if parsed.netloc and parsed.netloc.lower() not in {
        "itp.iq",
        "www.itp.iq",
    }:
        return False

    path = parsed.path.lower()

    # Accept likely tender-detail URLs without assuming
    # that every detail URL has exactly the same suffix.
    return (
        "/business/tenders/" in path
        or "/business/tender/" in path
    )


def extract_tenders(html):
    soup = BeautifulSoup(html, "html.parser")

    print(
        "Page title:",
        soup.title.get_text(" ", strip=True)
        if soup.title else "No title found",
    )
    print("HTML length:", len(html))
    print("Table rows:", len(soup.select("tr")))
    print("Table cells:", len(soup.select("td")))
    print("Links found:", len(soup.select("a[href]")))

    tenders = {}

    # First attempt: table-based listing.
    for row in soup.select("tr"):
        cells = row.find_all("td")
        links = [
            a for a in row.select("a[href]")
            if is_tender_link(a.get("href"))
        ]

        if not links:
            continue

        values = [
            clean_text(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        link = links[0]
        url = urljoin(BASE_URL, link.get("href", ""))
        key = url.rstrip("/").split("/")[-1]

        if not key:
            continue

        title = clean_text(link.get_text(" ", strip=True))

        # Use table cells where available.
        number = values[0] if values else "نامشخص"

        if len(values) > 1:
            title = values[1] or title

        entity = values[2] if len(values) > 2 else "نامشخص"
        published = values[4] if len(values) > 4 else "نامشخص"
        deadline = values[5] if len(values) > 5 else "نامشخص"

        if not title:
            title = "عنوان در صفحه مشخص نیست"

        tenders[key] = {
            "number": number,
            "title": title,
            "entity": entity,
            "published": published,
            "deadline": deadline,
            "url": url,
        }

    # Second attempt: tender-detail links outside tables.
    # This may help if the website changes its table layout.
    if not tenders:
        print(
            "No table tenders found. "
            "Trying links outside tables..."
        )

        for link in soup.select("a[href]"):
            href = link.get("href", "")

            if not is_tender_link(href):
                continue

            title = clean_text(link.get_text(" ", strip=True))
            if not title:
                continue

            url = urljoin(BASE_URL, href)
            key = url.rstrip("/").split("/")[-1]

            if not key:
                continue

            parent_text = clean_text(
                link.parent.get_text(" ", strip=True)
                if link.parent else ""
            )

            tenders[key] = {
                "number": "نامشخص",
                "title": title,
                "entity": "نامشخص",
                "published": "نامشخص",
                "deadline": "نامشخص",
                "url": url,
                "listing_text": parent_text[:500],
            }

    print("Parsed tenders:", len(tenders))

    # Diagnostic sample: show some URLs if extraction fails.
    if not tenders:
        print("No tender links were recognized.")
        print("Sample links from page:")

        for link in soup.select("a[href]")[:20]:
            print(
                "-",
                clean_text(link.get_text(" ", strip=True))[:100],
                "|",
                link.get("href"),
            )

        # Save a small HTML sample for diagnostics in the logs.
        print("HTML sample:")
        print(html[:1500])

    return tenders


# =========================
# History
# =========================

def load_previous():
    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_history(tenders):
    STATE_FILE.write_text(
        json.dumps(
            tenders,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# =========================
# Report
# =========================

def build_report(now, tenders, previous):
    changes = []

    for key, tender in tenders.items():
        old = previous.get(key)

        if old is None:
            changes.append(("🆕 آگهی جدید در فهرست", tender))

        elif old.get("deadline") != tender.get("deadline"):
            changes.append(("⚠️ تغییر احتمالی مهلت", tender))

    # First successful run: report existing listings too.
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
        lines.append(
            "در مقایسه با سابقه ذخیره‌شده، "
            "مورد جدید یا تغییر مهلتی شناسایی نشد."
        )

    for label, tender in changes[:20]:
        lines.extend([
            label,
            f"عنوان: {tender.get('title', 'نامشخص')}",
            f"سازمان: {tender.get('entity', 'نامشخص')}",
            f"شماره: {tender.get('number', 'نامشخص')}",
            f"تاریخ انتشار: {tender.get('published', 'نامشخص')}",
            f"مهلت: {tender.get('deadline', 'نامشخص')}",
            f"لینک: {tender.get('url', '')}",
            "",
        ])

    lines.extend([
        "منبع: پورتال ITP عراق",
        "توجه: استخراج‌شدن یک آگهی به‌تنهایی به معنی فعال‌بودن آن نیست.",
        "تاریخ و مهلت هر آگهی باید از منبع رسمی تأیید شود.",
        "این گزارش تضمین‌کننده پوشش تمام نهادهای دولتی نیست.",
    ])

    return "\n".join(lines)


# =========================
# Main
# =========================

def main():
    now = datetime.now(ZoneInfo("Asia/Baghdad"))

    print("Starting Iraq tender bot...")
    print("Baghdad time:", now.isoformat())
    print("Request URL:", TENDERS_URL)

    try:
        response = requests.get(
            TENDERS_URL,
            headers=HEADERS,
            timeout=40,
        )

        print("ITP HTTP status:", response.status_code)
        print("Final URL:", response.url)
        print(
            "Content-Type:",
            response.headers.get("Content-Type"),
        )

        response.raise_for_status()

    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to retrieve ITP page: {exc}"
        ) from exc

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        print(
            "Warning: response Content-Type is not HTML:",
            content_type,
        )

    tenders = extract_tenders(response.text)

    if not tenders:
        raise RuntimeError(
            "No tenders could be extracted from ITP. "
            "Review the diagnostic logs above. "
            "No Telegram report was sent."
        )

    previous = load_previous()
    report = build_report(now, tenders, previous)

    # Send first. Save state only after successful delivery.
    send_telegram(report)
    save_history(tenders)

    print(
        f"Success: report sent. "
        f"Parsed {len(tenders)} tender records."
    )


if __name__ == "__main__":
    main()
