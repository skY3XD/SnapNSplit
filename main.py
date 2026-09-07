"""
Receipt Split Telegram Bot (@snapnsplit_bot)
Uses python-telegram-bot v20+ and Google Gemini for receipt OCR.
Default language: EN | Default currency: EUR

- Currencies: EUR, USD, PLN, UAH, RUB, MNT (rates relative to EUR)
- Photos are processed only on explicit request:
  * Private: caption /split, ReplyKeyboard button, or /split mode
  * Group: @snapnsplit_bot mention, /split in caption, or reply to bot message
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

# Users who enabled "split mode" in private chats (next photo will be scanned)
split_mode_users: set[int] = set()

# Fixed exchange rates relative to EUR (replace with live API if needed)
RATES_TO_EUR: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.86,      # ~1 USD ≈ 0.86 EUR
    "PLN": 0.232,     # ~1 PLN ≈ 0.232 EUR
    "UAH": 0.021,     # ~1 UAH ≈ 0.021 EUR
    "RUB": 0.010,     # ~1 RUB ≈ 0.010 EUR
    "MNT": 0.000244,  # ~1 MNT ≈ 0.000244 EUR
}
SUPPORTED_CURRENCIES = ("EUR", "USD", "PLN", "UAH", "RUB", "MNT")
SUPPORTED_LANGS = ("EN", "PL", "UA", "RU")
TIP_OPTIONS = (0, 5, 10, 15)
DEFAULT_CURRENCY = "EUR"

# Reply-keyboard labels for enabling split mode (private chats)
SPLIT_MODE_BUTTONS = {
    "EN": "📷 Split receipt",
    "PL": "📷 Podziel rachunek",
    "UA": "📷 Розділити чек",
    "RU": "📷 Разделить чек",
}
SPLIT_MODE_BUTTON_SET = set(SPLIT_MODE_BUTTONS.values())

TEXTS: dict[str, dict[str, str]] = {
    "EN": {
        "welcome": (
            "👋 *Receipt Split Bot*\n\n"
            "Send a photo of a receipt with the caption `/split` "
            "(or use the button below) and I'll help split the bill among friends.\n\n"
            "In groups: mention @snapnsplit_bot, use `/split`, or reply to a bot message.\n\n"
            "Commands:\n"
            "/start — this message\n"
            "/split — enable split mode for the next photo (private)\n"
            "/cancel — discard current receipt\n"
            "/help — help"
        ),
        "help": (
            "📷 To scan a receipt:\n"
            "• Private: send photo with caption `/split`, press the button, or run /split then send photo\n"
            "• Group: photo + @snapnsplit_bot, caption `/split`, or reply to a bot message"
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
        "split_mode_on": (
            "📷 Split mode ON. Send a receipt photo now "
            "(or send photo with caption `/split`)."
        ),
        "split_mode_off": "Split mode cancelled.",
        "photo_ignored": (
            "Photo ignored. To scan a receipt use `/split` in the caption, "
            "the button below, or enable mode with /split."
        ),
    },
    "PL": {
        "welcome": (
            "👋 *Bot do dzielenia rachunku*\n\n"
            "Wyślij zdjęcie paragonu z podpisem `/split` "
            "(lub użyj przycisku poniżej), a pomogę podzielić rachunek.\n\n"
            "W grupach: wspomnij @snapnsplit_bot, użyj `/split` lub odpowiedz na wiadomość bota.\n\n"
            "Komendy:\n"
            "/start — ta wiadomość\n"
            "/split — włącz tryb dzielenia na następne zdjęcie (prywatnie)\n"
            "/cancel — anuluj bieżący rachunek\n"
            "/help — pomoc"
        ),
        "help": (
            "📷 Aby zeskanować paragon:\n"
            "• Prywatnie: zdjęcie z podpisem `/split`, przycisk lub /split, potem zdjęcie\n"
            "• Grupa: zdjęcie + @snapnsplit_bot, podpis `/split` lub odpowiedź na wiadomość bota"
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
        "split_mode_on": (
            "📷 Tryb dzielenia WŁĄCZONY. Wyślij teraz zdjęcie paragonu "
            "(lub zdjęcie z podpisem `/split`)."
        ),
        "split_mode_off": "Tryb dzielenia wyłączony.",
        "photo_ignored": (
            "Zdjęcie zignorowane. Aby zeskanować paragon użyj `/split` w podpisie, "
            "przycisku poniżej lub włącz tryb komendą /split."
        ),
    },
    "UA": {
        "welcome": (
            "👋 *Бот для розділення чеку*\n\n"
            "Надішліть фото чеку з підписом `/split` "
            "(або скористайтесь кнопкою нижче), і я допоможу розділити рахунок.\n\n"
            "У групах: згадайте @snapnsplit_bot, використайте `/split` або відповідайте на повідомлення бота.\n\n"
            "Команди:\n"
            "/start — це повідомлення\n"
            "/split — увімкнути режим розділення для наступного фото (особисто)\n"
            "/cancel — скасувати поточний чек\n"
            "/help — довідка"
        ),
        "help": (
            "📷 Щоб відсканувати чек:\n"
            "• Особисто: фото з підписом `/split`, кнопка або /split, потім фото\n"
            "• Група: фото + @snapnsplit_bot, підпис `/split` або відповідь на повідомлення бота"
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
        "split_mode_on": (
            "📷 Режим розділення УВІМКНЕНО. Надішліть фото чеку зараз "
            "(або фото з підписом `/split`)."
        ),
        "split_mode_off": "Режим розділення вимкнено.",
        "photo_ignored": (
            "Фото проігноровано. Щоб відсканувати чек використайте `/split` у підписі, "
            "кнопку нижче або увімкніть режим командою /split."
        ),
    },
    "RU": {
        "welcome": (
            "👋 *Бот для разделения чека*\n\n"
            "Отправьте фото чека с подписью `/split` "
            "(или воспользуйтесь кнопкой ниже), и я помогу разделить счёт.\n\n"
            "В группах: упомяните @snapnsplit_bot, используйте `/split` или ответьте на сообщение бота.\n\n"
            "Команды:\n"
            "/start — это сообщение\n"
            "/split — включить режим разделения для следующего фото (в личке)\n"
            "/cancel — отменить текущий чек\n"
            "/help — справка"
        ),
        "help": (
            "📷 Чтобы отсканировать чек:\n"
            "• В личке: фото с подписью `/split`, кнопка или /split, затем фото\n"
            "• В группе: фото + @snapnsplit_bot, подпись `/split` или ответ на сообщение бота"
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
        "split_mode_on": (
            "📷 Режим разделения ВКЛЮЧЁН. Отправьте фото чека сейчас "
            "(или фото с подписью `/split`)."
        ),
        "split_mode_off": "Режим разделения выключен.",
        "photo_ignored": (
            "Фото проигнорировано. Чтобы отсканировать чек используйте `/split` в подписи, "
            "кнопку ниже или включите режим командой /split."
        ),
    },
}


def t(receipt: dict[str, Any], key: str, **kwargs: Any) -> str:
    lang = receipt.get("lang", "EN")
    text = TEXTS.get(lang, TEXTS["EN"]).get(key, TEXTS["EN"].get(key, key))
    return text.format(**kwargs) if kwargs else text


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


def build_menu_keyboard(receipt: dict[str, Any]) -> InlineKeyboardMarkup:
    lang = receipt.get("lang", "EN")
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
            t(receipt, "lang_currency", lang=receipt.get("lang", "EN"), curr=cur),
            f"💳 {t(receipt, 'paid_by')}: {payer}",
            "",
            t(receipt, "select_below"),
        ]
    )


def build_submenu_keyboard(receipt: dict[str, Any], kind: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if kind == "lang":
        rows.append(
            [InlineKeyboardButton(code, callback_data=f"lang:{code}") for code in SUPPORTED_LANGS]
        )
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
        if _message_mentions_bot(update, context):
            return True
        if _is_reply_to_bot(update, context):
            return True
        return False

    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    lang = receipts.get(chat_id, {}).get("lang", "EN")
    keyboard = None
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        keyboard = _reply_keyboard_for_lang(lang)
    await update.message.reply_text(
        TEXTS[lang]["welcome"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    lang = receipts.get(chat_id, {}).get("lang", "EN")
    await update.message.reply_text(TEXTS[lang]["help"])


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = receipts.get(chat_id, {}).get("lang", "EN")
    receipts.pop(chat_id, None)
    if user:
        split_mode_users.discard(user.id)
    await update.message.reply_text(TEXTS[lang]["cancelled"])


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    lang = receipts.get(chat.id, {}).get("lang", "EN")

    if chat.type == ChatType.PRIVATE:
        split_mode_users.add(user.id)
        await update.message.reply_text(
            TEXTS[lang]["split_mode_on"],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_reply_keyboard_for_lang(lang),
        )
    else:
        await update.message.reply_text(
            TEXTS[lang]["help"],
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

    lang = receipts.get(chat.id, {}).get("lang", "EN")
    for code, label in SPLIT_MODE_BUTTONS.items():
        if label == text:
            lang = code
            break

    split_mode_users.add(user.id)
    await update.message.reply_text(
        TEXTS[lang]["split_mode_on"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_reply_keyboard_for_lang(lang),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    lang = receipts.get(chat_id, {}).get("lang", "EN")

    if not should_process_photo(update, context):
        if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
            await update.message.reply_text(
                TEXTS[lang]["photo_ignored"],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_reply_keyboard_for_lang(lang),
            )
        return

    if user:
        split_mode_users.discard(user.id)

    status = await update.message.reply_text(TEXTS[lang]["processing"])

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        data = await analyze_receipt_image(buf.getvalue())

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

        menu_msg = await update.message.reply_text(
            build_menu_text(receipt),
            reply_markup=build_menu_keyboard(receipt),
            parse_mode=ParseMode.MARKDOWN,
        )
        receipt["menu_message_id"] = menu_msg.message_id
        await status.delete()

    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.exception("Receipt parse error: %s", exc)
        await status.edit_text(TEXTS[lang]["scan_error"])
    except Exception as exc:
        logger.exception("Gemini/receipt error: %s", exc)
        await status.edit_text(TEXTS[lang]["scan_error"])


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    receipt = receipts.get(chat_id)

    if not receipt:
        await query.edit_message_text(TEXTS["EN"]["no_receipt"])
        return

    remember_user(receipt, user)
    data = query.data or ""

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
        receipt["lang"] = data.split(":")[1]
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
