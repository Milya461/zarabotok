
import re
import json
import calendar
import traceback
from datetime import datetime, timezone, timedelta

from js import fetch, Response, JSON
from pyodide.ffi import to_js
from workers import WorkerEntrypoint


HELP_TEXT = (
    "Учёт заработка грумера 🐾\n\n"
    "Команды:\n"
    "/today — заработок за сегодня\n"
    "/month — заработок за месяц\n"
    "/period — текущий зарплатный период\n"
    "/pay 15000 — записать выплату\n"
    "/help — помощь"
)


def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=3)


def js_to_python(value):
    """Преобразует JavaScript-объект в обычные Python-данные."""
    return json.loads(str(JSON.stringify(value)))


async def send_message(env, chat_id, text):
    token = env.TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    response = await fetch(
        url,
        to_js({
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False),
        }),
    )

    response_text = await response.text()

    try:
        result = json.loads(response_text)
    except Exception:
        result = {}

    if response.status >= 400 or not result.get("ok", False):
        error_description = result.get(
            "description",
            "Неизвестная ошибка Telegram API",
        )
        raise RuntimeError(
            f"Telegram sendMessage error: HTTP {response.status}; "
            f"{error_description}"
        )


async def db_init(env):
    await env.DB.prepare("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            earned REAL NOT NULL,
            rate INTEGER NOT NULL
        )
    """).run()

    await env.DB.prepare("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            amount REAL NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL
        )
    """).run()

    await env.DB.prepare("""
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id INTEGER PRIMARY KEY,
            processed_at TEXT NOT NULL
        )
    """).run()

    await env.DB.prepare("""
        CREATE TABLE IF NOT EXISTS pending_visits (
            chat_id TEXT PRIMARY KEY,
            service_date TEXT NOT NULL,
            services_json TEXT NOT NULL
        )
    """).run()


async def claim_update(env, update_id):
    result = await env.DB.prepare("""
        INSERT OR IGNORE INTO processed_updates (
            update_id,
            processed_at
        )
        VALUES (?, ?)
    """).bind(
        int(update_id),
        now_local().isoformat(),
    ).run()

    data = js_to_python(result)
    meta = data.get("meta", {})
    changes = meta.get("changes")

    if changes is None:
        raise RuntimeError(
            "Не удалось проверить результат регистрации update_id"
        )

    return int(changes) > 0


def parse_services(text, mode="extra"):
    items = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        parts = re.split(
            r";|,(?=\s*[^\d,;]+?\s+\d)",
            line,
        )

        for part in parts:
            part = part.strip()

            match = re.search(
                r"(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?)?\s*$",
                part,
                re.IGNORECASE,
            )

            if not match:
                continue

            amount_text = (
                match.group(1)
                .replace(" ", "")
                .replace(",", ".")
            )

            try:
                amount = float(amount_text)
            except ValueError:
                continue

            description = part[:match.start()].strip()

            if not description or amount <= 0:
                continue

            name = description.lower()

            if mode == "complex":
                rate = 35
            elif re.search(r"когт|когот", name):
                rate = 100
            elif re.search(r"парааналь|параанал|желез", name):
                rate = 100
            elif re.search(r"чаев|типс", name):
                rate = 100
            else:
                rate = 35

            earned = round(amount * rate / 100, 2)

            items.append({
                "description": description,
                "amount": amount,
                "earned": earned,
                "rate": rate,
            })

    return items


async def get_pending_visit(env, chat_id):
    result = await env.DB.prepare("""
        SELECT service_date, services_json
        FROM pending_visits
        WHERE chat_id = ?
    """).bind(str(chat_id)).first()

    if not result:
        return None

    data = js_to_python(result)

    return {
        "service_date": data["service_date"],
        "items": json.loads(data["services_json"]),
    }


async def save_pending_visit(env, chat_id, service_date, items):
    await env.DB.prepare("""
        INSERT INTO pending_visits (
            chat_id,
            service_date,
            services_json
        )
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            service_date = excluded.service_date,
            services_json = excluded.services_json
    """).bind(
        str(chat_id),
        service_date,
        json.dumps(items, ensure_ascii=False),
    ).run()


async def delete_pending_visit(env, chat_id):
    await env.DB.prepare("""
        DELETE FROM pending_visits
        WHERE chat_id = ?
    """).bind(str(chat_id)).run()


async def insert_service_items(env, chat_id, service_date, items):
    for item in items:
        await env.DB.prepare("""
            INSERT INTO services (
                chat_id,
                service_date,
                description,
                amount,
                earned,
                rate
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """).bind(
            str(chat_id),
            service_date,
            item["description"],
            item["amount"],
            item["earned"],
            item["rate"],
        ).run()


async def totals(env, chat_id, start, end):
    raw_result = await env.DB.prepare("""
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(amount), 0) AS amount,
            COALESCE(SUM(earned), 0) AS earned
        FROM services
        WHERE chat_id = ?
          AND service_date >= ?
          AND service_date <= ?
    """).bind(
        str(chat_id),
        start,
        end,
    ).all()

    result = js_to_python(raw_result)
    row = result["results"][0]

    return (
        int(row["cnt"]),
        float(row["amount"]),
        float(row["earned"]),
    )


def current_period(today):
    if today.day <= 15:
        start = today.replace(day=1)
        end = today.replace(day=15)
    else:
        start = today.replace(day=16)
        end = today.replace(
            day=calendar.monthrange(
                today.year,
                today.month,
            )[1]
        )

    return start, end


async def finish_visit(env, chat_id, service_date, items):
    """Сохраняет визит и показывает итог с датой визита."""
    await insert_service_items(
        env,
        chat_id,
        service_date,
        items,
    )

    await delete_pending_visit(env, chat_id)

    total_earned = round(
        sum(float(item["earned"]) for item in items),
        2,
    )

    lines = [
        f'{item["description"]}: '
        f'{float(item["amount"]):.2f} ₽ → '
        f'{float(item["earned"]):.2f} ₽ '
        f'({int(item["rate"])}%)'
        for item in items
    ]

    _, _, daily_earned = await totals(
        env,
        chat_id,
        service_date,
        service_date,
    )

    # Дата берётся из самого визита, а не из сегодняшнего дня.
    date_display = datetime.strptime(
        service_date,
        "%Y-%m-%d",
    ).strftime("%d.%m.%Y")

    await send_message(
        env,
        chat_id,
        "Записала визит 🐾\n\n"
        + "\n".join(lines)
        + f"\n\nЗаработок с этого визита: "
        f"{total_earned:.2f} ₽"
        + f"\nВсего за день ({date_display}): "
        f"{daily_earned:.2f} ₽",
    )


async def handle_message(env, message):
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return

    today = now_local().date()
    today_s = today.isoformat()

    month_start = today.replace(day=1).isoformat()
    month_end = today.replace(
        day=calendar.monthrange(
            today.year,
            today.month,
        )[1]
    ).isoformat()

    command = text.split()[0].split("@")[0].lower()

    if command in ("/start", "/help"):
        await send_message(env, chat_id, HELP_TEXT)
        return

    # Скрытая команда для установки даты незавершённого визита.
    # Формат: /date 2026-10-11
    if command == "/date":
        match = re.fullmatch(
            r"/date(?:@\w+)?\s+(\d{4}-\d{2}-\d{2})",
            text,
        )

        if not match:
            await send_message(
                env,
                chat_id,
                "Укажи дату в формате ГГГГ-ММ-ДД, например: "
                "/date 2026-10-11",
            )
            return

        try:
            service_date = datetime.strptime(
                match.group(1),
                "%Y-%m-%d",
            ).date().isoformat()
        except ValueError:
            await send_message(
                env,
                chat_id,
                "Не получилось распознать дату. "
                "Используй формат ГГГГ-ММ-ДД.",
            )
            return

        pending = await get_pending_visit(env, chat_id)

        if not pending:
            await send_message(
                env,
                chat_id,
                "Нет незавершённого визита, для которого можно "
                "изменить дату.",
            )
            return

        await save_pending_visit(
            env,
            chat_id,
            service_date,
            pending["items"],
        )

        date_display = datetime.strptime(
            service_date,
            "%Y-%m-%d",
        ).strftime("%d.%m.%Y")

        await send_message(
            env,
            chat_id,
            f"Дата визита установлена: {date_display}.",
        )
        return

    if command == "/today":
        count, amount, earned = await totals(
            env,
            chat_id,
            today_s,
            today_s,
        )

        await send_message(
            env,
            chat_id,
            f"Сегодня 🐾\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽",
        )
        return

    if command == "/month":
        count, amount, earned = await totals(
            env,
            chat_id,
            month_start,
            month_end,
        )

        await send_message(
            env,
            chat_id,
            f"Месяц: {today.strftime('%m.%Y')}\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽",
        )
        return

    if command == "/period":
        start, end = current_period(today)

        count, amount, earned = await totals(
            env,
            chat_id,
            start.isoformat(),
            end.isoformat(),
        )

        await send_message(
            env,
            chat_id,
            f"Период: {start:%d.%m}–{end:%d.%m}\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽",
        )
        return

    if command == "/pay":
        match = re.fullmatch(
            r"/pay(?:@\w+)?\s+(\d+(?:[.,]\d{1,2})?)",
            text,
        )

        if not match:
            await send_message(
                env,
                chat_id,
                "Укажи сумму, например: /pay 15000",
            )
            return

        amount = float(match.group(1).replace(",", "."))

        if amount <= 0:
            await send_message(
                env,
                chat_id,
                "Сумма выплаты должна быть больше нуля.",
            )
            return

        start, end = current_period(today)

        await env.DB.prepare("""
            INSERT INTO payments (
                chat_id,
                payment_date,
                amount,
                period_start,
                period_end
            )
            VALUES (?, ?, ?, ?, ?)
        """).bind(
            chat_id,
            today_s,
            amount,
            start.isoformat(),
            end.isoformat(),
        ).run()

        await send_message(
            env,
            chat_id,
            f"Выплата записана: {amount:.2f} ₽.\n"
            f"Период: {start:%d.%m}–{end:%d.%m}.",
        )
        return

    # Если бот уже ждёт дополнительные услуги для визита.
    pending = await get_pending_visit(env, chat_id)

    if pending:
        if text.lower() in (
            "нет",
            "не было",
            "нету",
            "без доп",
            "без дополнительных",
        ):
            await finish_visit(
                env,
                chat_id,
                pending["service_date"],
                pending["items"],
            )
            return

        extra_items = parse_services(text, mode="extra")

        if not extra_items:
            await send_message(
                env,
                chat_id,
                "Напиши дополнительные услуги с ценами "
                "по одной на строку или ответь «Нет», "
                "если их не было.",
            )
            return

        all_items = pending["items"] + extra_items

        await finish_visit(
            env,
            chat_id,
            pending["service_date"],
            all_items,
        )
        return

    # Первое сообщение: все услуги комплекса и дополнения к нему — 35%.
    items = parse_services(text, mode="complex")

    if not items:
        await send_message(
            env,
            chat_id,
            "Не смогла распознать услуги и цены.\n\n"
            "Отправь услуги комплекса и дополнения к нему, "
            "по одной на строку.\n"
            "Например:\n"
            "Кавалер 3000\n"
            "Зубы 450\n"
            "Парааналки 300",
        )
        return

    await save_pending_visit(
        env,
        chat_id,
        today_s,
        items,
    )

    await send_message(
        env,
        chat_id,
        "Записала услуги комплекса 🐾\n\n"
        "Были дополнительные услуги отдельно от комплекса?\n\n"
        "Если дополнительных услуг не было, ответь «Нет».",
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method != "POST":
            return Response.new("Zarabotok bot is running")

        try:
            raw_body = await request.text()
            update = json.loads(raw_body)

            message = update.get("message")

            if not message:
                return Response.new("OK")

            await db_init(self.env)

            update_id = update.get("update_id")

            if update_id is not None:
                is_new = await claim_update(
                    self.env,
                    update_id,
                )

                if not is_new:
                    return Response.new("OK")

            await handle_message(self.env, message)

            return Response.new("OK")

        except Exception as exc:
            print("WORKER ERROR:", repr(exc))
            print(traceback.format_exc())

            return Response.new(
                "Internal Server Error",
                to_js({"status": 500}),
            )
