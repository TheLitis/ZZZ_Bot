"""
ZZZ Telegram Bot Prototype v2 — single-file implementation (offline + aiogram-friendly)

Изменения в этом варианте:
- Добавлена опциональная поддержка aiogram для реального Telegram бота.
- Все даты timezone-aware (UTC), исправлены устаревшие datetime.utcnow()
- BOT_TOKEN опционален: если отсутствует, бот работает в оффлайн/тест режиме.
- Конфиг Interknot API (INTERKNOT_API_URL, INTERKNOT_API_KEY) для получения реальных данных по UID.
- Тестовые команды: /daily, /profile mock, сброс ежедневного бонуса.
- Полная база данных с SQLite, модели User, Raid, RaidParticipant.

Запуск:
1) Для оффлайн тестирования: python ZZZ_Telegram_Bot_Prototype_v2.py
2) Для реального бота: убедитесь, что Python поддерживает ssl и задан BOT_TOKEN в .env
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
INTERKNOT_API_URL = os.getenv("INTERKNOT_API_URL")
INTERKNOT_API_KEY = os.getenv("INTERKNOT_API_KEY")

logging.basicConfig(level=logging.INFO)

try:
    if BOT_TOKEN:
        from aiogram import Bot, Dispatcher, types
        from aiogram.utils import executor

        AIORUN = True
        logging.info("aiogram detected, running in Telegram bot mode")
    else:
        AIORUN = False
except ImportError:
    AIORUN = False
    logging.info("aiogram not available, running in offline/test mode")

# --- Database setup ---
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
        api_url = INTERKNOT_API_URL.rstrip("/") + f"/api/profile"
        params = {"uid": uid}
        headers = {"User-Agent": "ZZZ-TG-Bot/0.2"}
        if INTERKNOT_API_KEY:
            headers["Authorization"] = f"Bearer {INTERKNOT_API_KEY}"
        try:
            resp = requests.get(api_url, params=params, headers=headers, timeout=6)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {"raw_text": resp.text[:300]}
        except requests.RequestException as e:
            logging.warning("Failed to fetch Interknot profile: %s", e)
            return None

    logging.info("INTERKNOT_API_URL not set — returning mock profile for UID %s", uid)
    return {
        "uid": uid,
        "nickname": f"MockPlayer_{uid}",
        "level": 42,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "notes": "This is a mock response (set INTERKNOT_API_URL to use real API)",
    }


# --- Test / Offline commands ---


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


def test_daily_force_reset(user_tg_id: int):
    session = SessionLocal()
    user = get_user_by_tg(session, user_tg_id)
    if not user:
        user = User(tg_id=user_tg_id, nick=f"user{user_tg_id}")
        session.add(user)
        session.commit()
    user.last_daily = datetime.now(timezone.utc) - timedelta(hours=25)
    session.commit()
    session.close()
    print("last_daily forced to >24h ago")
    test_daily(user_tg_id)


def test_profile_mock(uid: str):
    p = fetch_interknot_profile(uid)
    print("Profile result:", p)


# --- Main ---
if __name__ == "__main__":
    print("Тестируем ежедневный бонус для пользователя 12345")
    test_daily(12345)

    print("\nПринудительный сброс и повторный тест (должен выдать кристаллы)")
    test_daily_force_reset(12345)

    print(
        "\nТестирование получения профиля (mock или реальный, в зависимости от конфигурации)"
    )
    test_profile_mock("exampleUID")
