import re
import json
import calendar
from datetime import datetime, timezone, timedelta

from js import fetch, Response
from pyodide.ffi import to_js
from workers import WorkerEntrypoint


def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=3)


async def send_message(env, chat_id, text):
    token = env.TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    await fetch(
        url,
        to_js({
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False),
        }),
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
            elif re.search(r"чаев|типс", name):
                rate = 100
            else:
                rate = 35

            earned = round(amount * rate / 100, 2)
            items.append((description, amount, earned, rate))

    return items


async def totals(env, chat_id, start, end):
    result = await env.DB.prepare("""
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(amount), 0) AS amount,
            COALESCE(SUM(earned), 0) AS earned
        FROM services
        WHERE chat_id = ?
          AND service_date >= ?
          AND service_date <= ?
    """).bind(str(chat_id), start, end).all()

    row = result.results[0]
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
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    return start, end


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
        await send_message(
            env,
            chat_id,
            "Учёт заработка грумера 🐾\n\n"
            "Присылай услуги с ценами:\n"
            "Шпиц стрижка 3200\n"
            "Колтуны 500\n"
            "Когти 300\n"
            "Уши 300, зубы 300\n"
            "Чаевые 500\n\n"
            "Команды:\n"
            "/today — заработок за сегодня\n"
            "/month — заработок за месяц\n"
            "/period — текущий зарплатный период\n"
            "/pay 15000 — записать выплату\n"
            "/help — помощь\n\n"
            "Стрижка, колтуны, уши и зубы: 35%.\n"
            "Отдельные когти, параанальные железы "
            "и чаевые: 100%.\n\n"
            "Указывай фактически оплаченную сумму."
        )
        return

    if text.startswith("/today"):
        count, amount, earned = await totals(env, chat_id, today_s, today_s)
        await send_message(
            env,
            chat_id,
            f"Сегодня 🐾\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/month"):
        count, amount, earned = await totals(
            env, chat_id, month_start, month_end
        )
        await send_message(
            env,
            chat_id,
            f"Месяц: {today.strftime('%m.%Y')}\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/period"):
        start, end = current_period(today)
        count, amount, earned = await totals(
            env, chat_id, start.isoformat(), end.isoformat()
        )
        await send_message(
            env,
            chat_id,
            f"Период: {start:%d.%m}–{end:%d.%m}\n"
            f"Записей услуг: {count}\n"
            f"Стоимость услуг: {amount:.2f} ₽\n"
            f"Твой заработок: {earned:.2f} ₽"
        )
        return

    if text.startswith("/pay"):
        match = re.search(r"/pay\s+(\d+(?:[.,]\d{1,2})?)", text)
        if not match:
            await send_message(
                env, chat_id, "Укажи сумму, например: /pay 15000"
            )
            return

        amount = float(match.group(1).replace(",", "."))
        start, end = current_period(today)
        await env.DB.prepare("""
            INSERT INTO payments (
                chat_id, payment_date, amount, period_start, period_end
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
            f"Период: {start:%d.%m}–{end:%d.%m}."
        )
        return

    items = parse_services(text)
    if not items:
        await send_message(
            env,
            chat_id,
            "Не смогла распознать услуги и цены.\n"
            "Пример: Шпиц стрижка 3200"
        )
        return

    lines = []
    total_earned = 0
    for description, amount, earned, rate in items:
        await env.DB.prepare("""
            INSERT INTO services (
                chat_id, service_date, description, amount, earned, rate
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """).bind(
            chat_id,
            today_s,
            description,
            amount,
            earned,
            rate,
        ).run()

        total_earned += earned
        lines.append(
            f"{description}: {amount:.2f} ₽ → {earned:.2f} ₽ ({rate}%)"
        )

    _, _, daily_earned = await totals(env, chat_id, today_s, today_s)
    await send_message(
        env,
        chat_id,
        "Записала 🐾\n\n"
        + "\n".join(lines)
        + f"\n\nЗаработок с этой записи: {total_earned:.2f} ₽"
        + f"\nВсего за сегодня: {daily_earned:.2f} ₽"
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method != "POST":
            return Response.new("Zarabotok bot is running")

        try:
            await db_init(self.env)
            raw_body = await request.text()
            update = json.loads(raw_body)
            message = update.get("message")
            if message:
                await handle_message(self.env, message)
            return Response.new("OK")
        except Exception as exc:
            print(str(exc))
            return Response.new(
                "Internal error",
                to_js({"status": 500})
            )
