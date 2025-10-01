"""
ZZZ Telegram Bot — Enka integration (fixed)

Single-file runnable prototype.
- Optional enka integration (async) if `enka` package is installed.
- Supports three run modes: aiogram (auto v2/v3), polling fallback (getUpdates), and tests.
- Safe SQLAlchemy session handling.
- /help command included.

Run examples:
  python zzz_bot_enka_fixed.py --mode tests
  python zzz_bot_enka_fixed.py --mode polling
  python zzz_bot_enka_fixed.py --mode aiogram

Set BOT_TOKEN in .env to run Telegram bot.
"""

from __future__ import annotations
import os
import sys
import argparse
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

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("zzz_bot")

# --- Optional enka import ---
HAS_ENKA = False
try:
    import enka  # type: ignore

    HAS_ENKA = True
    _enka_ver = getattr(enka, "__version__", "?")
    logger.info("enka available (ver=%s)", _enka_ver)
except Exception as _e:
    logger.info("enka not available: %s", _e)

# --- Database ---
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

# --- Helpers ---


def get_user_by_tg(session, tg_id: int) -> Optional[User]:
    return session.query(User).filter(User.tg_id == tg_id).first()


def _to_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Help text
HELP_TEXT = (
    "Доступные команды:\n"
    "/start — регистрация\n"
    "/linkuid <UID> — привязать игровой UID\n"
    "/profile — показать профиль (из enka, если доступен)\n"
    "/daily — получить ежедневный бонус (50 кристаллов)\n"
    '/create_raid "Boss" YYYY-MM-DD HH:MM [slots] — создать рейд (UTC)\n'
    "/join <raid_id> — записаться на рейд\n"
    "/export_raids — экспорт списка рейдов (только владелец)\n"
    "/help — показать это сообщение\n"
)


# --- enka wrappers ---
async def _fetch_enka_showcase_async(uid: str) -> Optional[dict]:
    """Fetch showcase/profile via enka async client. Returns simplified dict or None.
    This function assumes enka.ZZZClient exists and is an async context manager with fetch_showcase.
    """
    if not HAS_ENKA:
        return None
    try:
        # Many enka wrappers name the client slightly different; we attempt to use ZZZClient
        Client = getattr(enka, "ZZZClient", None) or getattr(enka, "EnkaClient", None)
        if Client is None:
            logger.warning("enka installed but no known client class found")
            return None
        async with Client() as client:  # type: ignore
            # Some clients expect int uid
            resp = await client.fetch_showcase(int(uid))  # type: ignore
            out = {"uid": str(uid)}
            # try to extract common fields if present
            player = getattr(resp, "player", None)
            if player:
                out["nickname"] = getattr(player, "nickname", None)
                out["level"] = getattr(player, "level", None)
            # characters or summary
            chars = getattr(resp, "characters", None)
            if chars is None:
                # try alternative attribute names
                chars = getattr(resp, "avatars", None) or []
            out["summary"] = {"characters": len(chars) if chars else 0}
            return out
    except Exception as e:
        logger.warning("enka fetch failed for UID %s: %s", uid, e)
        return None


def fetch_enka_profile_sync(uid: str) -> Optional[dict]:
    """Synchronous wrapper for enka fetch used in polling/tests.
    If enka not installed, returns a mock dict.
    If an asyncio loop is already running, returns None to avoid blocking.
    """
    if not uid:
        return None
    if not HAS_ENKA:
        logger.info("enka not installed — returning mock profile for %s", uid)
        return {
            "uid": uid,
            "nickname": f"MockPlayer_{uid}",
            "level": 42,
            "notes": "enka not installed",
        }
    try:
        # if event loop running, avoid blocking
        try:
            loop = asyncio.get_running_loop()
            logger.warning("Event loop is running — cannot run sync enka fetch")
            return None
        except RuntimeError:
            return asyncio.run(_fetch_enka_showcase_async(uid))
    except Exception as e:
        logger.warning("fetch_enka_profile_sync failed: %s", e)
        return None


# --- Core logic (safe sessions) ---


def ensure_user(session, tg_id: int, nick: Optional[str] = None) -> User:
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id, nick=nick)  # type: ignore[name-defined]
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def cmd_start_logic(tg_id: int, username: Optional[str]) -> str:
    session = SessionLocal()
    try:
        user = get_user_by_tg(session, tg_id)
        if not user:
            user = User(tg_id=tg_id, nick=username)  # type: ignore[name-defined]
            session.add(user)
            session.commit()
            session.refresh(user)
            return "Привет! Ты зарегистрирован. Привяжи UID: /linkuid <UID>"
        return "/profile чтобы посмотреть профиль"
    finally:
        session.close()


def cmd_linkuid_logic(tg_id: int, uid: str) -> str:
    session = SessionLocal()
    try:
        user = get_user_by_tg(session, tg_id)
        if not user:
            user = User(tg_id=tg_id, uid=uid)  # type: ignore[name-defined]
            session.add(user)
        else:
            user.uid = uid
        session.commit()
        return f"UID {uid} привязан"
    finally:
        session.close()


def cmd_profile_logic(tg_id: int) -> str:
    session = SessionLocal()
    try:
        user = get_user_by_tg(session, tg_id)
        if not user:
            return "Не зарегистрированы. /start"
        lines = [f"Профиль @{user.nick}", f"Кристаллы: {user.crystals}"]
        if user.uid:
            lines.append(f"UID: {user.uid}")
            profile = fetch_enka_profile_sync(user.uid)
            if profile:
                if profile.get("nickname"):
                    lines.append(f"Игровой ник: {profile.get('nickname')}")
                if profile.get("level"):
                    lines.append(f"Уровень: {profile.get('level')}")
                if profile.get("notes"):
                    lines.append(f"({profile.get('notes')})")
            else:
                lines.append(
                    "(Не удалось получить данные с Enka — возможно enka не установлен или работающий loop)"
                )
        else:
            lines.append("UID не привязан. /linkuid <UID>")
        return "\n".join(lines)
    finally:
        session.close()


def cmd_daily_logic(tg_id: int) -> str:
    session = SessionLocal()
    try:
        user = get_user_by_tg(session, tg_id)
        if not user:
            user = User(tg_id=tg_id, nick=None)  # type: ignore[name-defined]
            session.add(user)
            session.commit()
            session.refresh(user)
        now = datetime.now(timezone.utc)
        last = _to_aware(user.last_daily)
        if last and (now - last) < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last)
            hh, rem = divmod(int(remaining.total_seconds()), 3600)
            mm, ss = divmod(rem, 60)
            return f"Ежедневный бонус уже взят. Следующий: {hh:02d}:{mm:02d}:{ss:02d}"
        user.crystals = (user.crystals or 0) + 50
        user.last_daily = now
        session.commit()
        return f"Хвостик вручает тебе 50 кристаллов! Сейчас: {user.crystals} кристаллов. Следующий бесплатный бонус через 24:00:00."
    finally:
        session.close()


def cmd_create_raid_logic(tg_id: int, boss: str, dt: datetime, slots: int) -> str:
    session = SessionLocal()
    try:
        user = get_user_by_tg(session, tg_id)
        if not user:
            user = User(tg_id=tg_id)  # type: ignore[name-defined]
            session.add(user)
            session.commit()
            session.refresh(user)
        raid = Raid(boss=boss, start_time=dt, slots=slots, creator_id=user.id)
        session.add(raid)
        session.commit()
        rid = raid.id
        return f"Рейд создан: ID {rid}. {boss} в {dt.isoformat()} (UTC). Слотов: {slots}. /join {rid}"
    finally:
        session.close()


def cmd_join_logic(tg_id: int, raid_id: int) -> str:
    session = SessionLocal()
    try:
        raid = session.query(Raid).filter(Raid.id == raid_id).first()
        if not raid:
            return "Рейд не найден."
        user = get_user_by_tg(session, tg_id)
        if not user:
            user = User(tg_id=tg_id)  # type: ignore[name-defined]
            session.add(user)
            session.commit()
            session.refresh(user)
        count = (
            session.query(RaidParticipant)
            .filter(RaidParticipant.raid_id == raid.id)
            .count()
        )
        if count >= raid.slots:
            return "Все слоты заняты."
        exists = (
            session.query(RaidParticipant)
            .filter(
                RaidParticipant.raid_id == raid.id, RaidParticipant.user_id == user.id
            )
            .first()
        )
        if exists:
            return "Вы уже записаны."
        rp = RaidParticipant(raid_id=raid.id, user_id=user.id)
        session.add(rp)
        session.commit()
        return f"Вы записаны на рейд {raid.id} — {raid.boss} в {raid.start_time.isoformat()} (UTC)."
    finally:
        session.close()


def cmd_export_raids_logic(tg_id: int) -> str:
    if OWNER_TG_ID and tg_id != OWNER_TG_ID:
        return "Только владелец может использовать эту команду."
    session = SessionLocal()
    try:
        raids = session.query(Raid).all()
        lines: List[str] = []
        for r in raids:
            participants = (
                session.query(RaidParticipant)
                .filter(RaidParticipant.raid_id == r.id)
                .all()
            )
            lines.append(
                f"{r.id}\t{r.boss}\t{r.start_time.isoformat()}\t{r.slots}\t{len(participants)}"
            )
        return "ID\tBoss\tStart\tSlots\tParticipants\n" + "\n".join(lines)
    finally:
        session.close()


# --- Polling fallback implementation ---


def send_message_via_api(chat_id: int, text: str):
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set; cannot send message")
        return False
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
    logger.info("Starting simple polling fallback (getUpdates)")
    offset: Optional[int] = None
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
                text = msg.get("text") or ""
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].lstrip("/").split("@")[0].lower()
                args_raw = " ".join(parts[1:])
                if cmd in ("help", "h"):
                    send_message_via_api(chat_id, HELP_TEXT)
                    continue
                try:
                    if cmd == "start":
                        resp = cmd_start_logic(from_id, username)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "linkuid":
                        if not parts[1:]:
                            send_message_via_api(
                                chat_id, "Использование: /linkuid <UID>"
                            )
                        else:
                            resp = cmd_linkuid_logic(from_id, parts[1])
                            send_message_via_api(chat_id, resp)
                    elif cmd == "profile":
                        resp = cmd_profile_logic(from_id)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "daily":
                        resp = cmd_daily_logic(from_id)
                        send_message_via_api(chat_id, resp)
                    elif cmd == "create_raid":
                        import shlex

                        try:
                            args = shlex.split(args_raw)
                            if len(args) < 3:
                                raise ValueError("args")
                            boss = args[0]
                            date = args[1]
                            time_part = args[2]
                            slots = int(args[3]) if len(args) >= 4 else 5
                            dt = datetime.fromisoformat(date + "T" + time_part)
                            dt = dt.replace(tzinfo=timezone.utc)
                            resp = cmd_create_raid_logic(from_id, boss, dt, slots)
                        except Exception:
                            resp = 'Неверный формат даты/времени. Используйте: /create_raid "Boss" YYYY-MM-DD HH:MM [slots]'
                        send_message_via_api(chat_id, resp)
                    elif cmd == "join":
                        if not parts[1:]:
                            send_message_via_api(
                                chat_id, "Использование: /join <raid_id>"
                            )
                        else:
                            try:
                                rid = int(parts[1])
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
                except Exception:
                    logger.exception("Error handling command")
                    send_message_via_api(chat_id, "Ошибка при обработке команды.")
        except requests.RequestException as e:
            logger.warning("getUpdates failed: %s", e)
            time.sleep(3)
        except Exception:
            logger.exception("Unexpected error in polling loop")
            time.sleep(3)


# --- aiogram integration (v2/v3) with enka-aware handlers ---


def try_run_aiogram() -> bool:
    try:
        import aiogram

        logger.info("Detected aiogram %s", getattr(aiogram, "__version__", "?"))
    except Exception as e:
        logger.info("aiogram not available: %s", e)
        return False

    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set — cannot run aiogram mode")
        return False

    # Try v2-style
    try:
        from aiogram import Bot, Dispatcher
        from aiogram import types as aiotypes

        try:
            dp = Dispatcher(Bot(token=BOT_TOKEN))
            v2_like = hasattr(dp, "message_handler") or hasattr(
                dp, "register_message_handler"
            )
        except Exception:
            dp = Dispatcher()
            v2_like = hasattr(dp, "message_handler") or hasattr(
                dp, "register_message_handler"
            )

        if dp and v2_like:
            logger.info("Using aiogram v2-style handlers")

            @dp.message_handler(commands=["start"])
            async def _h_start(message: aiotypes.Message):
                await message.reply(
                    cmd_start_logic(message.from_user.id, message.from_user.username)
                )

            @dp.message_handler(commands=["linkuid"])
            async def _h_linkuid(message: aiotypes.Message):
                args = message.get_args().strip()
                if not args:
                    await message.reply("Использование: /linkuid <UID>")
                    return
                await message.reply(
                    cmd_linkuid_logic(message.from_user.id, args.split()[0])
                )

            @dp.message_handler(commands=["profile"])
            async def _h_profile(message: aiotypes.Message):
                session = SessionLocal()
                try:
                    user = get_user_by_tg(session, message.from_user.id)
                    if not user:
                        await message.reply("Не зарегистрированы. /start")
                        return
                    lines = [f"Профиль @{user.nick}", f"Кристаллы: {user.crystals}"]
                    if user.uid:
                        lines.append(f"UID: {user.uid}")
                        profile = None
                        if HAS_ENKA:
                            profile = await _fetch_enka_showcase_async(user.uid)
                        if profile:
                            if profile.get("nickname"):
                                lines.append(f"Игровой ник: {profile.get('nickname')}")
                            if profile.get("level"):
                                lines.append(f"Уровень: {profile.get('level')}")
                        else:
                            lines.append(
                                "(Не удалось получить данные с Enka — проверьте установку пакета)"
                            )
                    else:
                        lines.append("UID не привязан. /linkuid <UID>")
                    await message.reply("\n".join(lines))
                finally:
                    session.close()

            @dp.message_handler(commands=["daily"])
            async def _h_daily(message: aiotypes.Message):
                await message.reply(cmd_daily_logic(message.from_user.id))

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
                    await message.reply(
                        cmd_create_raid_logic(message.from_user.id, boss, dt, slots)
                    )
                except Exception:
                    await message.reply("Неверный формат даты/времени.")

            @dp.message_handler(commands=["join"])
            async def _h_join(message: aiotypes.Message):
                args = message.get_args().strip().split()
                if not args:
                    await message.reply("Использование: /join <raid_id>")
                    return
                try:
                    rid = int(args[0])
                    await message.reply(cmd_join_logic(message.from_user.id, rid))
                except ValueError:
                    await message.reply("ID рейда должен быть числом.")

            @dp.message_handler(commands=["export_raids"])
            async def _h_export(message: aiotypes.Message):
                await message.reply(cmd_export_raids_logic(message.from_user.id))

            @dp.message_handler(commands=["help", "h"])
            async def _h_help(message: aiotypes.Message):
                await message.reply(HELP_TEXT)

            # run polling
            try:
                from aiogram.utils import executor

                logger.info("Starting aiogram executor polling")
                executor.start_polling(dp, skip_updates=True)
            except Exception:
                logger.info("Falling back to dp.start_polling()")
                asyncio.run(dp.start_polling())
            return True
    except Exception as e:
        logger.info("v2-style registration failed: %s", e)

    # Try v3-style
    try:
        from aiogram import Bot, Dispatcher
        from aiogram import Router
        from aiogram.filters import Command
        from aiogram.types import Message

        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()
        router = Router()

        async def _reply(msg: Message, text: str):
            await msg.answer(text)

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
            session = SessionLocal()
            try:
                user = get_user_by_tg(session, message.from_user.id)
                if not user:
                    await _reply(message, "Не зарегистрированы. /start")
                    return
                lines = [f"Профиль @{user.nick}", f"Кристаллы: {user.crystals}"]
                if user.uid:
                    lines.append(f"UID: {user.uid}")
                    profile = None
                    if HAS_ENKA:
                        profile = await _fetch_enka_showcase_async(user.uid)
                    if profile:
                        if profile.get("nickname"):
                            lines.append(f"Игровой ник: {profile.get('nickname')}")
                        if profile.get("level"):
                            lines.append(f"Уровень: {profile.get('level')}")
                    else:
                        lines.append(
                            "(Не удалось получить данные с Enka — проверьте установку пакета)"
                        )
                else:
                    lines.append("UID не привязан. /linkuid <UID>")
                await _reply(message, "\n".join(lines))
            finally:
                session.close()

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

        @router.message(Command("help"))
        async def _r_help(message: Message):
            await _reply(message, HELP_TEXT)

        dp.include_router(router)
        try:
            from aiogram import executor as aio_executor

            logger.info("Starting aiogram v3 executor polling")
            aio_executor.start_polling(dp)
        except Exception:
            logger.info("Starting aiogram v3 via dp.start_polling")
            asyncio.run(dp.start_polling(bot))
        return True

    except Exception as e:
        logger.info("v3-style registration failed: %s", e)

    return False


# --- CLI / Runner ---


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["auto", "aiogram", "polling", "tests"], default="auto"
    )
    args = parser.parse_args()

    mode = args.mode
    logger.info("Startup mode=%s BOT_TOKEN=%s", mode, bool(BOT_TOKEN))

    if mode == "tests":
        print("Running offline tests")
        print("Тестируем ежедневный бонус для пользователя 12345")
        print(cmd_daily_logic(12345))
        print("\nПринудительный сброс и повторный тест (должен выдать кристаллы)")
        session = SessionLocal()
        try:
            u = get_user_by_tg(session, 12345)
            if not u:
                u = User(tg_id=12345, nick="user12345")
                session.add(u)
                session.commit()
            u.last_daily = datetime.now(timezone.utc) - timedelta(hours=25)
            session.commit()
        finally:
            session.close()
        print(cmd_daily_logic(12345))
        print("\nТестирование получения профиля (mock или enka если установлен)")
        print(cmd_profile_logic(12345))
        print("\n/help пример:")
        print(HELP_TEXT)
        return

    if mode == "aiogram":
        ok = try_run_aiogram()
        if not ok:
            logger.error("aiogram mode failed to start; falling back to polling")
            run_simple_polling()
        return

    if mode == "polling":
        run_simple_polling()
        return

    # auto mode
    if BOT_TOKEN:
        ok = try_run_aiogram()
        if not ok:
            logger.info("aiogram not usable — starting polling fallback")
            run_simple_polling()
    else:
        print("BOT_TOKEN not set — running tests")
        main_args = sys.argv
        os.execv(sys.executable, [sys.executable] + main_args + ["--mode", "tests"])


if __name__ == "__main__":
    main()
