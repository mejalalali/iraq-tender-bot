import os
import json
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import gspread
from google.oauth2.service_account import Credentials

API_URL = "https://api.itp.iq/api/bus/tenders/get"
SOURCE_URL = "https://itp.iq/BUSINESS/tenders?count=100&page=0&statuses=all"
SPREADSHEET_ID = "1-B1L5GTpqngMpfvERI7mCCge2pW9dkOkszl_Ierlb3A"

STATE_FILE = Path("seen_tenders.json")
TRANSLATION_FILE = Path("translations.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

BAGHDAD = ZoneInfo("Asia/Baghdad")
API_STATUSES = ["Published", "Closed", "Awarded", "Canceled"]

HEADERS = [
    "ردیف",
    "شناسه مناقصه",
    "شماره مناقصه",
    "عنوان فارسی",
    "عنوان اصلی عربی/انگلیسی",
    "سازمان برگزارکننده",
    "استان",
    "تاریخ انتشار",
    "مهلت نهایی",
    "وضعیت",
    "لینک آگهی/سند",
    "آخرین زمان به‌روزرسانی",
]


def clean(value):
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def translate_arabic_to_persian(text, cache):
    text = clean(text)
    if not text:
        return "نامشخص"
    if text in cache:
        return cache[text]

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
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        translated = "".join(
            part[0] for part in data[0] if part and part[0]
        )
        result = clean(translated) or text
    except Exception as exc:
        print(f"Translation failed; keeping original title: {exc}")
        result = text

    cache[text] = result
    # Save incrementally so a long run does not lose completed translations.
    save_json(TRANSLATION_FILE, cache)
    time.sleep(0.15)
    return result


def format_date(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(BAGHDAD).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return clean(value)


def parse_api_page(session, page_number, count):
    payload = {
        "page": page_number,
        "count": count,
        "searchTerm": "",
        "statuses": API_STATUSES,
        "ownedBy": "Organization",
        "tags": [],
        "tenderStatus": API_STATUSES,
    }
    response = session.post(
        API_URL,
        json=payload,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://itp.iq",
            "Referer": SOURCE_URL,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError(
            f"Unexpected ITP API response on page {page_number}: "
            f"{str(data)[:500]}"
        )
    return data


def fetch_all_tenders():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 IraqTenderMonitor/1.0"})

    # Try 100 records per page first. If the API rejects that page size,
    # retry using the observed default of 10.
    try:
        first = parse_api_page(session, 0, 100)
        page_size = 100
    except requests.RequestException as exc:
        print(f"API rejected count=100; retrying count=10: {exc}")
        first = parse_api_page(session, 0, 10)
        page_size = 10

    all_records = list(first["data"])
    # Some APIs silently cap the requested page size. Trust the actual
    # count returned by the server, not the requested count.
    actual_count = int(first.get("count") or len(first["data"]) or page_size)
    page_size = actual_count
    total_pages = int(first.get("totalPages") or 1)
    print(
        f"ITP API: actualCount={page_size}, totalPages={total_pages}, "
        f"firstPageRecords={len(first['data'])}"
    )

    for page_number in range(1, total_pages):
        page_data = parse_api_page(session, page_number, page_size)
        records = page_data["data"]
        all_records.extend(records)
        if page_number % 10 == 0 or page_number == total_pages - 1:
            print(
                f"Fetched page {page_number + 1}/{total_pages}; "
                f"records so far: {len(all_records)}"
            )
        time.sleep(0.15)

    # Deduplicate by stable ITP tenderId, retaining the newest version
    # encountered if the API unexpectedly repeats a tender.
    unique = {}
    for record in all_records:
        tender_id = clean(record.get("tenderId"))
        if tender_id:
            unique[tender_id] = record

    print(f"Fetched {len(all_records)} records; unique IDs: {len(unique)}")
    return list(unique.values())


def get_document_url(record):
    files = record.get("tenderFiles") or []
    if not isinstance(files, list):
        return ""
    # Prefer the official advertisement document.
    for item in files:
        if isinstance(item, dict) and item.get("type") == "AdDocument":
            return clean(item.get("url"))
    for item in files:
        if isinstance(item, dict) and item.get("url"):
            return clean(item.get("url"))
    return ""


def normalize_tender(record, translations, now_text):
    organization = record.get("organization") or {}
    parent = organization.get("parentOrganization") or {}
    organization_name = clean(organization.get("name"))
    parent_name = clean(parent.get("name"))
    if parent_name and parent_name not in organization_name:
        organization_name = f"{organization_name} ({parent_name})"

    title = clean(record.get("title"))
    status_raw = clean(record.get("status"))
    extension_count = int(record.get("extensionCount") or 0)

    return {
        "id": clean(record.get("tenderId")),
        "version_id": clean(record.get("tenderVersionId")),
        "number": clean(record.get("number")) or "نامشخص",
        "title": title or "نامشخص",
        "title_fa": translate_arabic_to_persian(title, translations),
        "organization": organization_name or "نامشخص",
        "province": "",
        "published": format_date(record.get("publishDate")),
        "deadline": format_date(record.get("closeDate")),
        "status_raw": status_raw,
        "status_fa": "تمدیدشده" if extension_count > 0 else "فعال",
        "extension_count": extension_count,
        "republish_count": int(record.get("republishCount") or 0),
        "url": get_document_url(record),
        "updated": now_text,
    }


def classify_records(records, translations, now_text):
    active = []
    extended = []
    excluded_counts = {
        "Canceled": 0,
        "Closed": 0,
        "Awarded": 0,
        "Other": 0,
        "ExpiredPublished": 0,
    }
    now_utc = datetime.now(timezone.utc)

    for record in records:
        status = clean(record.get("status"))
        if status == "Canceled":
            excluded_counts["Canceled"] += 1
            continue
        if status == "Closed":
            excluded_counts["Closed"] += 1
            continue
        if status == "Awarded":
            excluded_counts["Awarded"] += 1
            continue
        if status != "Published":
            excluded_counts["Other"] += 1
            continue

        close_date = record.get("closeDate")
        if close_date:
            try:
                close_dt = datetime.fromisoformat(
                    str(close_date).replace("Z", "+00:00")
                )
                if close_dt <= now_utc:
                    excluded_counts["ExpiredPublished"] += 1
                    continue
            except ValueError:
                # Do not silently discard a Published tender because its
                # date format is unfamiliar; retain it for manual review.
                pass

        tender = normalize_tender(record, translations, now_text)
        if tender["extension_count"] > 0:
            extended.append(tender)
        else:
            active.append(tender)

    active.sort(key=lambda item: (item["deadline"], item["number"]))
    extended.sort(key=lambda item: (item["deadline"], item["number"]))
    return active, extended, excluded_counts


def get_google_client():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError(
            "Missing GOOGLE_SERVICE_ACCOUNT_JSON GitHub Actions secret."
        )
    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON."
        ) from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )
    return gspread.authorize(credentials)


def ensure_worksheet(spreadsheet, title):
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=len(HEADERS),
        )
    return worksheet


def rows_for_sheet(tenders):
    rows = [HEADERS]
    for index, tender in enumerate(tenders, start=1):
        rows.append([
            index,
            tender["id"],
            tender["number"],
            tender["title_fa"],
            tender["title"],
            tender["organization"],
            tender["province"],
            tender["published"],
            tender["deadline"],
            tender["status_fa"],
            tender["url"],
            tender["updated"],
        ])
    return rows


def replace_sheet_contents(worksheet, tenders):
    values = rows_for_sheet(tenders)
    worksheet.clear()
    worksheet.update(values=values, range_name="A1", value_input_option="RAW")
    worksheet.freeze(rows=1)
    worksheet.format(
        "A1:L1",
        {
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    )
    worksheet.format(
        f"A2:L{max(2, len(values))}",
        {
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    )
    # Apply a filter to the populated header/data range.
    try:
        worksheet.set_basic_filter()
    except Exception as exc:
        print(f"Could not apply filter to {worksheet.title}: {exc}")


def update_google_sheet(active, extended):
    client = get_google_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    active_ws = ensure_worksheet(spreadsheet, "Active Tenders")
    extended_ws = ensure_worksheet(spreadsheet, "Extended Tenders")

    replace_sheet_contents(active_ws, active)
    replace_sheet_contents(extended_ws, extended)

    return spreadsheet.url


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets not configured; skipping Telegram message.")
        return

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
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {str(result)[:500]}")


def main():
    now = datetime.now(BAGHDAD)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    print("Starting Iraq tender API monitor.")
    print("Baghdad time:", now.isoformat())
    print("API:", API_URL)

    records = fetch_all_tenders()
    if not records:
        raise RuntimeError("ITP API returned zero tender records; refusing to clear Sheets.")

    translations = load_json(TRANSLATION_FILE)
    previous = load_json(STATE_FILE)

    active, extended, excluded = classify_records(
        records, translations, now_text
    )

    # Persist translations before the Google Sheets operation.
    save_json(TRANSLATION_FILE, translations)

    sheet_url = update_google_sheet(active, extended)

    current_state = {}
    new_count = 0
    changed_count = 0
    for tender in active + extended:
        key = tender["id"]
        compact = {
            "version_id": tender["version_id"],
            "deadline": tender["deadline"],
            "status": tender["status_raw"],
            "extension_count": tender["extension_count"],
            "number": tender["number"],
            "title": tender["title"],
        }
        old = previous.get(key)
        if old is None:
            new_count += 1
        elif old != compact:
            changed_count += 1
        current_state[key] = compact

    save_json(STATE_FILE, current_state)

    message = "\n".join([
        "🇮🇶 به‌روزرسانی مناقصات دولتی عراق انجام شد.",
        f"زمان بغداد: {now:%Y-%m-%d %H:%M}",
        f"مناقصات فعال: {len(active)}",
        f"مناقصات تمدیدشده: {len(extended)}",
        f"موارد جدید: {new_count}",
        f"موارد دارای تغییر: {changed_count}",
        "",
        "موارد حذف‌شده از جدول:",
        f"لغوشده: {excluded['Canceled']}",
        f"بسته‌شده: {excluded['Closed']}",
        f"واگذارشده: {excluded['Awarded']}",
        f"منتشرشده با مهلت گذشته: {excluded['ExpiredPublished']}",
        "",
        "Google Sheets:",
        sheet_url,
    ])
    send_telegram(message)

    print(
        f"Completed. Active={len(active)}, Extended={len(extended)}, "
        f"Excluded={sum(excluded.values())}"
    )


if __name__ == "__main__":
    main()
