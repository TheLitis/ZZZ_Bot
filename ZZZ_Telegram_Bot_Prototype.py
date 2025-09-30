"""
ZZZ Telegram Bot — compatibility layer for aiogram v2 and v3 + polling fallback

This file is a drop-in replacement that fixes the AttributeError: 'Dispatcher' object has no attribute 'message_handler'
by supporting multiple aiogram versions. It follows this strategy:

1. If aiogram is not installed or BOT_TOKEN is missing -> run offline tests or polling fallback.
2. If aiogram is installed, detect whether it's v2-style or v3-style API:
   - v2-style: Dispatcher has method `message_handler` or `register_message_handler` -> use decorator approach
   - v3-style: use `Router()` and `router.message.register(...)` to register handlers, then include router into Dispatcher.
3. If aiogram is installed but the specific API calls fail, fall back to the simple polling implementation.

This file preserves the same command logic as before (/start, /linkuid, /profile, /daily, /create_raid, /join, /export_raids)

Run: set BOT_TOKEN in .env for real bot, otherwise runs offline tests.
"""

import os
import logging
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_TG_ID = int(os.getenv("OWNER_TG_ID") or 0)
INTERKNOT_API_URL = os.getenv("INTERKNOT_API_URL")
INTERKNOT_API_KEY = os.getenv("INTERKNOT_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database (same as before) ---
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    func,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()
engine = create_engine(
    "sqlite:///zzz_bot.db", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    nick = Column(String, nullable=True)
    uid = Column(String, nullable=True)
    crystals = Column(Integer, default=0)
    last_daily = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=func.now())


class Raid(Base):
    __tablename__ = "raids"
    id = Column(Integer, primary_key=True)
    boss = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    slots = Column(Integer, nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"))
    creator = relationship("User")
    created_at = Column(DateTime, default=func.now())


class RaidParticipant(Base):
    __tablename__ = "raid_participants"
    id = Column(Integer, primary_key=True)
    raid_id = Column(Integer, ForeignKey("raids.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    joined_at = Column(DateTime, default=func.now())


Base.metadata.create_all(engine)

# --- Helpers and core logic (copied + reused) ---


def get_user_by_tg(session, tg_id: int) -> Optional[User]:
    return session.query(User).filter(User.tg_id == tg_id).first()


def _to_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_interknot_profile(uid: str) -> Optional[dict]:
    if not uid:
        return None
    if INTERKNOT_API_URL:
        api_url = INTERKNOT_API_URL.rstrip("/") + "/api/profile"
        params = {"uid": uid}
        headers = {"User-Agent": "ZZZ-TG-Bot/compat"}
        if INTERKNOT_API_KEY:
            headers["Authorization"] = f"Bearer {INTERKNOT_API_KEY}"
        try:
            r = requests.get(api_url, params=params, headers=headers, timeout=6)
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return {"raw_text": r.text[:300]}
        except requests.RequestException as e:
            logger.warning("Interknot request failed: %s", e)
            return None
    logger.info("Returning mock profile for UID %s", uid)
    return {
        "uid": uid,
        "nickname": f"MockPlayer_{uid}",
        "level": 42,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


def ensure_user(session, tg_id: int, nick: Optional[str] = None) -> User:
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id, nick=nick)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def cmd_start_logic(tg_id: int, username: Optional[str]) -> str:
    session = SessionLocal()
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id, nick=username)
        session.add(user)
        session.commit()
        session.refresh(user)
        session.close()
        return "Привет! Ты зарегистрирован. Привяжи UID: /linkuid <UID>"
    session.close()
    return "/profile чтобы посмотреть профиль"


def cmd_linkuid_logic(tg_id: int, uid: str) -> str:
    session = SessionLocal()
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id, uid=uid)
        session.add(user)
    else:
        user.uid = uid
    session.commit()
    session.close()
    return f"UID {uid} привязан"


def cmd_profile_logic(tg_id: int) -> str:
    session = SessionLocal()
    user = get_user_by_tg(session, tg_id)
    if not user:
        session.close()
        return "Не зарегистрированы. /start"
    lines = [f"Профиль @{user.nick}", f"Кристаллы: {user.crystals}"]
    if user.uid:
        lines.append(f"UID: {user.uid}")
        profile = fetch_interknot_profile(user.uid)
        if profile:
            if "nickname" in profile:
                lines.append(f"Игровой ник: {profile.get('nickname')}")
            if "level" in profile:
                lines.append(f"Уровень: {profile.get('level')}")
        else:
            lines.append("(Не удалось получить данные с внешнего сервиса)")
    else:
        lines.append("UID не привязан. /linkuid <UID>")
    session.close()
    return "\n".join(lines)


def cmd_daily_logic(tg_id: int) -> str:
    session = SessionLocal()
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id)
        session.add(user)
        session.commit()
    now = datetime.now(timezone.utc)
    last = _to_aware(user.last_daily)
    if last and (now - last) < timedelta(hours=24):
        remaining = timedelta(hours=24) - (now - last)
        hh, rem = divmod(int(remaining.total_seconds()), 3600)
        mm, ss = divmod(rem, 60)
        session.close()
        return f"Ежедневный бонус уже взят. Следующий: {hh:02d}:{mm:02d}:{ss:02d}"
    user.crystals = (user.crystals or 0) + 50
    user.last_daily = now
    session.commit()
    crystals = user.crystals
    session.close()
    return f"Хвостик вручает тебе 50 кристаллов! Сейчас: {crystals} кристаллов. Следующий бесплатный бонус через 24:00:00."


def cmd_create_raid_logic(tg_id: int, boss: str, dt: datetime, slots: int) -> str:
    session = SessionLocal()
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id)
        session.add(user)
        session.commit()
    raid = Raid(boss=boss, start_time=dt, slots=slots, creator_id=user.id)
    session.add(raid)
    session.commit()
    rid = raid.id
    session.close()
    return f"Рейд создан: ID {rid}. {boss} в {dt.isoformat()} (UTC). Слотов: {slots}. /join {rid}"


def cmd_join_logic(tg_id: int, raid_id: int) -> str:
    session = SessionLocal()
    raid = session.query(Raid).filter(Raid.id == raid_id).first()
    if not raid:
        session.close()
        return "Рейд не найден."
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id)
        session.add(user)
        session.commit()
    count = (
        session.query(RaidParticipant)
        .filter(RaidParticipant.raid_id == raid.id)
        .count()
    )
    if count >= raid.slots:
        session.close()
        return "Все слоты заняты."
    exists = (
        session.query(RaidParticipant)
        .filter(RaidParticipant.raid_id == raid.id, RaidParticipant.user_id == user.id)
        .first()
    )
    if exists:
        session.close()
        return "Вы уже записаны."
    rp = RaidParticipant(raid_id=raid.id, user_id=user.id)
    session.add(rp)
    session.commit()
    session.close()
    return f"Вы записаны на рейд {raid.id} — {raid.boss} в {raid.start_time.isoformat()} (UTC)."


def cmd_export_raids_logic(tg_id: int) -> str:
    if OWNER_TG_ID and tg_id != OWNER_TG_ID:
        return "Только владелец может использовать эту команду."
    session = SessionLocal()
    raids = session.query(Raid).all()
    lines: List[str] = []
    for r in raids:
        participants = (
            session.query(RaidParticipant).filter(RaidParticipant.raid_id == r.id).all()
        )
        lines.append(
            f"{r.id}\t{r.boss}\t{r.start_time.isoformat()}\t{r.slots}\t{len(participants)}"
        )
    session.close()
    return "ID\tBoss\tStart\tSlots\tParticipants\n" + "\n".join(lines)


# --- Polling fallback (same as v4) ---


def send_message_via_api(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.warning("sendMessage failed: %s", e)
        return False


def run_simple_polling():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is required for polling mode")
        return
    logger.info("Starting simple polling fallback (long polling via getUpdates)")
    offset = None
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    while True:
        try:
            params = {"timeout": 30, "limit": 20}
            if offset:
                params["offset"] = offset
            r = requests.get(api_base + "/getUpdates", params=params, timeout=40)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                time.sleep(1)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "message" not in upd:
                    continue
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                from_id = msg["from"]["id"]
                username = msg["from"].get("username")
                text = msg.get("text", "")
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].lstrip("/").split("@")[0].lower()
                args = parts[1:]
                try:
                    if cmd == "start":
                        resp = cmd_start_logic(from_id, username)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "linkuid" and args:
                        resp = cmd_linkuid_logic(from_id, args[0])
                        send_message_via_api(chat_id, resp)
                    elif cmd == "profile":
                        resp = cmd_profile_logic(from_id)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "daily":
                        resp = cmd_daily_logic(from_id)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "create_raid" and len(args) >= 3:
                        boss = args[0]
                        date = args[1]
                        time_part = args[2]
                        slots = int(args[3]) if len(args) >= 4 else 5
                        try:
                            dt = datetime.fromisoformat(date + "T" + time_part)
                            dt = dt.replace(tzinfo=timezone.utc)
                            resp = cmd_create_raid_logic(from_id, boss, dt, slots)
                        except Exception:
                            resp = 'Неверный формат даты/времени. Используйте: /create_raid "Boss" YYYY-MM-DD HH:MM [slots]'
                        send_message_via_api(chat_id, resp)
                    elif cmd == "join" and args:
                        try:
                            rid = int(args[0])
                            resp = cmd_join_logic(from_id, rid)
                        except ValueError:
                            resp = "ID рейда должен быть числом."
                        send_message_via_api(chat_id, resp)
                    elif cmd == "export_raids":
                        resp = cmd_export_raids_logic(from_id)
                        send_message_via_api(chat_id, resp)
                    else:
                        send_message_via_api(
                            chat_id, "Неизвестная команда или неверные аргументы."
                        )
                except Exception as e:
                    logger.exception("Error handling command: %s", e)
                    send_message_via_api(chat_id, "Ошибка при обработке команды.")
        except requests.RequestException as e:
            logger.warning("getUpdates failed: %s", e)
            time.sleep(3)
        except Exception as e:
            logger.exception("Unexpected error in polling loop: %s", e)
            time.sleep(3)


# --- aiogram compatibility chooser: register handlers for v2 or v3 ---
try:
    import aiogram

    aiogram_version = getattr(aiogram, "__version__", "unknown")
    logger.info("aiogram version: %s", aiogram_version)
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set — skipping aiogram run")

    from aiogram import Bot, Dispatcher
    from aiogram import types as aiotypes

    # Try v2-style Dispatcher with message_handler decorator
    try:
        dp = Dispatcher(Bot(token=BOT_TOKEN))
        v2_like = hasattr(dp, "message_handler") or hasattr(
            dp, "register_message_handler"
        )
    except Exception:
        try:
            dp = Dispatcher()
            v2_like = hasattr(dp, "message_handler") or hasattr(
                dp, "register_message_handler"
            )
        except Exception:
            dp = None
            v2_like = False

    if dp and v2_like:
        logger.info(
            "Detected aiogram v2-like API. Registering handlers with decorators."
        )

        @dp.message_handler(commands=["start"])
        async def _h_start(message: aiotypes.Message):
            text = cmd_start_logic(message.from_user.id, message.from_user.username)
            await message.reply(text)

        @dp.message_handler(commands=["linkuid"])
        async def _h_linkuid(message: aiotypes.Message):
            args = message.get_args().strip()
            if not args:
                await message.reply("Использование: /linkuid <UID>")
                return
            resp = cmd_linkuid_logic(message.from_user.id, args.split()[0])
            await message.reply(resp)

        @dp.message_handler(commands=["profile"])
        async def _h_profile(message: aiotypes.Message):
            resp = cmd_profile_logic(message.from_user.id)
            await message.reply(resp)

        @dp.message_handler(commands=["daily"])
        async def _h_daily(message: aiotypes.Message):
            resp = cmd_daily_logic(message.from_user.id)
            await message.reply(resp)

        @dp.message_handler(commands=["create_raid"])
        async def _h_create_raid(message: aiotypes.Message):
            import shlex

            args = shlex.split(message.get_args())
            if len(args) < 3:
                await message.reply(
                    'Использование: /create_raid "Boss" YYYY-MM-DD HH:MM [slots]'
                )
                return
            boss = args[0]
            date = args[1]
            time_part = args[2]
            slots = int(args[3]) if len(args) >= 4 else 5
            try:
                dt = datetime.fromisoformat(date + "T" + time_part)
                dt = dt.replace(tzinfo=timezone.utc)
                resp = cmd_create_raid_logic(message.from_user.id, boss, dt, slots)
            except Exception:
                resp = "Неверный формат даты/времени."
            await message.reply(resp)

        @dp.message_handler(commands=["join"])
        async def _h_join(message: aiotypes.Message):
            args = message.get_args().strip().split()
            if not args:
                await message.reply("Использование: /join <raid_id>")
                return
            try:
                rid = int(args[0])
                resp = cmd_join_logic(message.from_user.id, rid)
            except ValueError:
                resp = "ID рейда должен быть числом."
            await message.reply(resp)

        @dp.message_handler(commands=["export_raids"])
        async def _h_export(message: aiotypes.Message):
            resp = cmd_export_raids_logic(message.from_user.id)
            await message.reply(resp)

        # Start polling using aiogram's executor if available, otherwise fallback to dp.start_polling
        try:
            # look for executor in aiogram.utils
            from aiogram.utils import executor

            logger.info("Starting aiogram polling via executor")
            executor.start_polling(dp, skip_updates=True)
        except Exception:
            logger.info("Starting aiogram polling via dp.start_polling")
            asyncio.run(dp.start_polling())

    else:
        # try v3-style: Router
        try:
            from aiogram import Router
            from aiogram.filters import Command
            from aiogram.types import Message

            bot = Bot(token=BOT_TOKEN)
            dp = Dispatcher()
            router = Router()

            async def _reply(msg: Message, text: str):
                await msg.answer(text)

            # register handlers using router
            @router.message(Command("start"))
            async def _r_start(message: Message):
                await _reply(
                    message,
                    cmd_start_logic(message.from_user.id, message.from_user.username),
                )

            @router.message(Command("linkuid"))
            async def _r_linkuid(message: Message):
                args = (message.text or "").split()[1:]
                if not args:
                    await _reply(message, "Использование: /linkuid <UID>")
                    return
                await _reply(message, cmd_linkuid_logic(message.from_user.id, args[0]))

            @router.message(Command("profile"))
            async def _r_profile(message: Message):
                await _reply(message, cmd_profile_logic(message.from_user.id))

            @router.message(Command("daily"))
            async def _r_daily(message: Message):
                await _reply(message, cmd_daily_logic(message.from_user.id))

            @router.message(Command("create_raid"))
            async def _r_create_raid(message: Message):
                import shlex

                args = shlex.split(" ".join((message.text or "").split()[1:]))
                if len(args) < 3:
                    await _reply(
                        message,
                        'Использование: /create_raid "Boss" YYYY-MM-DD HH:MM [slots]',
                    )
                    return
                boss = args[0]
                date = args[1]
                time_part = args[2]
                slots = int(args[3]) if len(args) >= 4 else 5
                try:
                    dt = datetime.fromisoformat(date + "T" + time_part)
                    dt = dt.replace(tzinfo=timezone.utc)
                    await _reply(
                        message,
                        cmd_create_raid_logic(message.from_user.id, boss, dt, slots),
                    )
                except Exception:
                    await _reply(message, "Неверный формат даты/времени.")

            @router.message(Command("join"))
            async def _r_join(message: Message):
                args = (message.text or "").split()[1:]
                if not args:
                    await _reply(message, "Использование: /join <raid_id>")
                    return
                try:
                    rid = int(args[0])
                    await _reply(message, cmd_join_logic(message.from_user.id, rid))
                except ValueError:
                    await _reply(message, "ID рейда должен быть числом.")

            @router.message(Command("export_raids"))
            async def _r_export(message: Message):
                await _reply(message, cmd_export_raids_logic(message.from_user.id))

            dp.include_router(router)
            # start polling
            try:
                from aiogram import executor as aio_executor

                logger.info("Starting aiogram v3 polling via executor")
                aio_executor.start_polling(dp)
            except Exception:
                logger.info("Starting aiogram v3 polling via dp.start_polling")
                asyncio.run(dp.start_polling(bot))

        except Exception as e:
            logger.exception("Failed to register v3 handlers: %s", e)
            logger.info("Falling back to simple polling")
            run_simple_polling()

except Exception as e:
    logger.info(
        "aiogram not available or error checking it (%s) — using polling fallback", e
    )
    if not BOT_TOKEN:
        # offline tests
        print("Running offline tests")
        print("Тестируем ежедневный бонус для пользователя 12345")
        print(cmd_daily_logic(12345))
        print("\nПринудительный сброс и повторный тест (должен выдать кристаллы)")
        session = SessionLocal()
        u = get_user_by_tg(session, 12345)
        if not u:
            u = User(tg_id=12345, nick="user12345")
            session.add(u)
            session.commit()
        u.last_daily = datetime.now(timezone.utc) - timedelta(hours=25)
        session.commit()
        session.close()
        print(cmd_daily_logic(12345))
        print("\nТестирование получения профиля (mock)")
        print(cmd_profile_logic(12345))
    else:
        run_simple_polling()
