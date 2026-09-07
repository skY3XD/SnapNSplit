"""
Receipt Split Telegram Bot (@snapnsplit_bot)
Uses python-telegram-bot v20+ and Google Gemini for receipt OCR.
Default currency: EUR

- Languages: EN, DE, PL, UA, RU (asked on /start)
- Currencies: EUR, USD, PLN, UAH, RUB, MNT (rates relative to EUR)
- Photos processed only on explicit request:
  * Private: caption /split, ReplyKeyboard button, or /split mode
  * Group: @snapnsplit_bot mention, /split in caption, reply to bot message,
           /split then next photo, or reply to a photo with /split
"""

import asyncio
import json
import logging
import os
import re
from io import BytesIO
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_USERNAME = "snapnsplit_bot"  # without @

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY in .env")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-3.6-flash"

# One active receipt per chat
receipts: dict[int, dict[str, Any]] = {}

# Preferred language per chat (set on /start language pick)
chat_langs: dict[int, str] = {}

# Private: user ids that enabled one-shot split mode
split_mode_users: set[int] = set()

# Groups: chat ids where next photo should be scanned (after /split)
split_mode_chats: set[int] = set()

RATES_TO_EUR: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.86,
    "PLN": 0.232,
    "UAH": 0.021,
    "RUB": 0.010,
    "MNT": 0.000244,
}
SUPPORTED_CURRENCIES = ("EUR", "USD", "PLN", "UAH", "RUB", "MNT")
SUPPORTED_LANGS = ("EN", "DE", "PL", "UA", "RU")
TIP_OPTIONS = (0, 5, 10, 15)
DEFAULT_CURRENCY = "EUR"
DEFAULT_LANG = "EN"

LANG_LABELS = {
    "EN": "🇬🇧 English",
    "DE": "🇩🇪 Deutsch",
    "PL": "🇵🇱 Polski",
    "UA": "🇺🇦 Українська",
    "RU": "🇷🇺 Русский",
}

SPLIT_MODE_BUTTONS = {
    "EN": "📷 Split receipt",
    "DE": "📷 Beleg teilen",
    "PL": "📷 Podziel rachunek",
    "UA": "📷 Розділити чек",
    "RU": "📷 Разделить чек",
}
SPLIT_MODE_BUTTON_SET = set(SPLIT_MODE_BUTTONS.values())

TEXTS: dict[str, dict[str, str]] = {
    "EN": {
        "choose_lang_first": "🌐 Please choose your language:",
        "welcome": (
            "👋 *Receipt Split Bot*\n\n"
            "Send a photo of a receipt with the caption `/split` "
            "(or use the button below) and I'll help split the bill.\n\n"
            "In groups: mention @snapnsplit_bot, use `/split`, reply to a bot message, "
            "or reply to a photo with `/split`.\n\n"
            "Commands:\n"
            "/start — language + this message\n"
            "/split — next photo will be scanned\n"
            "/cancel — discard current receipt\n"
            "/help — help"
        ),
        "help": (
            "📷 To scan a receipt:\n"
            "• Private: photo + `/split` caption, button, or /split then photo\n"
            "• Group: @snapnsplit_bot, caption `/split`, reply to bot, "
            "/split then next photo, or reply to a photo with `/split`"
        ),
        "cancelled": "❌ Current receipt cancelled.",
        "no_receipt": "No active receipt. Send a photo first.",
        "processing": "🔍 Scanning receipt, please wait…",
        "scan_error": "❌ Could not read the receipt. Please send a clearer photo.",
        "receipt_header": "🧾 *Receipt successfully scanned!*",
        "total": "💰 Total Amount",
        "participants": "👥 Participants",
        "tip": "☕️ Tip",
        "lang_currency": "🌐 Language: {lang} | 💵 Currency: {curr}",
        "select_below": "Select items below or adjust settings:",
        "btn_lang": "🌐 Language",
        "btn_currency": "💵 Currency",
        "btn_people": "👥 People",
        "btn_tip": "☕️ Tip",
        "btn_payer": "💳 Who paid?",
        "btn_calc": "✅ Calculate Final Split",
        "selected": "Selected",
        "final_title": "📊 *FINAL RECEIPT SPLIT*",
        "paid_by": "Paid by",
        "tip_included": "Tip included",
        "to_pay": "To pay {payer}",
        "total_split": "Total split",
        "share": "1/{n}",
        "tip_line": "Tip ({pct}%)",
        "choose_payer": "💳 Select who paid for everything:",
        "choose_people": "👥 How many people? (for unassigned items)",
        "choose_tip": "☕️ Select tip percentage:",
        "choose_lang": "🌐 Select language:",
        "choose_currency": "💵 Select currency:",
        "payer_not_set": "not set",
        "back": "⬅️ Back",
        "split_mode_on": "📷 Split mode ON. Send a receipt photo now.",
        "split_mode_on_group": (
            "📷 Split mode ON for this chat. "
            "The next photo will be scanned (or reply to a photo with `/split`)."
        ),
        "lang_set": "✅ Language set to *{lang}*.",
    },
    "DE": {
        "choose_lang_first": "🌐 Bitte wähle deine Sprache:",
        "welcome": (
            "👋 *Beleg-Teilungs-Bot*\n\n"
            "Sende ein Foto eines Belegs mit der Beschriftung `/split` "
            "(oder nutze den Button unten), und ich helfe, die Rechnung zu teilen.\n\n"
            "In Gruppen: @snapnsplit_bot erwähnen, `/split` nutzen, auf eine Bot-Nachricht "
            "antworten oder auf ein Foto mit `/split` antworten.\n\n"
            "Befehle:\n"
            "/start — Sprache + diese Nachricht\n"
            "/split — nächstes Foto wird gescannt\n"
            "/cancel — aktuellen Beleg verwerfen\n"
            "/help — Hilfe"
        ),
        "help": (
            "📷 Beleg scannen:\n"
            "• Privat: Foto + Beschriftung `/split`, Button oder /split dann Foto\n"
            "• Gruppe: @snapnsplit_bot, Beschriftung `/split`, Antwort auf Bot, "
            "/split dann nächstes Foto, oder Antwort auf Foto mit `/split`"
        ),
        "cancelled": "❌ Aktueller Beleg abgebrochen.",
        "no_receipt": "Kein aktiver Beleg. Zuerst ein Foto senden.",
        "processing": "🔍 Beleg wird gescannt, bitte warten…",
        "scan_error": "❌ Beleg konnte nicht gelesen werden. Bitte klareres Foto senden.",
        "receipt_header": "🧾 *Beleg erfolgreich gescannt!*",
        "total": "💰 Gesamtsumme",
        "participants": "👥 Teilnehmer",
        "tip": "☕️ Trinkgeld",
        "lang_currency": "🌐 Sprache: {lang} | 💵 Währung: {curr}",
        "select_below": "Positionen auswählen oder Einstellungen ändern:",
        "btn_lang": "🌐 Sprache",
        "btn_currency": "💵 Währung",
        "btn_people": "👥 Personen",
        "btn_tip": "☕️ Trinkgeld",
        "btn_payer": "💳 Wer hat bezahlt?",
        "btn_calc": "✅ Teilung berechnen",
        "selected": "Gewählt",
        "final_title": "📊 *ENDGÜLTIGE BELEGTEILUNG*",
        "paid_by": "Bezahlt von",
        "tip_included": "Trinkgeld enthalten",
        "to_pay": "Zu zahlen an {payer}",
        "total_split": "Gesamt geteilt",
        "share": "1/{n}",
        "tip_line": "Trinkgeld ({pct}%)",
        "choose_payer": "💳 Wer hat alles bezahlt?",
        "choose_people": "👥 Wie viele Personen? (für nicht zugewiesene Positionen)",
        "choose_tip": "☕️ Trinkgeld-Prozentsatz wählen:",
        "choose_lang": "🌐 Sprache wählen:",
        "choose_currency": "💵 Währung wählen:",
        "payer_not_set": "nicht gesetzt",
        "back": "⬅️ Zurück",
        "split_mode_on": "📷 Split-Modus AN. Sende jetzt ein Beleg-Foto.",
        "split_mode_on_group": (
            "📷 Split-Modus AN für diesen Chat. "
            "Das nächste Foto wird gescannt (oder antworte auf ein Foto mit `/split`)."
        ),
        "lang_set": "✅ Sprache auf *{lang}* gesetzt.",
    },
    "PL": {
        "choose_lang_first": "🌐 Wybierz język:",
        "welcome": (
            "👋 *Bot do dzielenia rachunku*\n\n"
            "Wyślij zdjęcie paragonu z podpisem `/split` "
            "(lub użyj przycisku poniżej), a pomogę podzielić rachunek.\n\n"
            "W grupach: wspomnij @snapnsplit_bot, użyj `/split`, odpowiedz na wiadomość bota "
            "lub odpowiedz na zdjęcie komendą `/split`.\n\n"
            "Komendy:\n"
            "/start — język + ta wiadomość\n"
            "/split — następne zdjęcie zostanie zeskanowane\n"
            "/cancel — anuluj bieżący rachunek\n"
            "/help — pomoc"
        ),
        "help": (
            "📷 Aby zeskanować paragon:\n"
            "• Prywatnie: zdjęcie + `/split`, przycisk lub /split, potem zdjęcie\n"
            "• Grupa: @snapnsplit_bot, podpis `/split`, odpowiedź na bota, "
            "/split potem następne zdjęcie, lub odpowiedź na zdjęcie z `/split`"
        ),
        "cancelled": "❌ Bieżący rachunek anulowany.",
        "no_receipt": "Brak aktywnego rachunku. Najpierw wyślij zdjęcie.",
        "processing": "🔍 Skanowanie paragonu, proszę czekać…",
        "scan_error": "❌ Nie udało się odczytać paragonu. Wyślij wyraźniejsze zdjęcie.",
        "receipt_header": "🧾 *Paragon zeskanowany pomyślnie!*",
        "total": "💰 Suma",
        "participants": "👥 Uczestnicy",
        "tip": "☕️ Napiwek",
        "lang_currency": "🌐 Język: {lang} | 💵 Waluta: {curr}",
        "select_below": "Wybierz pozycje poniżej lub zmień ustawienia:",
        "btn_lang": "🌐 Język",
        "btn_currency": "💵 Waluta",
        "btn_people": "👥 Osoby",
        "btn_tip": "☕️ Napiwek",
        "btn_payer": "💳 Kto zapłacił?",
        "btn_calc": "✅ Oblicz podział",
        "selected": "Wybrano",
        "final_title": "📊 *KOŃCOWY PODZIAŁ RACHUNKU*",
        "paid_by": "Zapłacił",
        "tip_included": "Napiwek wliczony",
        "to_pay": "Do zapłaty {payer}",
        "total_split": "Suma podziału",
        "share": "1/{n}",
        "tip_line": "Napiwek ({pct}%)",
        "choose_payer": "💳 Wybierz kto zapłacił za wszystko:",
        "choose_people": "👥 Ile osób? (dla nieprzypisanych pozycji)",
        "choose_tip": "☕️ Wybierz procent napiwku:",
        "choose_lang": "🌐 Wybierz język:",
        "choose_currency": "💵 Wybierz walutę:",
        "payer_not_set": "nie ustawiono",
        "back": "⬅️ Wstecz",
        "split_mode_on": "📷 Tryb dzielenia WŁĄCZONY. Wyślij teraz zdjęcie paragonu.",
        "split_mode_on_group": (
            "📷 Tryb dzielenia WŁĄCZONY w tym czacie. "
            "Następne zdjęcie zostanie zeskanowane (lub odpowiedz na zdjęcie `/split`)."
        ),
        "lang_set": "✅ Język ustawiony na *{lang}*.",
    },
    "UA": {
        "choose_lang_first": "🌐 Оберіть мову:",
        "welcome": (
            "👋 *Бот для розділення чеку*\n\n"
            "Надішліть фото чеку з підписом `/split` "
            "(або скористайтесь кнопкою нижче), і я допоможу розділити рахунок.\n\n"
            "У групах: згадайте @snapnsplit_bot, використайте `/split`, відповідайте на повідомлення бота "
            "або відповідайте на фото командою `/split`.\n\n"
            "Команди:\n"
            "/start — мова + це повідомлення\n"
            "/split — наступне фото буде відскановано\n"
            "/cancel — скасувати поточний чек\n"
            "/help — довідка"
        ),
        "help": (
            "📷 Щоб відсканувати чек:\n"
            "• Особисто: фото + `/split`, кнопка або /split, потім фото\n"
            "• Група: @snapnsplit_bot, підпис `/split`, відповідь на бота, "
            "/split потім наступне фото, або відповідь на фото з `/split`"
        ),
        "cancelled": "❌ Поточний чек скасовано.",
        "no_receipt": "Немає активного чеку. Спочатку надішліть фото.",
        "processing": "🔍 Сканую чек, зачекайте…",
        "scan_error": "❌ Не вдалося прочитати чек. Надішліть чіткіше фото.",
        "receipt_header": "🧾 *Чек успішно відскановано!*",
        "total": "💰 Загальна сума",
        "participants": "👥 Учасники",
        "tip": "☕️ Чайові",
        "lang_currency": "🌐 Мова: {lang} | 💵 Валюта: {curr}",
        "select_below": "Оберіть позиції нижче або змініть налаштування:",
        "btn_lang": "🌐 Мова",
        "btn_currency": "💵 Валюта",
        "btn_people": "👥 Люди",
        "btn_tip": "☕️ Чайові",
        "btn_payer": "💳 Хто заплатив?",
        "btn_calc": "✅ Розрахувати",
        "selected": "Обрано",
        "final_title": "📊 *ФІНАЛЬНИЙ РОЗДІЛ ЧЕКУ*",
        "paid_by": "Заплатив",
        "tip_included": "Чайові включено",
        "to_pay": "До сплати {payer}",
        "total_split": "Загальна сума",
        "share": "1/{n}",
        "tip_line": "Чайові ({pct}%)",
        "choose_payer": "💳 Оберіть хто заплатив за все:",
        "choose_people": "👥 Скільки людей? (для нерозподілених позицій)",
        "choose_tip": "☕️ Оберіть відсоток чайових:",
        "choose_lang": "🌐 Оберіть мову:",
        "choose_currency": "💵 Оберіть валюту:",
        "payer_not_set": "не встановлено",
        "back": "⬅️ Назад",
        "split_mode_on": "📷 Режим розділення УВІМКНЕНО. Надішліть фото чеку зараз.",
        "split_mode_on_group": (
            "📷 Режим розділення УВІМКНЕНО в цьому чаті. "
            "Наступне фото буде відскановано (або відповідайте на фото `/split`)."
        ),
        "lang_set": "✅ Мову встановлено: *{lang}*.",
    },
    "RU": {
        "choose_lang_first": "🌐 Выберите язык:",
        "welcome": (
            "👋 *Бот для разделения чека*\n\n"
            "Отправьте фото чека с подписью `/split` "
            "(или воспользуйтесь кнопкой ниже), и я помогу разделить счёт.\n\n"
            "В группах: упомяните @snapnsplit_bot, используйте `/split`, ответьте на сообщение бота "
            "или ответьте на фото командой `/split`.\n\n"
            "Команды:\n"
            "/start — язык + это сообщение\n"
            "/split — следующее фото будет отсканировано\n"
            "/cancel — отменить текущий чек\n"
            "/help — справка"
        ),
        "help": (
            "📷 Чтобы отсканировать чек:\n"
            "• В личке: фото + `/split`, кнопка или /split, затем фото\n"
            "• В группе: @snapnsplit_bot, подпись `/split`, ответ на бота, "
            "/split затем следующее фото, или ответ на фото с `/split`"
        ),
        "cancelled": "❌ Текущий чек отменён.",
        "no_receipt": "Нет активного чека. Сначала отправьте фото.",
        "processing": "🔍 Сканирую чек, подождите…",
        "scan_error": "❌ Не удалось прочитать чек. Отправьте более чёткое фото.",
        "receipt_header": "🧾 *Чек успешно отсканирован!*",
        "total": "💰 Итого",
        "participants": "👥 Участники",
        "tip": "☕️ Чаевые",
        "lang_currency": "🌐 Язык: {lang} | 💵 Валюта: {curr}",
        "select_below": "Выберите позиции ниже или измените настройки:",
        "btn_lang": "🌐 Язык",
        "btn_currency": "💵 Валюта",
        "btn_people": "👥 Люди",
        "btn_tip": "☕️ Чаевые",
        "btn_payer": "💳 Кто оплатил?",
        "btn_calc": "✅ Рассчитать",
        "selected": "Выбрано",
        "final_title": "📊 *ИТОГОВОЕ РАЗДЕЛЕНИЕ ЧЕКА*",
        "paid_by": "Оплатил",
        "tip_included": "Чаевые включены",
        "to_pay": "К оплате {payer}",
        "total_split": "Итого разделено",
        "share": "1/{n}",
        "tip_line": "Чаевые ({pct}%)",
        "choose_payer": "💳 Выберите кто оплатил всё:",
        "choose_people": "👥 Сколько человек? (для нераспределённых позиций)",
        "choose_tip": "☕️ Выберите процент чаевых:",
        "choose_lang": "🌐 Выберите язык:",
        "choose_currency": "💵 Выберите валюту:",
        "payer_not_set": "не указано",
        "back": "⬅️ Назад",
        "split_mode_on": "📷 Режим разделения ВКЛЮЧЁН. Отправьте фото чека сейчас.",
        "split_mode_on_group": (
            "📷 Режим разделения ВКЛЮЧЁН в этом чате. "
            "Следующее фото будет отсканировано (или ответьте на фото `/split`)."
        ),
        "lang_set": "✅ Язык установлен: *{lang}*.",
    },
}


def get_lang(chat_id: int) -> str:
    if chat_id in chat_langs:
        return chat_langs[chat_id]
    if chat_id in receipts:
        return receipts[chat_id].get("lang", DEFAULT_LANG)
    return DEFAULT_LANG


def set_lang(chat_id: int, lang: str) -> None:
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    chat_langs[chat_id] = lang
    if chat_id in receipts:
        receipts[chat_id]["lang"] = lang


def t_lang(lang: str, key: str, **kwargs: Any) -> str:
    text = TEXTS.get(lang, TEXTS[DEFAULT_LANG]).get(
        key, TEXTS[DEFAULT_LANG].get(key, key)
    )
    return text.format(**kwargs) if kwargs else text


def t(receipt: dict[str, Any], key: str, **kwargs: Any) -> str:
    return t_lang(receipt.get("lang", DEFAULT_LANG), key, **kwargs)


def user_label(receipt: dict[str, Any], user_id: int) -> str:
    return receipt.get("known_users", {}).get(user_id, f"User{user_id}")


def remember_user(receipt: dict[str, Any], user) -> None:
    if user.username:
        label = f"@{user.username}"
    elif user.first_name:
        label = user.first_name
    else:
        label = f"User{user.id}"
    receipt.setdefault("known_users", {})[user.id] = label


def convert_amount(amount: float, from_cur: str, to_cur: str) -> float:
    if from_cur == to_cur:
        return round(amount, 2)
    in_eur = amount * RATES_TO_EUR.get(from_cur, 1.0)
    rate_to = RATES_TO_EUR.get(to_cur, 1.0)
    return round(in_eur / rate_to, 2) if rate_to else round(amount, 2)


def parse_receipt_json(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def guess_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
        return "image/webp"
    return "image/jpeg"


def _analyze_receipt_sync(image_bytes: bytes) -> dict[str, Any]:
    system_prompt = (
        "Analyze the receipt in the photo. Return STRICTLY a valid JSON object "
        "without markdown or conversational text with this structure:\n"
        "{\n"
        '  "currency_detected": "EUR",\n'
        '  "items": [{"id": 1, "name": "Item name", "price": 100.0}],\n'
        '  "total_sum": 100.0\n'
        "}"
    )
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(
                data=image_bytes,
                mime_type=guess_mime_type(image_bytes),
            ),
            "Analyze this receipt and return the JSON.",
        ],
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
        ),
    )
    content = response.text or ""
    return parse_receipt_json(content)


async def analyze_receipt_image(image_bytes: bytes) -> dict[str, Any]:
    return await asyncio.to_thread(_analyze_receipt_sync, image_bytes)


def compute_split(receipt: dict[str, Any]) -> tuple[dict[int, dict], float]:
    currency = receipt["currency"]
    orig = receipt["original_currency"]
    tips_pct = receipt.get("tips_percent", 0)

    items = []
    for item in receipt["items"]:
        price = convert_amount(float(item["price"]), orig, currency)
        items.append({**item, "price": price})

    user_items: dict[int, list[dict]] = {}
    active_users = list(
        {uid for it in receipt["items"] for uid in it.get("selected_by", [])}
    )

    for item in items:
        selectors = item.get("selected_by", [])
        if selectors:
            share = round(item["price"] / len(selectors), 2)
            for uid in selectors:
                user_items.setdefault(uid, []).append(
                    {
                        "name": item["name"],
                        "amount": share,
                        "share_n": len(selectors),
                    }
                )
        else:
            n = len(active_users) if active_users else receipt.get("num_people", 1)
            share = round(item["price"] / n, 2)
            targets = active_users if active_users else [0]
            for uid in targets:
                user_items.setdefault(uid, []).append(
                    {
                        "name": item["name"],
                        "amount": share,
                        "share_n": n,
                        "unassigned": True,
                    }
                )

    items_subtotal = sum(it["price"] for it in items)
    tip_pool = round(items_subtotal * tips_pct / 100, 2)

    result: dict[int, dict] = {}
    grand = 0.0

    for uid, lines in user_items.items():
        sub = round(sum(line["amount"] for line in lines), 2)
        tip_share = round(sub / items_subtotal * tip_pool, 2) if items_subtotal else 0.0
        total = round(sub + tip_share, 2)
        result[uid] = {"items": lines, "subtotal": sub, "tip": tip_share, "total": total}
        grand += total

    return result, round(grand, 2)


def build_lang_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code in SUPPORTED_LANGS:
        row.append(
            InlineKeyboardButton(LANG_LABELS[code], callback_data=f"setlang:{code}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_menu_keyboard(receipt: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = receipt.get("lang", DEFAULT_LANG)
    cur = receipt.get("currency", DEFAULT_CURRENCY)
    orig = receipt.get("original_currency", DEFAULT_CURRENCY)
    tips = receipt.get("tips_percent", 0)

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t(receipt, "btn_lang") + f": {lang}", callback_data="menu:lang"
            ),
            InlineKeyboardButton(
                t(receipt, "btn_currency") + f": {cur}", callback_data="menu:cur"
            ),
        ],
        [
            InlineKeyboardButton(
                t(receipt, "btn_people") + f" ({receipt.get('num_people', 1)})",
                callback_data="menu:people",
            ),
            InlineKeyboardButton(
                t(receipt, "btn_tip") + f" ({tips}%)", callback_data="menu:tip"
            ),
        ],
        [InlineKeyboardButton(t(receipt, "btn_payer"), callback_data="menu:payer")],
    ]

    for item in receipt["items"]:
        price = convert_amount(float(item["price"]), orig, cur)
        selectors = item.get("selected_by", [])
        if selectors:
            names = ", ".join(user_label(receipt, uid) for uid in selectors[:3])
            if len(selectors) > 3:
                names += "…"
            label = (
                f"✅ {item['name']} ({t(receipt, 'selected')}: {names}) "
                f"— {price:.2f} {cur}"
            )
        else:
            label = f"⬜ {item['name']} — {price:.2f} {cur}"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"item:{item['id']}")]
        )

    rows.append([InlineKeyboardButton(t(receipt, "btn_calc"), callback_data="calc")])
    return InlineKeyboardMarkup(rows)


def build_menu_text(receipt: dict[str, Any]) -> str:
    orig = receipt.get("original_currency", DEFAULT_CURRENCY)
    cur = receipt.get("currency", DEFAULT_CURRENCY)
    total = convert_amount(float(receipt.get("total_sum", 0)), orig, cur)
    tips = receipt.get("tips_percent", 0)
    tips_sum = round(
        sum(convert_amount(float(i["price"]), orig, cur) for i in receipt["items"])
        * tips
        / 100,
        2,
    )
    payer = receipt.get("payer_username") or t(receipt, "payer_not_set")

    return "\n".join(
        [
            t(receipt, "receipt_header"),
            f"{t(receipt, 'total')}: {total:.2f} {cur}",
            f"{t(receipt, 'participants')}: {receipt.get('num_people', 1)}",
            f"{t(receipt, 'tip')}: {tips}% ({tips_sum:.2f} {cur})",
            t(receipt, "lang_currency", lang=receipt.get("lang", DEFAULT_LANG), curr=cur),
            f"💳 {t(receipt, 'paid_by')}: {payer}",
            "",
            t(receipt, "select_below"),
        ]
    )


def build_submenu_keyboard(receipt: dict[str, Any], kind: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if kind == "lang":
        row = []
        for code in SUPPORTED_LANGS:
            row.append(InlineKeyboardButton(LANG_LABELS[code], callback_data=f"lang:{code}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    elif kind == "cur":
        rows.append(
            [
                InlineKeyboardButton(code, callback_data=f"cur:{code}")
                for code in SUPPORTED_CURRENCIES[:3]
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(code, callback_data=f"cur:{code}")
                for code in SUPPORTED_CURRENCIES[3:]
            ]
        )
    elif kind == "people":
        rows = [
            [
                InlineKeyboardButton(str(n), callback_data=f"people:{n}")
                for n in range(1, 6)
            ],
            [
                InlineKeyboardButton(str(n), callback_data=f"people:{n}")
                for n in range(6, 11)
            ],
        ]
    elif kind == "tip":
        rows.append(
            [InlineKeyboardButton(f"{p}%", callback_data=f"tip:{p}") for p in TIP_OPTIONS]
        )
    elif kind == "payer":
        users = receipt.get("known_users", {})
        if users:
            for uid, label in users.items():
                rows.append(
                    [InlineKeyboardButton(label, callback_data=f"payer:{uid}")]
                )
        else:
            rows.append([InlineKeyboardButton("—", callback_data="noop")])

    rows.append([InlineKeyboardButton(t(receipt, "back"), callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _reply_keyboard_for_lang(lang: str) -> ReplyKeyboardMarkup:
    label = SPLIT_MODE_BUTTONS.get(lang, SPLIT_MODE_BUTTONS["EN"])
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _caption_has_split(caption: str | None) -> bool:
    if not caption:
        return False
    return bool(re.search(r"(?i)(?:^|\s)/split(?:@\w+)?(?:\s|$)", caption))


def _message_mentions_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if not msg:
        return False
    bot_username = (context.bot.username or BOT_USERNAME).lower()

    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    text = (msg.text or "") + "\n" + (msg.caption or "")
    for ent in entities:
        if ent.type == "mention":
            mention = text[ent.offset : ent.offset + ent.length].lower()
            if mention == f"@{bot_username}":
                return True
        elif ent.type == "text_mention" and ent.user and ent.user.is_bot:
            if ent.user.id == context.bot.id:
                return True
    return f"@{bot_username}" in text.lower()


def _is_reply_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return False
    replied = msg.reply_to_message
    if replied.from_user and replied.from_user.id == context.bot.id:
        return True
    return False


def should_process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return False

    caption = msg.caption or ""

    if _caption_has_split(caption):
        return True

    if chat.type == ChatType.PRIVATE:
        if user.id in split_mode_users:
            return True
        if caption.strip() in SPLIT_MODE_BUTTON_SET:
            return True
        return False

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if chat.id in split_mode_chats:
            return True
        if _message_mentions_bot(update, context):
            return True
        if _is_reply_to_bot(update, context):
            return True
        return False

    return False


async def process_photo_bytes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    image_bytes: bytes,
    status_message,
) -> None:
    """Shared path: Gemini OCR → build receipt menu."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = get_lang(chat_id)

    try:
        data = await analyze_receipt_image(image_bytes)

        detected_cur = str(data.get("currency_detected", DEFAULT_CURRENCY)).upper()
        if detected_cur not in SUPPORTED_CURRENCIES:
            detected_cur = DEFAULT_CURRENCY

        items = []
        for i, raw in enumerate(data.get("items", []), start=1):
            items.append(
                {
                    "id": int(raw.get("id", i)),
                    "name": str(raw.get("name", f"Item {i}")),
                    "price": float(raw.get("price", 0)),
                    "selected_by": [],
                }
            )

        if not items:
            raise ValueError("No items found on receipt")

        receipt: dict[str, Any] = {
            "items": items,
            "original_currency": detected_cur,
            "currency": detected_cur,
            "total_sum": float(
                data.get("total_sum", sum(item["price"] for item in items))
            ),
            "tips_percent": 0,
            "payer_id": None,
            "payer_username": None,
            "lang": lang,
            "num_people": 1,
            "menu_message_id": None,
            "known_users": {},
        }
        remember_user(receipt, user)
        receipts[chat_id] = receipt

        menu_msg = await update.effective_message.reply_text(
            build_menu_text(receipt),
            reply_markup=build_menu_keyboard(receipt),
            parse_mode=ParseMode.MARKDOWN,
        )
        receipt["menu_message_id"] = menu_msg.message_id
        await status_message.delete()

    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.exception("Receipt parse error: %s", exc)
        await status_message.edit_text(t_lang(lang, "scan_error"))
    except Exception as exc:
        logger.exception("Gemini/receipt error: %s", exc)
        await status_message.edit_text(t_lang(lang, "scan_error"))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Always ask for language first, then show welcome in that language."""
    # Multilingual prompt (no language chosen yet)
    await update.message.reply_text(
        "🌐 Please choose your language / Bitte Sprache wählen / "
        "Wybierz język / Оберіть мову / Выберите язык:",
        reply_markup=build_lang_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    lang = get_lang(chat_id)
    await update.message.reply_text(t_lang(lang, "help"))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = get_lang(chat_id)
    receipts.pop(chat_id, None)
    if user:
        split_mode_users.discard(user.id)
    split_mode_chats.discard(chat_id)
    await update.message.reply_text(t_lang(lang, "cancelled"))


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /split:
    - If reply to a photo → scan that photo immediately
    - Private → enable one-shot mode for next photo
    - Group → enable one-shot mode for next photo in this chat
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    lang = get_lang(chat.id)

    # Reply to a photo with /split → scan that photo
    replied = msg.reply_to_message
    if replied and replied.photo:
        status = await msg.reply_text(t_lang(lang, "processing"))
        try:
            photo = replied.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            await process_photo_bytes(update, context, buf.getvalue(), status)
        except Exception as exc:
            logger.exception("Reply-/split photo error: %s", exc)
            await status.edit_text(t_lang(lang, "scan_error"))
        return

    if chat.type == ChatType.PRIVATE:
        split_mode_users.add(user.id)
        await msg.reply_text(
            t_lang(lang, "split_mode_on"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_reply_keyboard_for_lang(lang),
        )
    else:
        split_mode_chats.add(chat.id)
        await msg.reply_text(
            t_lang(lang, "split_mode_on_group"),
            parse_mode=ParseMode.MARKDOWN,
        )


async def handle_split_mode_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type != ChatType.PRIVATE:
        return
    text = (update.message.text or "").strip()
    if text not in SPLIT_MODE_BUTTON_SET:
        return

    lang = get_lang(chat.id)
    for code, label in SPLIT_MODE_BUTTONS.items():
        if label == text:
            lang = code
            set_lang(chat.id, lang)
            break

    split_mode_users.add(user.id)
    await update.message.reply_text(
        t_lang(lang, "split_mode_on"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_reply_keyboard_for_lang(lang),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = get_lang(chat_id)

    if not should_process_photo(update, context):
        return

    if user:
        split_mode_users.discard(user.id)
    split_mode_chats.discard(chat_id)

    status = await update.message.reply_text(t_lang(lang, "processing"))

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        await process_photo_bytes(update, context, buf.getvalue(), status)
    except Exception as exc:
        logger.exception("Photo download/process error: %s", exc)
        await status.edit_text(t_lang(lang, "scan_error"))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    data = query.data or ""

    # Language pick from /start (before any receipt)
    if data.startswith("setlang:"):
        lang = data.split(":")[1]
        if lang not in SUPPORTED_LANGS:
            lang = DEFAULT_LANG
        set_lang(chat_id, lang)
        keyboard = None
        if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
            keyboard = _reply_keyboard_for_lang(lang)
        await query.edit_message_text(
            t_lang(lang, "lang_set", lang=LANG_LABELS.get(lang, lang))
            + "\n\n"
            + t_lang(lang, "welcome"),
            parse_mode=ParseMode.MARKDOWN,
        )
        # ReplyKeyboard can't be set via edit — send a follow-up if private
        if keyboard:
            await context.bot.send_message(
                chat_id=chat_id,
                text=t_lang(lang, "help"),
                reply_markup=keyboard,
            )
        return

    receipt = receipts.get(chat_id)

    if not receipt:
        await query.edit_message_text(t_lang(get_lang(chat_id), "no_receipt"))
        return

    remember_user(receipt, user)

    submenu_map = {
        "menu:lang": ("choose_lang", "lang"),
        "menu:cur": ("choose_currency", "cur"),
        "menu:people": ("choose_people", "people"),
        "menu:tip": ("choose_tip", "tip"),
        "menu:payer": ("choose_payer", "payer"),
    }
    if data in submenu_map:
        text_key, kind = submenu_map[data]
        await query.edit_message_text(
            t(receipt, text_key),
            reply_markup=build_submenu_keyboard(receipt, kind),
        )
        return

    if data == "menu:back":
        await query.edit_message_text(
            build_menu_text(receipt),
            reply_markup=build_menu_keyboard(receipt),
            parse_mode=ParseMode.MARKDOWN,
        )
        receipt["menu_message_id"] = query.message.message_id
        return

    if data.startswith("lang:"):
        new_lang = data.split(":")[1]
        receipt["lang"] = new_lang
        set_lang(chat_id, new_lang)
    elif data.startswith("cur:"):
        receipt["currency"] = data.split(":")[1]
    elif data.startswith("people:"):
        receipt["num_people"] = int(data.split(":")[1])
    elif data.startswith("tip:"):
        receipt["tips_percent"] = int(data.split(":")[1])
    elif data.startswith("payer:"):
        uid = int(data.split(":")[1])
        receipt["payer_id"] = uid
        receipt["payer_username"] = user_label(receipt, uid)
    elif data.startswith("item:"):
        item_id = int(data.split(":")[1])
        for item in receipt["items"]:
            if item["id"] == item_id:
                selected = item.setdefault("selected_by", [])
                if user.id in selected:
                    selected.remove(user.id)
                else:
                    selected.append(user.id)
                break
    elif data == "calc":
        split, grand = compute_split(receipt)
        cur = receipt["currency"]
        orig = receipt["original_currency"]
        receipt_total = convert_amount(float(receipt["total_sum"]), orig, cur)
        payer = receipt.get("payer_username") or t(receipt, "payer_not_set")
        tips = receipt.get("tips_percent", 0)

        lines = [
            t(receipt, "final_title"),
            f"{t(receipt, 'paid_by')}: {payer}",
            f"{t(receipt, 'tip_included')}: {tips}%",
            "",
            "──────────────────────",
        ]

        for uid, info in split.items():
            label = (
                t(receipt, "participants")
                if uid == 0
                else user_label(receipt, uid)
            )
            lines.append(f"👤 {label}:")
            for it in info["items"]:
                share_txt = ""
                if it.get("share_n", 1) > 1:
                    share_txt = f" ({t(receipt, 'share', n=it['share_n'])})"
                lines.append(f"  • {it['name']}{share_txt}: {it['amount']:.2f} {cur}")
            if tips:
                lines.append(
                    f"  • {t(receipt, 'tip_line', pct=tips)}: {info['tip']:.2f} {cur}"
                )
            lines.append(
                f"  👉 *{t(receipt, 'to_pay', payer=payer)}: {info['total']:.2f} {cur}*"
            )
            lines.append("")

        lines.extend(
            [
                "──────────────────────",
                f"{t(receipt, 'total_split')}: {grand:.2f} / {receipt_total:.2f} {cur}",
            ]
        )

        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return
    else:
        return

    await query.edit_message_text(
        build_menu_text(receipt),
        reply_markup=build_menu_keyboard(receipt),
        parse_mode=ParseMode.MARKDOWN,
    )
    receipt["menu_message_id"] = query.message.message_id


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(
                r"^("
                + "|".join(re.escape(b) for b in SPLIT_MODE_BUTTON_SET)
                + r")$"
            ),
            handle_split_mode_button,
        )
    )
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Receipt Split Bot (@%s) started.", BOT_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
