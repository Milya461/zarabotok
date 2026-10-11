import re
import calendar
from datetime import datetime, timezone, timedelta
from js import fetch, Response, JSON


def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=3)


async def send_message(env, chat_id, text):
    token = env.TELEGRAM_BOT_TOKEN
    await fetch(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": JSON.stringify({
                "chat_id": chat_id,
                "text": text
            })
        }
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


def parse_services(text):
    items = []

    for line in text.splitlines():
        for part in re.split(r"[,;]", line):
            part = part.strip()
            match = re.search(r"(\d[\d ]*(?:[.,]\d{1,2})?)\s*$", part)

            if not match:
                continue

            amount_text = match.group(1).replace(" ", "").replace(",", ".")
            amount = float(amount_text)
            description = part[:match.start()].strip()

            if not description or amount <= 0:
                continue

            name = description.lower()

            if re.search(r"когт|когот", name):
                rate = 100
            elif re.search(r"парааналь|параанал|желез", name):
                rate = 100
            elif re.search(r"чаев|чаевые|типс", name):
                rate = 100
            else:
                rate = 35

            earned = round(amount * rate / 100, 2)
            items.append((description, amount, earned, rate))

    return items


async def totals(env, chat_id, start, end):
    result = await env.DB.prepare("""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(amount), 0) AS amount,
               COALESCE(SUM(earned), 0) AS earned
        FROM services
        WHERE chat_id = ? AND service_date >= ? AND service_date <= ?
    """).bind(str(chat_id), start, end).all()

    row = result.results[0]
    return int(row["cnt"]), float(row["amount"]), float(row["earned"])


async def handle_message(env, message):
    chat_id = str(message["chat"]["id"])
    text = message.get("text", "").strip()

    if not text:
        return

    today = now_local().date()
    today_s = today.isoformat()
    month_start = today.replace(day=1).isoformat()
    month_end = today.replace(
        day=calendar.monthrange(today.year, today.month)[1]
    ).isoformat()

    if text.startswith("/start") or text.startswith("/help"):
        await send_message(env, chat_id,
            "Учёт заработка грумера 🐾\n\n"
            "Присылай услуги с ценами, например:\n"
            "Шпиц стрижка 3200\n"
            "Когти 300\n"
            "Уши 300, зубы 300\n"
            "Чаевые 500\n\n"
            "/today — заработок за сегодня\n"
            "/month — заработок за месяц\n"
            "/period — текущий зарплатный период\n"
            "/pay 15000 — записать полученную выплату\n"
            "/help — помощь\n\n"
            "Стрижка, колтуны, уши и зубы: 35%.\n"
            "Отдельные когти, параанальные железы и чаевые: 100%."
        )
        return

    if text.startswith("/today"):
        count, amount, earned = await totals(
            env, chat_id, today_s, today_s
        )
        await send_message(env, chat_id,
            f"Сегодня 🐾\nЗаписей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/month"):
        count, amount, earned = await totals(
            env, chat_id, month_start, month_end
        )
        await send_message(env, chat_id,
            f"Месяц: {today.strftime('%m.%Y')}\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/period"):
        if today.day <= 15:
            start = today.replace(day=1)
            end = today.replace(day=15)
        else:
            start = today.replace(day=16)
            end = today.replace(
                day=calendar.monthrange(today.year, today.month)[1]
            )

        count, amount, earned = await totals(
            env, chat_id, start.isoformat(), end.isoformat()
        )
        await send_message(env, chat_id,
            f"Период: {start:%d.%m}–{end:%d.%m}\n"
            f"Записей услуг: {count}\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/pay"):
        match = re.search(r"/pay\s+(\d+(?:[.,]\d{1,2})?)", text)
        if not match:
            await send_message(env, chat_id,
                "Укажи сумму, например: /pay 15000"
            )
            return

        amount = float(match.group(1).replace(",", "."))

        if today.day <= 15:
            start = today.replace(day=1)
            end = today.replace(day=15)
        else:
            start = today.replace(day=16)
            end = today.replace(
                day=calendar.monthrange(today.year, today.month)[1]
            )

        await env.DB.prepare("""
            INSERT INTO payments
            (chat_id, payment_date, amount, period_start, period_end)
            VALUES (?, ?, ?, ?, ?)
        """).bind(
            chat_id, today_s, amount,
            start.isoformat(), end.isoformat()
        ).run()

        await send_message(env, chat_id,
            f"Выплата записана: {amount:.2f} ₽.\n"
            f"Период: {start:%d.%m}–{end:%d.%m}."
        )
        return

    items = parse_services(text)

    if not items:
        await send_message(env, chat_id,
            "Не смогла распознать услуги и цены.\n"
            "Пример: Шпиц стрижка 3200"
        )
        return

    lines = []
    total_earned = 0

    for description, amount, earned, rate in items:
        await env.DB.prepare("""
            INSERT INTO services
            (chat_id, service_date, description, amount, earned, rate)
            VALUES (?, ?, ?, ?, ?, ?)
        """).bind(
            chat_id, today_s, description, amount, earned, rate
        ).run()

        total_earned += earned
        lines.append(
            f"{description}: {amount:.2f} ₽ → "
            f"{earned:.2f} ₽ ({rate}%)"
        )

    _, _, daily_earned = await totals(env, chat_id, today_s, today_s)

    await send_message(env, chat_id,
        "Записала 🐾\n\n" + "\n".join(lines) +
        f"\n\nЗаработок с этой записи: {total_earned:.2f} ₽"
        f"\nВсего за сегодня: {daily_earned:.2f} ₽"
    )


async def on_fetch(request, env):
    if request.method != "POST":
        return Response.new("Zarabotok bot is running")

    try:
        await db_init(env)
        update = await request.json()
        message = update.get("message")

        if message:
            await handle_message(env, message)

        return Response.new("OK")
    except Exception as exc:
        print(str(exc))
        return Response.new("Internal error", {"status": 500})
