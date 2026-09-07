# SnapNSplit (`receipt-split-bot`

A lightweight, 24/7 Telegram bot built with **Python** and **Google Gemini AI** to effortlessly parse paper receipts and calculate shared expenses among groups.

## Core Features

-  **Smart On-Demand OCR:** Processes receipt images using `gemini-3.6-flash`. Ignores general chat photos and triggers parsing **only** when requested (via `/split`, direct mentions, or message replies) to conserve API usage.
-  **Multi-Currency Support:** Native parsing and formatting for standard and regional currencies, including **USD ($)**, **EUR (€)**, **RUB (₽)**, **UAH (₴)**, and **MNT (₮)**.
-  **Instant Language Switching:** Seamlessly toggle interface languages on the fly without interrupting active splits.
-  **Fair Bill Splitting:** Automatically extracts items, tax, and totals, allowing group members to split custom items or calculate equal shares instantly.


## Tech Stack

- **Language:** Python 3.13
- **Bot Framework:** `python-telegram-bot`
- **Vision & AI:** `google-genai`
- **Environment Handling:** `python-dotenv`
- **Deployment Target:** PythonAnywhere / Linux VPS (`nohup` Background Worker)


## Repository Structure

```text
receipt-split-bot/
├── main.py              # Application entry point & Telegram handlers
├── requirements.txt     # Third-party dependencies
├── .env                 # API tokens & secrets (git-ignored)
└── .gitignore           # Ignored files (venv, logs, cache)
