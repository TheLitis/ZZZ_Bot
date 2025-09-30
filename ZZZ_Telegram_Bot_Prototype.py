"""
ZZZ Telegram Bot Prototype v3 — adds full aiogram handlers and reminder task

This iteration adds:
- aiogram handlers: /start, /linkuid, /profile, /daily, /create_raid, /join, /export_raids
- background reminder task that notifies participants and creator 30 and 10 minutes before raid
- async wrapper for fetch_interknot_profile (uses requests under the hood via to_thread)
- safer Dispatcher initialization (Dispatcher(bot)) and guarded startup

Usage:
- Set BOT_TOKEN in .env to run real Telegram bot (ensure Python has ssl module).
- If BOT_TOKEN is not set or aiogram isn't installed, file can still be used for offline tests by running the main section.
"""

import os
import logging
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


def fetch_interknot_profile(uid: str) -> Optional[dict]:
    if not uid:
        return None
    if INTERKNOT_API_URL:
        api_url = INTERKNOT_API_URL.rstrip("/") + "/api/profile"
        params = {"uid": uid}
        headers = {"User-Agent": "ZZZ-TG-Bot/0.3"}
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
    # mock
    logger.info("Returning mock profile for UID %s", uid)
    return {
        "uid": uid,
        "nickname": f"MockPlayer_{uid}",
        "level": 42,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_interknot_profile_async(uid: str) -> Optional[dict]:
    return await asyncio.to_thread(fetch_interknot_profile, uid)


# --- Core logic ---


def ensure_user(session, tg_id: int, nick: Optional[str] = None) -> User:
    user = get_user_by_tg(session, tg_id)
    if not user:
        user = User(tg_id=tg_id, nick=nick)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


# --- Offline tests ---


def test_daily(user_tg_id: int):
    session = SessionLocal()
    user = get_user_by_tg(session, user_tg_id)
    if not user:
        user = User(tg_id=user_tg_id, nick=f"user{user_tg_id}")
        session.add(user)
        session.commit()
    now = datetime.now(timezone.utc)
    last = _to_aware(user.last_daily)
    if last and (now - last) < timedelta(hours=24):
        remaining = timedelta(hours=24) - (now - last)
        hh, rem = divmod(int(remaining.total_seconds()), 3600)
        mm, ss = divmod(rem, 60)
        print(f"Ежедневный бонус уже взят. Следующий через {hh:02d}:{mm:02d}:{ss:02d}")
        session.close()
        return
    user.crystals = (user.crystals or 0) + 50
    user.last_daily = now
    session.commit()
    print(f"Выдано 50 кристаллов. Сейчас: {user.crystals}")
    session.close()


# --- aiogram mode ---
HAS_AIOGRAM = False
try:
    if BOT_TOKEN:
        from aiogram import Bot, Dispatcher, types
        from aiogram.utils import executor

        HAS_AIOGRAM = True
        logger.info("aiogram available and BOT_TOKEN set — running in Telegram mode")
    else:
        logger.info("BOT_TOKEN not set — aiogram will not be used")
except Exception as e:
    logger.info("aiogram import failed: %s", e)
    HAS_AIOGRAM = False


if not HAS_AIOGRAM:
    if __name__ == "__main__":
        print("Running offline tests")
        test_daily(12345)
        print("\nForcing reset and re-testing")
        session = SessionLocal()
        u = get_user_by_tg(session, 12345)
        if not u:
            u = User(tg_id=12345, nick="user12345")
            session.add(u)
            session.commit()
        u.last_daily = datetime.now(timezone.utc) - timedelta(hours=25)
        session.commit()
        session.close()
        test_daily(12345)
        import asyncio as _async

        _async.run(fetch_interknot_profile_async("exampleUID"))

else:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot)

    async def send_message_safe(chat_id: int, text: str):
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", chat_id, e)

    @dp.message_handler(commands=["start"])
    async def cmd_start(message: types.Message):
        session = SessionLocal()
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            user = User(tg_id=message.from_user.id, nick=message.from_user.username)
            session.add(user)
            session.commit()
            await message.reply(
                "Привет! Ты зарегистрирован. Привяжи UID: /linkuid <UID>"
            )
        else:
            await message.reply("/profile чтобы посмотреть профиль")
        session.close()

    @dp.message_handler(commands=["linkuid"])
    async def cmd_linkuid(message: types.Message):
        args = message.get_args().strip()
        if not args:
            await message.reply("Использование: /linkuid <UID>")
            return
        uid = args.split()[0]
        session = SessionLocal()
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            user = User(
                tg_id=message.from_user.id, nick=message.from_user.username, uid=uid
            )
            session.add(user)
        else:
            user.uid = uid
        session.commit()
        session.close()
        await message.reply(f"UID {uid} привязан")

    @dp.message_handler(commands=["profile"])
    async def cmd_profile(message: types.Message):
        session = SessionLocal()
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            await message.reply("Не зарегистрированы. /start")
            session.close()
            return
        lines = [f"Профиль @{user.nick}", f"Кристаллы: {user.crystals}"]
        if user.uid:
            lines.append(f"UID: {user.uid}")
            profile = await fetch_interknot_profile_async(user.uid)
            if profile:
                if "nickname" in profile:
                    lines.append(f"Игровой ник: {profile.get('nickname')}")
                if "level" in profile:
                    lines.append(f"Уровень: {profile.get('level')}")
                if "raw_text" in profile:
                    lines.append("(Получены данные — см. админку)")
            else:
                lines.append("(Не удалось получить данные с внешнего сервиса)")
        else:
            lines.append("UID не привязан. /linkuid <UID>")
        await message.reply("\n".join(lines))
        session.close()

    @dp.message_handler(commands=["daily"])
    async def cmd_daily(message: types.Message):
        session = SessionLocal()
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            user = User(tg_id=message.from_user.id, nick=message.from_user.username)
            session.add(user)
            session.commit()
        now = datetime.now(timezone.utc)
        last = _to_aware(user.last_daily)
        if last and (now - last) < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last)
            hh, rem = divmod(int(remaining.total_seconds()), 3600)
            mm, ss = divmod(rem, 60)
            await message.reply(
                f"Ежедневный бонус уже взят. Следующий: {hh:02d}:{mm:02d}:{ss:02d}"
            )
            session.close()
            return
        user.crystals = (user.crystals or 0) + 50
        user.last_daily = now
        session.commit()
        await message.reply(
            f"Хвостик вручает тебе 50 кристаллов! Сейчас: {user.crystals} кристаллов. Следующий бесплатный бонус через 24:00:00."
        )
        session.close()

    def _parse_create_raid_args(args: str) -> Optional[tuple]:
        import shlex

        try:
            parts = shlex.split(args)
        except Exception:
            return None
        if len(parts) < 3:
            return None
        boss = parts[0]
        date = parts[1]
        time = parts[2]
        slots = int(parts[3]) if len(parts) >= 4 else 5
        try:
            dt = datetime.fromisoformat(date + "T" + time)
            dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        return boss, dt, slots

    @dp.message_handler(commands=["create_raid"])
    async def cmd_create_raid(message: types.Message):
        args = message.get_args()
        parsed = _parse_create_raid_args(args)
        if not parsed:
            await message.reply(
                'Использование: /create_raid "Boss name" YYYY-MM-DD HH:MM [slots]'
            )
            return
        boss, dt, slots = parsed
        session = SessionLocal()
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            user = User(tg_id=message.from_user.id, nick=message.from_user.username)
            session.add(user)
            session.commit()
        raid = Raid(boss=boss, start_time=dt, slots=slots, creator_id=user.id)
        session.add(raid)
        session.commit()
        await message.reply(
            f"Рейд создан: ID {raid.id}. {boss} в {dt.isoformat()} (UTC). Слотов: {slots}. /join {raid.id}"
        )
        session.close()

    @dp.message_handler(commands=["join"])
    async def cmd_join(message: types.Message):
        args = message.get_args().strip()
        if not args:
            await message.reply("Использование: /join <raid_id>")
            return
        try:
            raid_id = int(args.split()[0])
        except ValueError:
            await message.reply("ID рейда должен быть числом.")
            return
        session = SessionLocal()
        raid = session.query(Raid).filter(Raid.id == raid_id).first()
        if not raid:
            await message.reply("Рейд не найден.")
            session.close()
            return
        user = get_user_by_tg(session, message.from_user.id)
        if not user:
            user = User(tg_id=message.from_user.id, nick=message.from_user.username)
            session.add(user)
            session.commit()
        count = (
            session.query(RaidParticipant)
            .filter(RaidParticipant.raid_id == raid.id)
            .count()
        )
        if count >= raid.slots:
            await message.reply("Все слоты заняты.")
            session.close()
            return
        exists = (
            session.query(RaidParticipant)
            .filter(
                RaidParticipant.raid_id == raid.id, RaidParticipant.user_id == user.id
            )
            .first()
        )
        if exists:
            await message.reply("Вы уже записаны.")
            session.close()
            return
        rp = RaidParticipant(raid_id=raid.id, user_id=user.id)
        session.add(rp)
        session.commit()
        session.close()
        await message.reply(
            f"Вы записаны на рейд {raid.id} — {raid.boss} в {raid.start_time.isoformat()} (UTC)."
        )

    @dp.message_handler(commands=["export_raids"])
    async def cmd_export_raids(message: types.Message):
        if OWNER_TG_ID and message.from_user.id != OWNER_TG_ID:
            await message.reply("Только владелец может использовать эту команду.")
            return
        session = SessionLocal()
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
        session.close()
        await message.reply("ID\tBoss\tStart\tSlots\tParticipants\n" + "\n".join(lines))

    async def reminder_task():
        await asyncio.sleep(1)
        while True:
            try:
                now = datetime.now(timezone.utc)
                session = SessionLocal()
                upcoming = session.query(Raid).filter(Raid.start_time > now).all()
                for raid in upcoming:
                    delta = raid.start_time - now
                    minutes = int(delta.total_seconds() // 60)
                    notify_times = [30, 10]
                    if minutes in notify_times:
                        participants = (
                            session.query(RaidParticipant)
                            .filter(RaidParticipant.raid_id == raid.id)
                            .all()
                        )
                        tg_ids = set()
                        creator = (
                            session.query(User)
                            .filter(User.id == raid.creator_id)
                            .first()
                        )
                        if creator:
                            tg_ids.add(creator.tg_id)
                        for p in participants:
                            u = session.query(User).filter(User.id == p.user_id).first()
                            if u:
                                tg_ids.add(u.tg_id)
                        text = f"Напоминание: рейд {raid.boss} через {minutes} минут (ID {raid.id})."
                        for tg in tg_ids:
                            asyncio.create_task(send_message_safe(tg, text))
                session.close()
            except Exception as e:
                logger.exception("Ошибка в reminder_task: %s", e)
            await asyncio.sleep(50)

    async def on_startup(_):
        logger.info("Starting reminder task...")
        asyncio.create_task(reminder_task())

    if __name__ == "__main__":
        logger.info("Starting aiogram bot...")
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
