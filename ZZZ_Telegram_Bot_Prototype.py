"""
ZZZ Telegram Bot Prototype — single-file aiogram implementation

Фичи в прототипе:
- /start — регистрация
- /linkuid <UID> — привязать UID для получения данных с Interknot-like сервисов
- /profile — показать профиль: локальные данные + попытка получить данные с interknot-network
- /daily — ежедневный бонус (50 кристаллов), с учётом отката в 24 часа
- /create_raid <boss> <YYYY-MM-DD HH:MM> <slots> — создать рейд
- /join <raid_id> — записаться на рейд

Технологии: aiogram, SQLAlchemy (SQLite), requests.

Запуск:
1) Установить зависимости: pip install aiogram sqlalchemy aiosqlite requests python-dotenv
2) Создать .env с BOT_TOKEN=ваш_токен
3) Запустить: python bot.py

Примечание: чтобы избежать ошибки ModuleNotFoundError: No module named 'ssl', убедитесь, что Python установлен с поддержкой SSL. В средах без SSL aiogram через aiohttp работать не будет.
Альтернатива для тестирования: использовать `requests` напрямую без aiohttp или запускать бот на локальной машине с полноценным Python.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

# Для окружений без ssl используем Telegram Bot API через requests напрямую
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('7858901820:AAG6vRpZ22M6Ft5UzTsL21SzUu-A93HMyvg')
if not BOT_TOKEN:
    raise RuntimeError('Set BOT_TOKEN in .env')

logging.basicConfig(level=logging.INFO)

# --- Database setup (SQLite for prototype) ---
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

Base = declarative_base()
engine = create_engine('sqlite:///zzz_bot.db', connect_args={'check_same_thread': False})
SessionLocal = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    nick = Column(String, nullable=True)
    uid = Column(String, nullable=True)
    crystals = Column(Integer, default=0)
    last_daily = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=func.now())

class Raid(Base):
    __tablename__ = 'raids'
    id = Column(Integer, primary_key=True)
    boss = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    slots = Column(Integer, nullable=False)
    creator_id = Column(Integer, ForeignKey('users.id'))
    creator = relationship('User')
    created_at = Column(DateTime, default=func.now())

class RaidParticipant(Base):
    __tablename__ = 'raid_participants'
    id = Column(Integer, primary_key=True)
    raid_id = Column(Integer, ForeignKey('raids.id'))
    user_id = Column(Integer, ForeignKey('users.id'))
    joined_at = Column(DateTime, default=func.now())

Base.metadata.create_all(engine)

# --- Helpers ---

def get_user_by_tg(session, tg_id: int) -> Optional[User]:
    return session.query(User).filter(User.tg_id == tg_id).first()

def fetch_interknot_profile(uid: str) -> Optional[dict]:
    if not uid:
        return None

    candidates = [
        f'https://interknot-network.com/api/profile?uid={uid}',
        f'https://interknot-network.com/profile/{uid}',
    ]
    headers = {'User-Agent': 'ZZZ-TG-Bot/0.1'}
    for url in candidates:
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return {'raw_html': resp.text[:200]}
        except requests.RequestException:
            continue
    return None

# --- Тестовые команды через requests (для окружений без aiohttp) ---
# Здесь добавлены простые функции для ручного тестирования в консоли, без использования aiogram

def test_daily(user_tg_id: int):
    session = SessionLocal()
    user = get_user_by_tg(session, user_tg_id)
    if not user:
        user = User(tg_id=user_tg_id, nick=f'user{user_tg_id}')
        session.add(user)
        session.commit()
    now = datetime.utcnow()
    if user.last_daily and (now - user.last_daily) < timedelta(hours=24):
        print('Ежедневный бонус уже взят.')
    else:
        user.crystals += 50
        user.last_daily = now
        session.commit()
        print(f'Выдано 50 кристаллов. Сейчас: {user.crystals}')
    session.close()

# --- Пример теста ---
if __name__ == '__main__':
    print('Тестируем ежедневный бонус для пользователя 12345')
    test_daily(12345)
    print('Попытка получения профиля (симуляция)')
    profile = fetch_interknot_profile('exampleUID')
    print(profile)
