"""
بوت "ورتّل" — بينشر محتوى من Google Sheet لقناة تليجرام في مواعيدها المحددة.

المصدر: Google Sheet منشور كـ CSV (رابط SHEET_CSV_URL).
الأعمدة المتوقعة (الترتيب مش مهم، الاسم هو اللي مهم):
    التاريخ        -> بصيغة D/M/YYYY  (مثال: 18/9/2026)
    الوقت          -> بصيغة 24 ساعة HH:MM  (مثال: 05:30)
    النوع          -> دعاء / حديث / خبر / دليل ... إلخ (بيتحط في أول السطر كعنوان)
    النص           -> نص البوست
    المصدر         -> اختياري، بيتحط في آخر البوست لو موجود
    تمت المراجعة   -> اختياري. لو موجود ومكتوب فيه "نعم" فقط، ينشر. لو العمود مش
                       موجود أصلاً، كل الصفوف تتعامل كأنها متراجعة.

آلية منع التكرار:
    بعد ما ينشر أي صف، بيتسجل "بصمة" له (تاريخ+وقت+أول 40 حرف من النص) في
    ملف posted_log.json في نفس الريبو، وده بيمنع إعادة نشر نفس الصف تاني
    لو الأكشن اشتغل أكتر من مرة حوالين نفس الموعد.
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------- إعدادات ----------
SHEET_CSV_URL = os.environ["SHEET_CSV_URL"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]  # مثال: @Wartil  أو  -1001234567890

TIMEZONE = ZoneInfo("Africa/Cairo")
LOG_PATH = "posted_log.json"

# لو الأكشن اتأخر شوية عن موعده (شائع في GitHub Actions)، هنقبل أي صف
# موعده خلال آخر 30 دقيقة ولسه ما نزلش، بدل ما نستنى بالظبط نفس الدقيقة.
LOOKBACK_MINUTES = 30


def normalize_date(raw: str) -> str:
    """يحاول يفهم صيغ متعددة للتاريخ ويرجعها كـ YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"مش قادر أفهم صيغة التاريخ: {raw!r}")


def normalize_time(raw: str) -> str:
    raw = raw.strip()

    # الصيغة العربية: "10:22:00 ص" أو "10:22 م"
    is_pm = None
    if "ص" in raw:
        is_pm = False
        raw = raw.replace("ص", "").strip()
    elif "م" in raw:
        is_pm = True
        raw = raw.replace("م", "").strip()

    if is_pm is not None:
        # فيه علامة صباح/مساء -> ده وقت بنظام 12 ساعة
        for fmt in ("%I:%M:%S", "%I:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                hour = dt.hour % 12
                if is_pm:
                    hour += 12
                return f"{hour:02d}:{dt.minute:02d}"
            except ValueError:
                continue
    else:
        # من غير علامة -> نظام 24 ساعة (الصيغة القديمة)
        for fmt in ("%H:%M:%S", "%H:%M", "%H.%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                return f"{dt.hour:02d}:{dt.minute:02d}"
            except ValueError:
                continue

    raise ValueError(f"مش قادر أفهم صيغة الوقت: {raw!r}")


def load_rows():
    resp = requests.get(SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(resp.text))
    return [row for row in reader]


def load_posted_log():
    if not os.path.exists(LOG_PATH):
        return set()
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_posted_log(log: set):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(log), f, ensure_ascii=False, indent=2)


def row_key(row: dict, date_norm: str, time_norm: str) -> str:
    text_snippet = (row.get("النص") or "").strip()[:40]
    return f"{date_norm}_{time_norm}_{text_snippet}"


def is_reviewed(row: dict) -> bool:
    # لو العمود مش موجود أصلاً في الشيت، اعتبر الصف متراجع
    if "تمت المراجعة" not in row:
        return True
    value = (row.get("تمت المراجعة") or "").strip()
    return value == "نعم"


def build_message(row: dict) -> str:
    kind = (row.get("النوع") or "").strip()
    text = (row.get("النص") or "").strip()
    source = (row.get("المصدر") or "").strip()

    parts = []
    if kind:
        parts.append(f"📿 {kind}")
    parts.append(text)
    if source:
        parts.append(f"\n— {source}")
    return "\n\n".join(parts)


def send_to_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHANNEL_ID,
            "text": message,
            "parse_mode": None,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"فشل الإرسال لتليجرام: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def main():
    now = datetime.now(TIMEZONE)
    window_start = now - timedelta(minutes=LOOKBACK_MINUTES)

    rows = load_rows()
    posted_log = load_posted_log()
    any_new = False

    for row in rows:
        raw_date = row.get("التاريخ") or ""
        raw_time = row.get("الوقت") or ""
        if not raw_date.strip() or not raw_time.strip():
            continue

        try:
            date_norm = normalize_date(raw_date)
            time_norm = normalize_time(raw_time)
        except ValueError as e:
            print(f"تخطي صف بتاريخ/وقت غير مفهوم: {e}", file=sys.stderr)
            continue

        scheduled_dt = datetime.strptime(
            f"{date_norm} {time_norm}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TIMEZONE)

        if not (window_start <= scheduled_dt <= now):
            continue

        if not is_reviewed(row):
            print(f"تخطي صف لسه مش متراجع: {date_norm} {time_norm}")
            continue

        key = row_key(row, date_norm, time_norm)
        if key in posted_log:
            continue

        message = build_message(row)
        if not message.strip():
            continue

        send_to_telegram(message)
        posted_log.add(key)
        any_new = True
        print(f"تم النشر: {key}")

    if any_new:
        save_posted_log(posted_log)
    else:
        print("مفيش محتوى جديد مستحق النشر دلوقتي.")


if __name__ == "__main__":
    main()
