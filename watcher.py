# watcher.py
#
# Воркер для Railway:
#   Procfile: worker: python watcher.py
#
# Логика (пока только скелет):
#   - грузим конфиг из CONFIG_JSON или config.json
#   - настраиваем сессию requests
#   - в бесконечном цикле раз в N минут:
#       - (позже) будем парсить FunPay и искать выгодные аккаунты
#       - отправлять сигналы в Telegram

import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Dict

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# ===== 1. Загрузка конфигурации =====

if os.getenv("CONFIG_JSON"):
    config = json.loads(os.getenv("CONFIG_JSON"))
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

TELEGRAM_BOT_TOKEN: str = config["telegram_bot_token"]
TELEGRAM_CHAT_ID: str = config["telegram_chat_id"]

CHECK_INTERVAL_MINUTES: int = int(config.get("check_interval_minutes", 5))
BASE_URL: str = config.get("base_url", "https://funpay.com").rstrip("/")
BRAWL_ACCOUNTS_URL: str = config.get(
    "brawl_accounts_url",
    f"{BASE_URL}/lots/436/"
)

PRICE_RULES: List[Dict] = config.get("price_rules", [])


# ===== 2. Локальное состояние (state.json) =====

def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ===== 3. HTTP и Telegram =====

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
)


def fetch_page(url: str) -> str:
    """
    Скачиваем HTML страницы.
    """
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def send_telegram_message(text: str) -> None:
    """
    Отправка сообщения в Telegram в указанный чат.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


# ===== 4. Структуры данных =====

@dataclass
class Offer:
    offer_id: str              # например "66881057"
    url: str                   # полный URL оффера
    seller_name: str           # имя продавца (опционально, но полезно)
    heroes: Optional[int]      # количество бойцов (может быть None, если пока не знаем)
    price_rub: float           # цена в рублях
    title: str                 # короткое описание
    is_auto: bool              # есть ли автоматическая выдача


# ===== 5. Заглушки логики (дальше будем дописывать) =====

def collect_offers(state: dict) -> List[Offer]:
    """
    Парсим страницу 'Аккаунты Brawl Stars' на FunPay.
    Ищем все <a class="tc-item ...> и достаём:
      - ссылку на оффер
      - ID оффера (из параметра id=)
      - цену в рублях
      - примерное число бойцов (из data-f-hero или текста)
      - имя продавца
      - короткое описание
      - флаг авто-выдачи
    """
    print(f"[INFO] Загружаем список аккаунтов: {BRAWL_ACCOUNTS_URL}")
    try:
        html = fetch_page(BRAWL_ACCOUNTS_URL)
    except Exception as e:
        print(f"[ERROR] Не удалось загрузить страницу аккаунтов: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    offers: List[Offer] = []

    # каждый аккаунт в списке — это <a class="tc-item ...">
    for a in soup.find_all("a", class_="tc-item"):
        href = a.get("href")
        if not href:
            continue

        # Полный URL оффера
        if href.startswith("http"):
            url = href
        else:
            url = BASE_URL.rstrip("/") + "/" + href.lstrip("/")

        # ID оффера достаём из параметра id=...
        offer_id = None
        if "id=" in href:
            part = href.split("id=", 1)[1]
            offer_id = part.split("&")[0]
        if not offer_id:
            offer_id = href  # fallback

        # Цена: в примере она есть в атрибуте data-s у div.tc-price
        price_rub = None
        price_block = a.find("div", class_="tc-price")
        if price_block:
            data_s = price_block.get("data-s")
            if data_s:
                try:
                    price_rub = float(data_s.replace(",", "."))
                except ValueError:
                    price_rub = None
            if price_rub is None:
                # пробуем вытащить число из текста блока
                text_price = price_block.get_text(" ", strip=True).replace(",", ".")
                for token in text_price.split():
                    try:
                        val = float(token)
                        price_rub = val
                        break
                    except ValueError:
                        continue

        if price_rub is None:
            # если цену вообще не поняли — пропускаем оффер
            continue

        # Количество бойцов: сначала смотрим data-f-hero, если есть
        heroes: Optional[int] = None
        data_hero = a.get("data-f-hero")
        if data_hero:
            try:
                heroes = int(data_hero)
            except ValueError:
                heroes = None

        # Если нет data-f-hero — пробуем найти в тексте "14 бравлеров", "14 бойцов" и т.п.
        if heroes is None:
            desc_block = a.find("div", class_="tc-desc-text")
            text = ""
            if desc_block:
                text = desc_block.get_text(" ", strip=True).lower()
            if not text:
                text = a.get_text(" ", strip=True).lower()

            import re
            m = re.search(r"(\d+)\s*(бравлер|бравлеров|бойцов|бойца)", text)
            if m:
                try:
                    heroes = int(m.group(1))
                except ValueError:
                    heroes = None

        # Имя продавца
        seller_name = ""
        user_block = a.find("div", class_="media-user-name")
        if user_block:
            seller_name = user_block.get_text(strip=True)

        # Короткое описание
        title = ""
        desc_text_div = a.find("div", class_="tc-desc-text")
        if desc_text_div:
            title = desc_text_div.get_text(" ", strip=True)

        # Автовыдача: по иконке <i class="auto-dlv-icon">
        is_auto = bool(a.find("i", class_="auto-dlv-icon"))

        offer = Offer(
            offer_id=offer_id,
            url=url,
            seller_name=seller_name,
            heroes=heroes,
            price_rub=price_rub,
            title=title,
            is_auto=is_auto,
        )
        offers.append(offer)

    print(f"[INFO] Распарсено офферов: {len(offers)}")
    return offers


def filter_profitable_offers(offers: List[Offer], state: dict) -> List[Offer]:
    """
    Здесь позже будет логика фильтрации по price_rules и расчёт 'выгодности'.
    Пока просто возвращаем пустой список.
    """
    return []


def send_new_offers_to_telegram(offers: List[Offer], state: dict) -> None:
    """
    Отправляем в Телеграм только новые выгодные офферы.
    Пока просто печатаем размер списка.
    """
    sent_offers: Dict[str, bool] = state.setdefault("sent_offers", {})

    for offer in offers:
        if sent_offers.get(offer.offer_id):
            continue

        text = (
            f"Найден аккаунт:\n"
            f"Бойцов: {offer.heroes if offer.heroes is not None else 'неизвестно'}\n"
            f"Стоимость: {offer.price_rub:.2f} ₽\n"
            f"Ссылка: {offer.url}"
        )

        try:
            print(f"[INFO] Отправляем оффер {offer.offer_id} с ценой {offer.price_rub:.2f} ₽")
            send_telegram_message(text)
            sent_offers[offer.offer_id] = True
            save_state(state)
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")


def run_single_iteration() -> None:
    print("=" * 60)
    print("[INFO] Запуск проверки FunPay Brawl аккаунтов")

    state = load_state()

    offers = collect_offers(state)
    if not offers:
        print("[INFO] Пока нет распарсенных офферов (парсер ещё не дописан).")
        return

    profitable = filter_profitable_offers(offers, state)
    print(f"[INFO] Выгодных офферов найдено: {len(profitable)}")

    if not profitable:
        print("[INFO] Нет выгодных офферов в этой итерации.")
        return

    send_new_offers_to_telegram(profitable, state)


def main_loop() -> None:
    interval_sec = max(1, int(CHECK_INTERVAL_MINUTES * 60))
    print(f"[INFO] Старт главного цикла. Интервал: {CHECK_INTERVAL_MINUTES} минут.")

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            print(f"[FATAL] Необработанное исключение в итерации: {e}")
        print(f"[INFO] Спим {CHECK_INTERVAL_MINUTES} минут...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
