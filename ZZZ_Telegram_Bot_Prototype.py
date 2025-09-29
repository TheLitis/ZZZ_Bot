"""
ZZZ Telegram Bot Prototype — single-file implementation (offline-friendly)

Изменения в этом варианте:
- Убран жёсткий `aiogram` / `aiohttp` импорт — это обход для окружений без SSL.
- Исправлена устаревшая точка импорта SQLAlchemy (declarative_base из sqlalchemy.orm).
- Переведены даты на timezone-aware (UTC) — исправлены DeprecationWarning по datetime.utcnow().
- `BOT_TOKEN` теперь опционален: если он отсутствует — скрипт работает в режиме тестирования.
- Добавлен конфиг через окружение для реального Interknot API (INTERKNOT_API_URL, INTERKNOT_API_KEY). Если не задан — используется mock-ответ, чтобы тесты работали оффлайн.
- Добавлены дополнительные тесты: сброс ежедневного бонуса и тест мок-профиля.

Запуск: python ZZZ_Telegram_Bot_Prototype.py

"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# BOT_TOKEN опционален в этом локальном/тестовом варианте
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.info("BOT_TOKEN not set — running in offline/test mode (no aiogram).")

# Интеркнот-конфиг (опционально): если задан, будет использован для реального запроса
INTERKNOT_API_URL = os.getenv("INTERKNOT_API_URL")  # e.g. https://interknot-network.com
INTERKNOT_API_KEY = os.getenv("INTERKNOT_API_KEY")  # если требуется

logging.basicConfig(level=logging.INFO)

# --- Database setup (SQLite for prototype) ---
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
    """Return timezone-aware datetime in UTC. If dt is naive, assume UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_interknot_profile(uid: str) -> Optional[dict]:
    """
    If INTERKNOT_API_URL is configured, try to call real API.
    Otherwise return a mocked profile so offline tests pass.
    """
    if not uid:
        return None

    if INTERKNOT_API_URL:
        # Preferential real API call — adapt path to real service docs
        api_url = INTERKNOT_API_URL.rstrip("/") + f"/api/profile"
        params = {"uid": uid}
        headers = {"User-Agent": "ZZZ-TG-Bot/0.1"}
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

    # Offline/mock mode
    logging.info("INTERKNOT_API_URL not set — returning mock profile for UID %s", uid)
    return {
        "uid": uid,
        "nickname": f"MockPlayer_{uid}",
        "level": 42,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "notes": "This is a mock response (set INTERKNOT_API_URL to use real API)",
    }


# --- Тестовые команды (для окружений без aiohttp) ---


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
    """Force-reset last_daily to 25 hours ago and apply daily bonus (useful for tests)."""
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


# --- Пример тестов ---
if __name__ == "__main__":
    print("Тестируем ежедневный бонус для пользователя 12345")
    test_daily(12345)

    print("\nПринудительный сброс и повторный тест (должен выдать кристаллы)")
    test_daily_force_reset(12345)

    print(
        "\nТестирование получения профиля (mock или реальный, в зависимости от конфигурации)"
    )
    test_profile_mock("exampleUID")
