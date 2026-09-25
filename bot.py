import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest

try:
    # CopyTextButton появился в aiogram 3.7+ (Bot API 7.6) — кнопка, которая
    # копирует текст в буфер обмена по нажатию, без выполнения действия.
    from aiogram.types import CopyTextButton
    HAS_COPY_TEXT_BUTTON = True
except ImportError:
    CopyTextButton = None
    HAS_COPY_TEXT_BUTTON = False


def copy_text_button(label: str, copy_value: str) -> InlineKeyboardButton:
    """Кнопка, которая копирует текст команды по нажатию, чтобы игрок
    мог сам отправить её сообщением (в т.ч. без ответа на сообщение партнёра).
    Если версия aiogram не поддерживает copy_text — падаем обратно на
    callback-кнопку, которая просто подсказывает, что написать."""
    if HAS_COPY_TEXT_BUTTON:
        return InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=copy_value))
    return InlineKeyboardButton(text=label, callback_data=f"marriage_hint_{hash(copy_value) & 0xffff}")

# =========================================================
#  НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
# По запросу: бот теперь может работать не только с локальным файлом SQLite,
# но и с внешней базой PostgreSQL (Supabase/Neon/Railway и т.п.) — просто
# укажи переменную окружения DATABASE_URL со ссылкой вида
# postgres://user:password@host:5432/dbname (или postgresql://...).
# Если DATABASE_URL не задана — всё работает как раньше, локальный файл SQLite.
# ВАЖНО: для режима Postgres на сервере должен быть установлен пакет
# psycopg2-binary (pip install psycopg2-binary) — сам бот его не ставит.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# По запросу: путь к базе данных (для файлового режима SQLite) теперь можно
# переопределить переменной окружения DB_PATH (например, чтобы хранить файл
# на постоянном/примонтированном диске). Если переменная не задана — как и
# раньше, файл wolf_game.db рядом со скриптом. Не используется, если задана
# DATABASE_URL (режим Postgres).
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "wolf_game.db"))

WOLF_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "wolf.png")
WOLF_STICKER_ID = os.getenv("WOLF_STICKER_ID", "")

# ID админов через запятую в переменной окружения: ADMIN_IDS="111111,222222"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wolf_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =========================================================
#  ЗАЩИТА ОТ "ЧУЖИХ КНОПОК"
# =========================================================
# Для каждого экрана (сообщения с инлайн-кнопками) запоминаем, кому именно
# он принадлежит. Если по кнопке под чужим сообщением нажимает другой
# пользователь — бот вежливо откажет, вместо того чтобы выполнять действие
# от чужого имени поверх чужого экрана.
MESSAGE_OWNERS: dict[tuple[int, int], int] = {}

# callback_data-префиксы, которые ПО СМЫСЛУ предназначены не автору
# сообщения, а другому конкретному адресату (предложение брака/похода
# отправляется партнёру, заявки на выдачу видят только админы) —
# для них общая проверка владельца сообщения не применяется.
OWNER_CHECK_EXEMPT_PREFIXES = (
    "marry_yes_", "marry_no_",
    "joint_yes_", "joint_no_",
    "order_take_", "order_decline_",
)


def remember_owner(chat_id: int, message_id: int, owner_id: int):
    MESSAGE_OWNERS[(chat_id, message_id)] = owner_id
    # сохраняем и в БД, чтобы владение сообщением не терялось при перезапуске бота
    try:
        conn = db()
        conn.execute(
            "INSERT INTO message_owners (chat_id, message_id, owner_id) VALUES (?,?,?) "
            "ON CONFLICT(chat_id, message_id) DO UPDATE SET owner_id=excluded.owner_id",
            (chat_id, message_id, owner_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Не удалось сохранить владельца сообщения: %s", e)


def _get_owner(chat_id: int, message_id: int) -> int | None:
    if (chat_id, message_id) in MESSAGE_OWNERS:
        return MESSAGE_OWNERS[(chat_id, message_id)]
    # в памяти не нашли (например, бот только что перезапустился) — смотрим в БД
    try:
        conn = db()
        row = conn.execute(
            "SELECT owner_id FROM message_owners WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        conn.close()
    except Exception:
        row = None
    if row is None:
        return None
    MESSAGE_OWNERS[(chat_id, message_id)] = row["owner_id"]
    return row["owner_id"]


@dp.callback_query.outer_middleware()
async def owner_guard_middleware(handler, callback: CallbackQuery, data):
    """Выполняется ПЕРЕД любым обработчиком инлайн-кнопок: не даёт нажимать
    кнопки под чужим экраном (профиль/волк/арена/магазин и т.п.)."""
    if not await _owner_allowed(callback):
        return
    return await handler(callback, data)


async def _owner_allowed(callback: CallbackQuery) -> bool:
    """Общая проверка владельца сообщения. Используется и как middleware,
    и как глобальный router-level filter (на случай если middleware
    почему-то не сработает в конкретной среде — двойная защита).
    По запросу: чужие кнопки нельзя нажимать вообще никому, включая
    админов, — каждое сообщение принадлежит только тому, кто его открыл.
    Отдельные заявки админам (заказы, ...) идут через OWNER_CHECK_EXEMPT_PREFIXES
    и проверяются своей собственной is_admin-проверкой внутри хендлера."""
    msg = callback.message
    cb_data = callback.data or ""
    if msg is None or cb_data.startswith(OWNER_CHECK_EXEMPT_PREFIXES):
        return True
    owner_id = _get_owner(msg.chat.id, msg.message_id)
    if owner_id is not None and owner_id != callback.from_user.id:
        try:
            await callback.answer(
                "🚫 Это меню другого игрока! Откройте своё через «профиль» или «волк».",
                show_alert=True,
            )
        except Exception:
            pass
        return False
    return True


# Дополнительно регистрируем ту же проверку как router-level filter —
# это второй, независимый от middleware механизм aiogram, который
# применяется автоматически ко ВСЕМ callback_query-хендлерам этого
# диспетчера (логическое И с фильтром каждого конкретного хендлера).
dp.callback_query.filter(_owner_allowed)


@dp.message.outer_middleware()
async def track_known_chats_middleware(handler, message: Message, data):
    """Запоминает каждый чат, где бот увидел хоть одно сообщение — нужно
    для команды рассылки во все чаты."""
    try:
        record_known_chat(message.chat)
    except Exception as e:
        log.warning("Не удалось запомнить чат для рассылки: %s", e)
    return await handler(message, data)


def record_known_chat(chat):
    conn = db()
    conn.execute(
        "INSERT INTO known_chats (chat_id, chat_type, title, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET chat_type=excluded.chat_type, title=excluded.title, "
        "last_seen=excluded.last_seen",
        (chat.id, chat.type, chat.title or chat.full_name or chat.username or str(chat.id),
         datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_known_chat_ids() -> list[int]:
    conn = db()
    rows = conn.execute("SELECT chat_id FROM known_chats").fetchall()
    conn.close()
    return [row["chat_id"] for row in rows]

# =========================================================
#  БАЗА ДАННЫХ
# =========================================================

# =========================================================
#  БАЗА ДАННЫХ
# =========================================================
# По запросу: поддержка PostgreSQL (DATABASE_URL) в дополнение к SQLite.
# Весь остальной код бота как писал SQL с плейсхолдерами "?" и обращался
# к строкам как row["колонка"] (стиль sqlite3.Row) — так и продолжает,
# ничего в ~4000 строках бизнес-логики трогать не пришлось. Обёртки ниже
# просто подставляют psycopg2 под тем же интерфейсом, когда задана DATABASE_URL.


def _pg_translate_sql(sql: str) -> str:
    """SQLite использует плейсхолдеры '?', psycopg2 — '%s'. А поскольку '%' для
    psycopg2 имеет специальный смысл (это и есть его плейсхолдер), любой
    "настоящий" '%' в SQL (например, в LIKE '%текст%') сначала экранируем
    удвоением — иначе psycopg2 попытается подставить туда несуществующий параметр."""
    return sql.replace("%", "%%").replace("?", "%s")


class _PGCursor:
    """Обёртка над курсором psycopg2, повторяющая нужный код бота API sqlite3.Cursor:
    execute(...) возвращает сам объект (для цепочки .execute(...).fetchone()),
    plus fetchone()/fetchall()/lastrowid."""

    def __init__(self, pg_cursor):
        self._cur = pg_cursor
        self.lastrowid = None  # у Postgres нет lastrowid — см. create_order (RETURNING)

    def execute(self, sql: str, params=()):
        self._cur.execute(_pg_translate_sql(sql), tuple(params))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PGConn:
    """Обёртка над соединением psycopg2, повторяющая нужный код бота API
    sqlite3.Connection: execute(...), cursor(), commit(), close()."""

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def _cursor(self):
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=()):
        cur = _PGCursor(self._cursor())
        return cur.execute(sql, params)

    def cursor(self):
        return _PGCursor(self._cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def db():
    if USE_POSTGRES:
        pg_conn = psycopg2.connect(DATABASE_URL)
        return _PGConn(pg_conn)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL — читатели не блокируют писателя и наоборот (меньше "database is locked"
    # при параллельных запросах от разных игроков); NORMAL — быстрее fsync, безопасно с WAL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            registered_at TEXT,
            rank TEXT DEFAULT 'Смертный',
            dollars INTEGER DEFAULT 0,
            keys INTEGER DEFAULT 0,
            runes INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            rings INTEGER DEFAULT 0,
            chests INTEGER DEFAULT 0,
            last_bonus TEXT,
            last_gift TEXT,
            pending_action TEXT,
            prefix_until TEXT,
            antitarget_until TEXT,
            spouse_id INTEGER,
            maze_active INTEGER DEFAULT 0,
            maze_progress INTEGER DEFAULT 0,
            maze_mistakes INTEGER DEFAULT 0,
            maze_correct TEXT,
            pending_marriage_from INTEGER,
            marriage_xp INTEGER DEFAULT 0,
            marriage_level INTEGER DEFAULT 1,
            pending_msg_id INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pets (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            alive INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            pet_xp INTEGER DEFAULT 0,
            weight REAL DEFAULT 20.0,
            satiety INTEGER DEFAULT 100,
            bones INTEGER DEFAULT 0,
            firewood REAL DEFAULT 0,
            raw_meat INTEGER DEFAULT 0,
            cooked_meat INTEGER DEFAULT 0,
            last_fed TEXT,
            starved_since TEXT,
            busy_until TEXT,
            busy_activity TEXT,
            cooking_until TEXT,
            skin TEXT DEFAULT 'Wolf',
            fight_opponent_id INTEGER,
            fight_is_attacker INTEGER DEFAULT 0,
            joint_activity_bonus INTEGER DEFAULT 0,
            in_arena INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quests (
            user_id BIGINT,
            quest_id TEXT,
            period_start TEXT,
            progress REAL DEFAULT 0,
            completed INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, quest_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_quests (
            user_id BIGINT PRIMARY KEY,
            quest_ids TEXT,
            assigned_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_at TEXT,
            added_by INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id {orders_pk_type},
            user_id BIGINT,
            username TEXT,
            display_name TEXT,
            product TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            taken_by INTEGER,
            payload TEXT
        )
    """.format(orders_pk_type="SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS owned_skins (
            user_id BIGINT,
            skin_name TEXT,
            PRIMARY KEY (user_id, skin_name)
        )
    """)
    cur.execute("""
        -- По запросу: скины теперь можно создавать не только из захардкоженного
        -- списка SKINS, а из ЛЮБОГО стикера (админ отвечает командой на нужный
        -- стикер) — эта таблица хранит такие "самодельные" скины и их file_id.
        CREATE TABLE IF NOT EXISTS custom_skins (
            name TEXT PRIMARY KEY,
            sticker_id TEXT,
            created_by BIGINT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS marriage_action_cooldowns (
            user_id BIGINT,
            action_id TEXT,
            available_at TEXT,
            PRIMARY KEY (user_id, action_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS potions (
            user_id BIGINT,
            potion_type TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, potion_type)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_owners (
            chat_id BIGINT,
            message_id INTEGER,
            owner_id BIGINT,
            PRIMARY KEY (chat_id, message_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS known_chats (
            chat_id BIGINT PRIMARY KEY,
            chat_type TEXT,
            title TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()
    _migrate_columns(conn)
    _create_indexes(conn)
    _normalize_pet_weight(conn)
    _seed_builtin_custom_skins(conn)
    conn.close()


def _create_indexes(conn: sqlite3.Connection):
    """Индексы под самые частые запросы — фоновые проверки (busy/game/satiety)
    и поиск игрока по @username иначе делают полный скан таблицы, что с ростом
    базы становится всё медленнее."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_busy_alive ON pets(busy_until, alive)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_game_pending ON pets(game_pending, alive)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_alive_satiety ON pets(alive, satiety)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_cooking ON pets(cooking_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_arena ON pets(in_arena, alive, busy_until)")
    if USE_POSTGRES:
        # у Postgres нет "COLLATE NOCASE" — регистронезависимый индекс делаем через LOWER(...),
        # что как раз совпадает с тем, как find_user_by_username ищет игрока (LOWER(username)=?)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(LOWER(username))")
    else:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)")
    conn.commit()


def _normalize_pet_weight(conn: sqlite3.Connection):
    """Одноразовая (но безопасная при повторных запусках) нормализация: у части
    игроков вес улетел до 300-400+ кг (набегало от частого кормления, пока не было
    потолка). Приводим таких питомцев к потолку MAX_PET_WEIGHT — дальше он держится
    сам за счёт cap'а в кормлении."""
    conn.execute("UPDATE pets SET weight = ? WHERE weight > ?", (MAX_PET_WEIGHT, MAX_PET_WEIGHT))
    conn.commit()


def _seed_builtin_custom_skins(conn: sqlite3.Connection):
    """Регистрирует "встроенные" кастомные скины (например, приз из ларца),
    у которых пока нет привязанного стикера — админ сможет позже привязать
    настоящий стикер командой `!скин_стикер <имя>` (реплай на стикер), а до
    этого скин просто использует стикер по умолчанию (Wolf)."""
    conn.execute(
        "INSERT INTO custom_skins (name, sticker_id, created_by, created_at) VALUES (?, NULL, NULL, ?) "
        "ON CONFLICT (name) DO NOTHING",
        ("Счастливчик", datetime.utcnow().isoformat()),
    )
    conn.commit()


def _migrate_columns(conn: sqlite3.Connection):
    """Добавляет недостающие колонки в уже существующую базу (без потери данных)."""
    wanted = {
        "users": {
            "spouse_id": "BIGINT",
            "maze_active": "INTEGER DEFAULT 0",
            "maze_progress": "INTEGER DEFAULT 0",
            "maze_mistakes": "INTEGER DEFAULT 0",
            "maze_correct": "TEXT",
            "pending_marriage_from": "BIGINT",
            "marriage_xp": "INTEGER DEFAULT 0",
            "marriage_level": "INTEGER DEFAULT 1",
            "pending_msg_id": "INTEGER",
            # по запросу: ларцы — редкий предмет, открывается 3 ключами (см. LARETS_PRIZES)
            "larets": "INTEGER DEFAULT 0",
        },
        "pets": {
            "skin": "TEXT DEFAULT 'Wolf'",
            "fight_opponent_id": "BIGINT",
            "fight_is_attacker": "INTEGER DEFAULT 0",
            "joint_activity_bonus": "INTEGER DEFAULT 0",
            "in_arena": "INTEGER DEFAULT 0",
            "pending_potion_strength": "INTEGER DEFAULT 0",
            "pending_potion_speed": "INTEGER DEFAULT 0",
            "pending_potion_loot": "INTEGER DEFAULT 0",
            # мини-игра "чей след?" при возвращении из леса/охоты
            "game_pending": "INTEGER DEFAULT 0",
            "game_correct": "TEXT",
            "game_payload": "TEXT",
            "game_offered_at": "TEXT",
        },
    }
    cur = conn.cursor()
    for table, cols in wanted.items():
        if USE_POSTGRES:
            existing = {
                row["column_name"]
                for row in cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,)
                ).fetchall()
            }
        else:
            existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    _migrate_bigint_columns(conn)


# Колонки, которые хранят Telegram user_id/chat_id — раньше были объявлены как
# INTEGER (4 байта в Postgres, максимум ~2.1 млрд), а такие id уже давно бывают
# больше (супергруппы/каналы -100..., у части новых аккаунтов user_id тоже
# перевалил за 2^31), из-за чего падало "integer out of range". Актуальная
# схема уже создаётся с BIGINT, но CREATE TABLE IF NOT EXISTS не трогает базы,
# созданные раньше со старой схемой, — поэтому докручиваем тип явно на старте.
_BIGINT_COLUMNS = [
    ("users", "user_id"),
    ("users", "spouse_id"),
    ("users", "pending_marriage_from"),
    ("pets", "user_id"),
    ("pets", "fight_opponent_id"),
    ("quests", "user_id"),
    ("daily_quests", "user_id"),
    ("admins", "user_id"),
    ("orders", "user_id"),
    ("owned_skins", "user_id"),
    ("custom_skins", "created_by"),
    ("marriage_action_cooldowns", "user_id"),
    ("potions", "user_id"),
    ("message_owners", "chat_id"),
    ("message_owners", "owner_id"),
    ("known_chats", "chat_id"),
]


def _migrate_bigint_columns(conn):
    """Приводит существующие INTEGER-колонки с Telegram id к BIGINT (только Postgres;
    под SQLite тип — это просто affinity, там всё и так работало, поэтому там
    ничего не делаем). Безопасно запускать повторно при каждом старте."""
    if not USE_POSTGRES:
        return
    cur = conn.cursor()
    for table, col in _BIGINT_COLUMNS:
        try:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name=? AND column_name=?",
                (table, col),
            )
            row = cur.fetchone()
            if row and row["data_type"] == "integer":
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT")
                conn.commit()
        except Exception as e:
            conn.rollback()  # иначе psycopg2 держит соединение в aborted-транзакции
            log.warning("Не удалось привести %s.%s к BIGINT: %s", table, col, e)
    conn.commit()


def get_or_create_user(user_id: int, username: str, display_name: str) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, username, display_name, registered_at) VALUES (?,?,?,?)",
            (user_id, username, display_name, datetime.utcnow().strftime("%d.%m.%Y")),
        )
        conn.commit()
        row = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def find_user_by_username(username: str) -> sqlite3.Row | None:
    """Ищет игрока по @username (регистронезависимо, без учёта уже привязанного '@')."""
    username = username.lstrip("@").strip().lower()
    if not username:
        return None
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE LOWER(username)=?", (username,)
    ).fetchone()
    conn.close()
    return row


def get_pet(user_id: int) -> sqlite3.Row | None:
    conn = db()
    row = conn.execute("SELECT * FROM pets WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def upsert_pet(user_id: int, **fields):
    conn = db()
    cur = conn.cursor()
    existing = cur.execute("SELECT user_id FROM pets WHERE user_id=?", (user_id,)).fetchone()
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        cur.execute(f"UPDATE pets SET {sets} WHERE user_id=?", (*fields.values(), user_id))
    else:
        cols = ", ".join(["user_id", *fields.keys()])
        marks = ", ".join(["?"] * (len(fields) + 1))
        cur.execute(f"INSERT INTO pets ({cols}) VALUES ({marks})", (user_id, *fields.values()))
    conn.commit()
    conn.close()


def update_pet(user_id: int, **fields):
    upsert_pet(user_id, **fields)


def update_user(user_id: int, **fields):
    if not fields:
        return
    conn = db()
    cur = conn.cursor()
    sets = ", ".join(f"{k}=?" for k in fields)
    cur.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))
    conn.commit()
    conn.close()


# =========================================================
#  ЗЕЛЬЯ
# =========================================================

def get_potion_count(user_id: int, potion_type: str) -> int:
    conn = db()
    row = conn.execute(
        "SELECT count FROM potions WHERE user_id=? AND potion_type=?", (user_id, potion_type)
    ).fetchone()
    conn.close()
    return row["count"] if row else 0


def get_all_potion_counts(user_id: int) -> dict[str, int]:
    conn = db()
    rows = conn.execute("SELECT potion_type, count FROM potions WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    result = {row["potion_type"]: row["count"] for row in rows}
    return result


def add_potion(user_id: int, potion_type: str, delta: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO potions (user_id, potion_type, count) VALUES (?,?,?) "
        "ON CONFLICT(user_id, potion_type) DO UPDATE SET count = potions.count + excluded.count",
        (user_id, potion_type, delta),
    )
    conn.commit()
    conn.close()


def get_extra_admin_ids() -> set[int]:
    """Админы, выданные командой !admin, в дополнение к ADMIN_IDS из окружения."""
    conn = db()
    rows = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    return {row["user_id"] for row in rows}


def get_all_admin_ids() -> set[int]:
    return ADMIN_IDS | get_extra_admin_ids()


def is_admin(user_id: int) -> bool:
    return user_id in get_all_admin_ids()


PET_DEFAULT_WEIGHT = 20.0
# По запросу: у части игроков вес улетел до 300-400+ кг из-за частого кормления
# без ограничения сверху. Добавлен потолок — набор веса просто останавливается
# на этом значении, а не начинает вредить балансу. Существующие "разжиревшие"
# питомцы нормализуются до этого потолка один раз при запуске (см. _normalize_pet_weight).
MAX_PET_WEIGHT = 150.0


def create_new_pet(user_id: int, name: str):
    upsert_pet(
        user_id,
        name=name[:24],
        alive=1,
        level=1,
        pet_xp=0,
        weight=PET_DEFAULT_WEIGHT,
        satiety=100,
        bones=0,
        firewood=0,
        raw_meat=0,
        cooked_meat=0,
        last_fed=datetime.utcnow().isoformat(),
        starved_since=None,
        busy_until=None,
        busy_activity=None,
        cooking_until=None,
    )


# =========================================================
#  ИГРОВЫЕ КОНСТАНТЫ
# =========================================================

XP_PER_LEVEL = 130  # по запросу: система опыта была слишком тяжёлой — снизили требования
# Максимальный уровень питомца (по запросу). С каждым уровнем нужно больше
# опыта — см. xp_needed_for_level ниже (растёт линейно с уровнем).
PET_MAX_LEVEL = 70
# По запросу: волк умирает слишком быстро — замедлили падение сытости
# (было 4%/час ≈ полный голод за ~25ч, стало 2%/час ≈ ~50ч)
SATIETY_DECAY_PER_HOUR = 2

# Прогулка в лесу — ровно 1.5 часа, охота — ровно 1 час
FOREST_MIN_SEC, FOREST_MAX_SEC = 90 * 60, 90 * 60
HUNT_MIN_SEC, HUNT_MAX_SEC = 60 * 60, 60 * 60

# Единая система бонуса — используется и командой "бонус", и кнопкой 🎁
# Изменено по запросу: кулдаун бонуса/подарка теперь 24 часа вместо 4.
BONUS_COOLDOWN = timedelta(hours=24)

RENAME_COST_RUNES = 50
REVIVE_COST_RUNES = 100
# По запросу: волк не должен умирать так быстро — увеличен запас времени
# на нуле сытости (было 24-72ч, стало 48-96ч), прежде чем наступит голодная смерть
STARVE_GRACE_MIN_SEC, STARVE_GRACE_MAX_SEC = 48 * 60 * 60, 96 * 60 * 60

# По запросу: смерть от стаи в походах стала гораздо реже и мягче
AMBUSH_CHANCE = 0.12          # было 0.25 — нападения стали в 2 раза реже
AMBUSH_BONES_COST = 3          # было 5 — легче откупиться косточками
AMBUSH_DEATH_CHANCE_IF_FAIL = 0.05   # было 0.15 — смерть даже при неудаче теперь редкость

# По запросу: похудение от походов увеличено и стало разным для леса/охоты
# (охота — более тяжёлая нагрузка, теряется больше веса). При нападении стаи
# волк тратит на бегство/оборону ещё немного веса сверху (см. resolve_activity).
# По запросу: похудение было увеличено, но по новой обратной связи это оказалось
# слишком жёстко в паре со слабым набором — теперь набор веса сильнее, а потеря меньше.
FOREST_WEIGHT_LOSS_MIN, FOREST_WEIGHT_LOSS_MAX = 0.15, 0.35
HUNT_WEIGHT_LOSS_MIN, HUNT_WEIGHT_LOSS_MAX = 0.25, 0.55
AMBUSH_EXTRA_WEIGHT_LOSS_DEFENDED = 0.1   # отбились косточками — тоже устали
AMBUSH_EXTRA_WEIGHT_LOSS_FLED = 0.25       # убегали в панике — потеряли больше веса

# =========================================================
#  МИНИ-ИГРА "ЧЕЙ СЛЕД?" — при возвращении из леса/охоты
# =========================================================
# По запросу: добавлена мини-игра в лес/охоту. Пока волк "идёт обратно",
# игроку показываются 3 варианта следов на земле — угадавшему даётся бонус
# к добыче/опыту и меньшая потеря веса. Если игрок не ответит вовремя —
# результат забирается автоматически, без бонуса (см. resolve_stale_activity_games).
ANIMAL_TRACK_LABELS = {
    "заяц": "🐇 Заяц",
    "олень": "🦌 Олень",
    "кабан": "🐗 Кабан",
}
GAME_WIN_LOOT_BONUS_PERCENT = 25   # +25% к добыче и опыту при угадывании следа
GAME_WIN_WEIGHT_LOSS_MULT = 0.6    # угадал — меньше устал, вес теряется на 40% меньше
GAME_TIMEOUT_SECONDS = 10 * 60     # если не ответил за 10 минут — авто-результат без бонуса

# Готовка мяса на костре: 1 мясо + дрова -> немного готового мяса
# Расход дров при готовке случайный (0.25–2)
# По запросу: расход дров теперь случайный при каждой готовке (от 0.25 до 2)
CAMPFIRE_WOOD_COST_MIN, CAMPFIRE_WOOD_COST_MAX = 0.25, 2.0
CAMPFIRE_MEAT_COST = 1
CAMPFIRE_COOK_SECONDS = 20 * 60
CAMPFIRE_MEAT_OUTPUT_MIN, CAMPFIRE_MEAT_OUTPUT_MAX = 1, 1

FEED_COOKED_MEAT_COST = 1
FEED_SATIETY_GAIN = 100  # по запросу: 1 мясо полностью восстанавливает сытость (100%)
FEED_MAX_SATIETY = 99  # кормить своего питомца можно только если сытость меньше этого значения
# по запросу: было слишком медленно (0.02-0.05) — набор веса от кормления
# снова увеличен, теперь с потолком в 150 кг это не проблема.
FEED_WEIGHT_GAIN_MIN = 0.5
FEED_WEIGHT_GAIN_MAX = 1.2
PARTNER_FEED_MAX_SATIETY = 70  # по запросу: партнёра можно покормить, если сытость меньше 70%

# анти-флуд для напоминания "ответьте реплаем" (см. handle_pending_text)
_last_pending_nudge: dict[int, datetime] = {}
PENDING_NUDGE_COOLDOWN = timedelta(minutes=2)

# Скины волка — цена в долларах + id стикера Telegram для каждого скина.
# file_id стикеров заданы вручную и жёстко (раньше они подтягивались из
# стикерпака по ПОРЯДКУ стикеров в паке, а это порядок ничем не гарантирован —
# из-за этого скины у игроков получались "случайными", не совпадающими с названием).
SKINS = {
    "Wolf": 0,
    "Один": 500,
    "Тор": 450,
    "Фрейр": 350,
    "Фрейя": 350,
    "Локи": 400,
}
SKIN_STICKERS: dict[str, str] = {
    "Wolf": "CAACAgIAAx0Ee2bdnQABNcwaap0JagGHMXqGP6dQJK_lKfhBSCoAAkiAAAIlfgFIYjX7VvIKYBw9BA",
    "Тор": "CAACAgIAAxUAAWqqM8Uso7KyQfrtAjCRTsqCZ6seAALqfwACxL0wSf22fDTaL2B3PQQ",
    "Фрейр": "CAACAgIAAx0Cdw_nLgABHERZaqVgjbNEOJ5ZelBBi1y78ZP-E2cAArB0AAIsLEBJRR7-KmJFyk49BA",
    "Фрейя": "CAACAgIAAx0Cdw_nLgABHERaaqVgr_6m8SRRSyWK0poBsTyuCocAAkWBAAIp-0hJMUg9hS2r5rE9BA",
    "Один": "CAACAgIAAxUAAWqqM8W2wdnCm8NFAlyGkdnYhjb5AAJbdQACoGfxSUl3bi0kHvzUPQQ",
    "Локи": "CAACAgIAAx0Edw_nLgABHDZkaqAHghRh43h1OV8wj6XH9XRSgTcAAmOJAAKUSpFJOQ4uoBJW7lo9BA",
}


# =========================================================
#  СКИНЫ — покупаются один раз навсегда, дальше можно свободно
#  "надевать" их из вкладки скинов в меню волка
# =========================================================

def get_owned_skins(user_id: int) -> set[str]:
    conn = db()
    rows = conn.execute("SELECT skin_name FROM owned_skins WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    owned = {row["skin_name"] for row in rows}
    owned.add("Wolf")  # стандартный скин доступен всем бесплатно
    return owned


def owns_skin(user_id: int, skin_name: str) -> bool:
    # БАГФИКС: раньше SKINS.get(skin_name, 0) == 0 давал True для ЛЮБОГО
    # неизвестного имени скина (не только Wolf) — то есть кастомный скин
    # (не из словаря SKINS) считался бы "бесплатным и уже купленным" для всех.
    # get_owned_skins() и так всегда включает "Wolf", так что достаточно
    # проверять реальное владение.
    return skin_name in get_owned_skins(user_id)


def skin_exists(skin_name: str) -> bool:
    """Скин существует, если он либо в базовом списке SKINS, либо был
    зарегистрирован как кастомный (см. register_custom_skin / custom_skins)."""
    if skin_name in SKINS:
        return True
    conn = db()
    row = conn.execute("SELECT 1 FROM custom_skins WHERE name=?", (skin_name,)).fetchone()
    conn.close()
    return row is not None


def get_custom_skin_sticker(skin_name: str) -> str | None:
    conn = db()
    row = conn.execute("SELECT sticker_id FROM custom_skins WHERE name=?", (skin_name,)).fetchone()
    conn.close()
    return row["sticker_id"] if row and row["sticker_id"] else None


def register_custom_skin(skin_name: str, sticker_id: str, created_by: int | None = None):
    """Регистрирует (или обновляет стикер) кастомного скина — по запросу, теперь
    в качестве скина можно использовать ЛЮБОЙ стикер, не только 5 захардкоженных
    в SKIN_STICKERS."""
    conn = db()
    conn.execute(
        "INSERT INTO custom_skins (name, sticker_id, created_by, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET sticker_id=excluded.sticker_id",
        (skin_name, sticker_id, created_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_custom_skin_names(user_id: int | None = None) -> list[str]:
    """Список кастомных скинов; если передан user_id — только те, которыми он владеет."""
    conn = db()
    if user_id is None:
        rows = conn.execute("SELECT name FROM custom_skins ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            "SELECT cs.name FROM custom_skins cs "
            "JOIN owned_skins os ON os.skin_name = cs.name AND os.user_id=? ORDER BY cs.name",
            (user_id,),
        ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def grant_skin_ownership(user_id: int, skin_name: str):
    conn = db()
    conn.execute(
        "INSERT INTO owned_skins (user_id, skin_name) VALUES (?,?) "
        "ON CONFLICT (user_id, skin_name) DO NOTHING", (user_id, skin_name)
    )
    conn.commit()
    conn.close()
RING_COST_DOLLARS = 200
ANTITARGET_PRICES = {"1 день": (1, 15), "3 дня": (3, 35), "7 дней": (7, 70)}
PREFIX_PRICES = {"1 день": (1, 10), "3 дня": (3, 25), "7 дней": (7, 55)}

# Лабиринт — упрощён: меньше шагов, больше допустимых ошибок, приз скромнее, без колец
MAZE_KEY_COST = 1
MAZE_STEPS_TO_WIN = 6
MAZE_MAX_MISTAKES = 4
MAZE_DIRECTIONS = ["left", "straight", "right"]
MAZE_DIRECTION_LABELS = {"left": "⬅️ Влево", "straight": "⬆️ Прямо", "right": "➡️ Вправо"}
MAZE_WIN_RUNES = 8
# По запросу: доллары теперь даются только за бонус и лабиринт (и немного за квесты)
MAZE_WIN_DOLLARS_MIN, MAZE_WIN_DOLLARS_MAX = 1, 10
# по запросу: очень редкий шанс найти ларец прямо в лабиринте (см. cb_maze_move)
MAZE_LARETS_DROP_CHANCE = 0.03

# =========================================================
#  ЛАРЕЦ — редкий предмет, открывается 3 ключами, приз — случайный
#  из таблицы LARETS_PRIZES (по запросу)
# =========================================================
LARETS_OPEN_KEY_COST = 3
# приз выбирается взвешенным случайным выбором (чем больше вес — тем чаще выпадает)
LARETS_PRIZES = [
    {"label": "300 💵", "weight": 25, "grant": {"dollars": 300}},
    {"label": "2 🧪 зелья силы", "weight": 15, "grant": {"potion": ("strength", 2)}},
    {"label": "2 🧪 зелья скорости", "weight": 15, "grant": {"potion": ("speed", 2)}},
    {"label": "2 🧪 зелья добычи", "weight": 15, "grant": {"potion": ("loot", 2)}},
    {"label": "6 🀄 рун", "weight": 15, "grant": {"runes": 6}},
    {"label": "1 💍 кольцо", "weight": 8, "grant": {"rings": 1}},
    {"label": "18 🀄 рун", "weight": 4, "grant": {"runes": 18}},
    {"label": "900 💵 + 1 💍 кольцо", "weight": 2, "grant": {"dollars": 900, "rings": 1}},
    {"label": "🎭 Эксклюзивный скин «Счастливчик»", "weight": 1, "grant": {"skin": "Счастливчик"}},
]

# Бой волков — теперь идёт 30 минут, а не мгновенно
FIGHT_DURATION_SECONDS = 30 * 60
# По запросу: на бой теперь нужно согласие соперника в чате — вызов не стартует
# сражение сразу, а ждёт ответа (см. start_wolf_fight / cb_fight_accept/decline).
FIGHT_CHALLENGE_ACTIVITY = "вызов_боя"   # отдельное значение busy_activity, отличное от "бой"
FIGHT_CHALLENGE_TIMEOUT_SECONDS = 5 * 60  # если не ответил за 5 минут — вызов сгорает

# Брак — стоимость и настройки совместных действий
MARRIAGE_XP_PER_LEVEL = 500
JOINT_ACTIVITY_BONUS_XP_PERCENT = 20  # бонус опыта питомцу при совместной охоте/лесе
JOINT_ACTIVITY_SPEED_BONUS_PERCENT = 30  # вместе с партнёром волк возвращается быстрее
JOINT_ACTIVITY_LOOT_BONUS_PERCENT = 30   # и добыча больше
MARRIAGE_ACTION_LEVEL_GROWTH_PERCENT = 5  # награда за совместные действия растёт с уровнем пары

# Ссылка на пак стикеров со скинами
SKIN_STICKER_PACK = "guomaoguo"

# =========================================================
#  ЗЕЛЬЯ (магазин) — цены 5-20 долларов
# =========================================================
# strength — +25% силы в ближайшем бою
# speed    — -25% времени возвращения из леса/охоты
# loot     — +25% добычи из леса/охоты
# cooking  — мгновенно жарит мясо без затрат дров (1 мясо -> 2 готового)
POTION_STRENGTH_BONUS_PERCENT = 50
POTION_SPEED_BONUS_PERCENT = 50
POTION_LOOT_BONUS_PERCENT = 50
POTION_COOKING_OUTPUT = 2  # сколько готового мяса даёт 1 сырое мясо с зельем жарки

POTION_DEFS = {
    "strength": {"title": "Зелье силы", "icon": "💪", "price": 15,
                 "desc": f"+{POTION_STRENGTH_BONUS_PERCENT}% к силе в ближайшем бою"},
    "speed": {"title": "Зелье скорости", "icon": "🏃", "price": 12,
              "desc": f"-{POTION_SPEED_BONUS_PERCENT}% времени похода в лес/на охоту"},
    "loot": {"title": "Зелье добычи", "icon": "🎒", "price": 18,
             "desc": f"+{POTION_LOOT_BONUS_PERCENT}% добычи из ближайшего похода в лес/на охоту"},
    "cooking": {"title": "Зелье жарки", "icon": "🔥", "price": 10,
                "desc": f"Мгновенно жарит мясо без затрат дров (1 🥩 → {POTION_COOKING_OUTPUT} 🍖)"},
}

# При низкой сытости волк приносит меньше добычи (штраф)
LOW_SATIETY_THRESHOLD = 30
LOW_SATIETY_PENALTY_PERCENT = 70  # на столько % меньше добычи при низкой сытости

# Шанс принести мясо из похода в лес (не только на охоте)
FOREST_MEAT_CHANCE = 0.30


def xp_needed_for_level(level: int) -> int:
    return level * XP_PER_LEVEL


def apply_satiety_decay(pet: sqlite3.Row) -> int:
    if not pet["last_fed"]:
        return pet["satiety"]
    last_fed = datetime.fromisoformat(pet["last_fed"])
    hours = max(0.0, (datetime.utcnow() - last_fed).total_seconds() / 3600)
    decay = int(hours * SATIETY_DECAY_PER_HOUR)
    return max(0, pet["satiety"] - decay)


async def reply_or_send(message: Message, text: str, **kwargs):
    """По запросу: все ответы пользователю теперь отправляются реплаем на его
    сообщение (в группе сразу видно, кому и на что отвечает бот). Если реплай
    по какой-то причине невозможен (например, исходное сообщение успели удалить),
    тихо откатываемся на обычную отправку, чтобы бот не падал."""
    try:
        return await message.reply(text, **kwargs)
    except TelegramBadRequest:
        return await message.answer(text, **kwargs)


def pet_is_busy(pet: sqlite3.Row) -> bool:
    # питомец также считается занятым, пока не отвечена мини-игра "чей след?"
    # (иначе можно отправить его в новый поход поверх незавершённого предыдущего)
    if pet["game_pending"]:
        return True
    if not pet["busy_until"]:
        return False
    return datetime.utcnow() < datetime.fromisoformat(pet["busy_until"])


def fmt_timedelta(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def grant_pet_xp(pet: sqlite3.Row, amount: int):
    # Максимум 70 уровней (п. "Уровень максимально 70"). На макс. уровне
    # опыт больше не растрачивается на левелап — он просто не начисляется,
    # чтобы не копился бессмысленно.
    if pet["level"] >= PET_MAX_LEVEL:
        return pet["pet_xp"], pet["level"]

    new_xp = pet["pet_xp"] + amount
    new_level = pet["level"]
    levels_gained = 0
    needed = xp_needed_for_level(new_level)
    while new_xp >= needed and new_level < PET_MAX_LEVEL:
        new_xp -= needed
        new_level += 1
        levels_gained += 1
        needed = xp_needed_for_level(new_level)
    if new_level >= PET_MAX_LEVEL:
        new_level = PET_MAX_LEVEL
        new_xp = 0  # максимальный уровень достигнут — опыт дальше не нужен
    update_pet(pet["user_id"], pet_xp=new_xp, level=new_level)
    if levels_gained:
        record_quest_progress(pet["user_id"], "pet_level_up", levels_gained)
    return new_xp, new_level


def grant_rewards(user_id: int, xp=0, dollars=0, keys=0, runes=0, rings=0):
    user = get_or_create_user(user_id, "", "")
    update_user(
        user_id,
        xp=user["xp"] + xp,
        dollars=user["dollars"] + dollars,
        keys=user["keys"] + keys,
        runes=user["runes"] + runes,
        rings=user["rings"] + rings,
    )


# =========================================================
#  КВЕСТЫ (только ежедневные, случайно выбираемые из пула)
# =========================================================

DAILY_QUEST_PERIOD = timedelta(hours=24)
DAILY_QUEST_COUNT = 3  # сколько случайных заданий выдаётся игроку на день

# Пул возможных ежедневных заданий — каждый день игроку выпадает
# DAILY_QUEST_COUNT случайных штук из этого списка.
QUEST_DEFS = {
    # по запросу: доллары за квесты теперь дают совсем немного (1-3)
    "feed_pet": {"target": 1, "title": "Покормить волка", "reward": dict(xp=5)},
    "send_forest": {"target": 1, "title": "Отправить питомца в лес", "reward": dict(xp=5, dollars=2)},
    "send_hunt": {"target": 1, "title": "Отправить питомца на охоту", "reward": dict(xp=5, dollars=2)},
    "use_bonus": {"target": 1, "title": "Забрать ежедневный бонус", "reward": dict(keys=5)},
    "cook_meat": {"target": 2, "title": "Приготовить 2 порции мяса на костре", "reward": dict(dollars=3, keys=3)},
    "hunt_3_times": {"target": 3, "title": "Отправиться на охоту 3 раза", "reward": dict(keys=10)},
    "collect_bones_5": {"target": 5, "title": "Найти 5 косточек", "reward": dict(dollars=2)},
    "win_fight": {"target": 1, "title": "Победить в бою", "reward": dict(runes=5)},
    "pet_level_up": {"target": 1, "title": "Повысить уровень питомца", "reward": dict(dollars=3)},
}


def get_daily_quest_ids(user_id: int) -> list[str]:
    """Возвращает набор из DAILY_QUEST_COUNT случайных заданий на сегодня,
    выбирая заново раз в 24 часа."""
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM daily_quests WHERE user_id=?", (user_id,)).fetchone()
    now = datetime.utcnow()
    if row is None or now - datetime.fromisoformat(row["assigned_at"]) >= DAILY_QUEST_PERIOD:
        pool = list(QUEST_DEFS.keys())
        chosen = random.sample(pool, k=min(DAILY_QUEST_COUNT, len(pool)))
        ids_str = ",".join(chosen)
        if row is None:
            cur.execute(
                "INSERT INTO daily_quests (user_id, quest_ids, assigned_at) VALUES (?,?,?)",
                (user_id, ids_str, now.isoformat()),
            )
        else:
            cur.execute(
                "UPDATE daily_quests SET quest_ids=?, assigned_at=? WHERE user_id=?",
                (ids_str, now.isoformat(), user_id),
            )
        for qid in chosen:
            cur.execute("DELETE FROM quests WHERE user_id=? AND quest_id=?", (user_id, qid))
        conn.commit()
        result = chosen
    else:
        result = row["quest_ids"].split(",") if row["quest_ids"] else []
    conn.close()
    return result


def ensure_quest_row(user_id: int, quest_id: str) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT * FROM quests WHERE user_id=? AND quest_id=?", (user_id, quest_id)
    ).fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO quests (user_id, quest_id, period_start, progress, completed) VALUES (?,?,?,0,0)",
            (user_id, quest_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        row = cur.execute(
            "SELECT * FROM quests WHERE user_id=? AND quest_id=?", (user_id, quest_id)
        ).fetchone()
    conn.close()
    return row


async def _notify_quest_complete(user_id: int, quest_id: str):
    qdef = QUEST_DEFS[quest_id]
    reward = qdef["reward"]
    grant_rewards(user_id, **reward)
    parts = []
    if reward.get("xp"):
        parts.append(f"+{reward['xp']} ⭐ опыта игроку")
    if reward.get("dollars"):
        parts.append(f"+{reward['dollars']} 💵")
    if reward.get("keys"):
        parts.append(f"+{reward['keys']} 🔑 ключей")
    if reward.get("runes"):
        parts.append(f"+{reward['runes']} 🀄 рун")
    if reward.get("rings"):
        parts.append(f"+{reward['rings']} 💍 колец")

    try:
        await bot.send_message(
            user_id,
            f"✅ Задание выполнено: «{qdef['title']}»!\nНаграда: {', '.join(parts)}.",
        )
    except Exception as e:
        log.warning("Не удалось уведомить о квесте %s: %s", user_id, e)


def record_quest_progress(user_id: int, quest_id: str, amount: float = 1):
    # засчитывается только если задание реально выпало игроку сегодня
    if quest_id not in QUEST_DEFS or quest_id not in get_daily_quest_ids(user_id):
        return
    row = ensure_quest_row(user_id, quest_id)
    if row["completed"]:
        return
    qdef = QUEST_DEFS[quest_id]
    new_progress = row["progress"] + amount
    completed = 1 if new_progress >= qdef["target"] else 0
    conn = db()
    conn.execute(
        "UPDATE quests SET progress=?, completed=? WHERE user_id=? AND quest_id=?",
        (new_progress, completed, user_id, quest_id),
    )
    conn.commit()
    conn.close()
    if completed:
        asyncio.create_task(_notify_quest_complete(user_id, quest_id))


def _reward_str(reward: dict) -> str:
    icons = {"xp": "⭐", "dollars": "💵", "keys": "🔑", "runes": "🀄", "rings": "💍"}
    parts = [f"+{v}{icons.get(k, '')}" for k, v in reward.items() if v]
    return " ".join(parts)


def _progress_bar(progress: float, target: float, width: int = 10) -> str:
    ratio = 0 if target <= 0 else min(1.0, progress / target)
    filled = round(ratio * width)
    return "▓" * filled + "░" * (width - filled)


def quests_text(user_id: int) -> str:
    # только ежедневные случайные задания, с прогресс-барами и наградами
    lines = ["🎯 З А Д А Н И Я", "《 📅 Ежедневные задания 》", ""]
    for qid in get_daily_quest_ids(user_id):
        qdef = QUEST_DEFS[qid]
        row = ensure_quest_row(user_id, qid)
        mark = "✅" if row["completed"] else "▫️"
        progress = min(row["progress"], qdef["target"])
        percent = 100 if row["completed"] else int(progress / qdef["target"] * 100)
        bar = _progress_bar(progress, qdef["target"])
        lines.append(f"{mark} {qdef['title']}")
        lines.append(f"   {bar} {progress:g}/{qdef['target']:g} ({percent}%)")
        lines.append(f"   🏆 Награда: {_reward_str(qdef['reward'])}")
    lines.append("")
    lines.append("Новый набор заданий выпадет через 24 часа после получения текущего.")
    return "\n".join(lines).strip()


def quests_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ Назад в профиль", callback_data="back_profile")],
    ])


# =========================================================
#  КЛАВИАТУРЫ
# =========================================================

def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁", callback_data="gift"),
            InlineKeyboardButton(text="🐾", callback_data="pet_menu"),
            InlineKeyboardButton(text="🎯 Квесты", callback_data="quests"),
        ],
        [InlineKeyboardButton(text="🔑 Лабиринт", callback_data="maze")],
        [InlineKeyboardButton(text="🏪 Магазин", callback_data="shop")],
    ])


def pet_menu_kb(busy: bool, pet: sqlite3.Row | None = None) -> InlineKeyboardMarkup:
    # /меню волка показывается полностью, даже когда питомец занят;
    # отдельные кнопки при нажатии сами сообщат, что волк занят.
    # кнопка входа/выхода с арены живёт теперь только внутри самой
    # арены (arena_kb), а не дублируется в общем меню волка.
    top_row = [
        InlineKeyboardButton(text="🌲 Лес", callback_data="forest"),
        InlineKeyboardButton(text="🏹 Охота", callback_data="hunt"),
    ]
    second_row = []
    # кнопка "Покормить" видна только когда хватает готового мяса
    if pet is not None and pet["cooked_meat"] >= FEED_COOKED_MEAT_COST:
        second_row.append(InlineKeyboardButton(text="🍖 Покормить", callback_data="feed"))
    second_row.append(InlineKeyboardButton(text="🔥 Костёр", callback_data="campfire"))
    return InlineKeyboardMarkup(inline_keyboard=[
        top_row,
        second_row,
        [
            InlineKeyboardButton(text="🎭 Скин", callback_data="skin_menu"),
            InlineKeyboardButton(text="⚔️ Арена", callback_data="arena"),
        ],
        [InlineKeyboardButton(text="🧪 Зелья", callback_data="potions_menu")],
        [InlineKeyboardButton(text=f"✏️ Сменить имя ({RENAME_COST_RUNES}🀄)", callback_data="rename")],
        [InlineKeyboardButton(text="⬅ Назад в профиль", callback_data="back_profile")],
    ])


def pet_skins_kb(user_id: int, current_skin: str | None = None) -> InlineKeyboardMarkup:
    # кнопка текущего надетого скина сразу помечается галочкой
    owned = get_owned_skins(user_id)
    rows = []
    for name, price in SKINS.items():
        if name == current_skin:
            label = f"✅ {name} (надето)"
        elif name in owned:
            label = f"🐺 {name} (надеть)"
        else:
            label = f"{name} — {price}💵 (купить)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy_skin_{name}")])
    # по запросу: кастомные скины (выданные админом/из ларца через стикер) —
    # их нет в магазине, показываем только если реально есть в собственности
    for name in get_all_custom_skin_names(user_id):
        label = f"✅ {name} (надето)" if name == current_skin else f"🎭 {name} (надеть)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy_skin_{name}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад к питомцу", callback_data="pet_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def arena_kb(opponents: list[sqlite3.Row], in_arena: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"⚔️ {o['name']} (ур. {o['level']}) — {o['display_name']}",
            callback_data=f"fight_{o['user_id']}",
        )]
        for o in opponents
    ]
    # прямо с арены тоже можно выйти из неё, если ты в ней сейчас сидишь
    if in_arena:
        rows.append([InlineKeyboardButton(text="🚪 Выйти с арены", callback_data="arena_toggle")])
    else:
        rows.append([InlineKeyboardButton(text="🚩 Войти на арену", callback_data="arena_toggle")])
    rows.append([InlineKeyboardButton(text="⬅ Назад к питомцу", callback_data="pet_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def campfire_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔥 Готовить мясо", callback_data="campfire_cook")]]
    if get_potion_count(user_id, "cooking") > 0:
        rows.append([InlineKeyboardButton(
            text=f"🧪 Зелье жарки (мгновенно, 1🥩→{POTION_COOKING_OUTPUT}🍖)",
            callback_data="campfire_potion_cook",
        )])
    rows.append([InlineKeyboardButton(text="⬅ Назад к питомцу", callback_data="pet_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def revive_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✨ Воскресить ({REVIVE_COST_RUNES}🀄)", callback_data="revive")],
        [InlineKeyboardButton(text="💔 Не воскрешать", callback_data="decline_revive")],
    ])


def shop_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📛 Префикс", callback_data="shop_prefix")],
        [InlineKeyboardButton(text="🐺 Скины", callback_data="shop_skins")],
        [InlineKeyboardButton(text="🧪 Зелья", callback_data="shop_potions")],
        [InlineKeyboardButton(text="💍 Кольца (брак)", callback_data="shop_rings")],
        [InlineKeyboardButton(text="🛡 Анти-таргет", callback_data="shop_antitarget")],
        [InlineKeyboardButton(text="⬅ Назад в профиль", callback_data="back_profile")],
    ])


def shop_potions_kb(user_id: int) -> InlineKeyboardMarkup:
    owned = get_all_potion_counts(user_id)
    rows = []
    for ptype, info in POTION_DEFS.items():
        have = owned.get(ptype, 0)
        rows.append([InlineKeyboardButton(
            text=f"{info['icon']} {info['title']} — {info['price']}💵 (есть: {have})",
            callback_data=f"buy_potion_{ptype}",
        )])
    rows.append([InlineKeyboardButton(text="⬅ Назад в магазин", callback_data="shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_prefix_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"Купить префикс на {label} — {price}💵", callback_data=f"buy_prefix_{days}")]
        for label, (days, price) in PREFIX_PRICES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅ Назад в магазин", callback_data="shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_antitarget_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"Купить анти-таргет на {label} — {price}💵", callback_data=f"buy_at_{days}")]
        for label, (days, price) in ANTITARGET_PRICES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅ Назад в магазин", callback_data="shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_skins_kb(user_id: int) -> InlineKeyboardMarkup:
    owned = get_owned_skins(user_id)
    rows = []
    for name, price in SKINS.items():
        label = f"✅ {name} (уже куплен)" if name in owned else f"{name} — {price}💵"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy_skin_{name}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад в магазин", callback_data="shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_rings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💍 Купить кольцо — {RING_COST_DOLLARS}💵", callback_data="buy_ring")],
        [InlineKeyboardButton(text="⬅ Назад в магазин", callback_data="shop")],
    ])


# =========================================================
#  ТЕКСТЫ
# =========================================================

def profile_text(user: sqlite3.Row, pet: sqlite3.Row | None) -> str:
    if pet and pet["alive"]:
        pet_line = f"{pet['name']} (ур. {pet['level']})"
    elif pet and pet["name"]:
        pet_line = f"{pet['name']} — 💔 погиб"
    else:
        pet_line = "нет 🐾"

    if user["spouse_id"]:
        spouse = get_or_create_user(user["spouse_id"], "", "")
        spouse_line = f"💍 {spouse['display_name']}"
    else:
        spouse_line = "нет"

    return (
        "👤 ПРОФИЛЬ ИГРОКА 💬\n\n"
        f"▫️ Имя: {user['display_name']}\n"
        f"▪️ ID: {user['user_id']}\n"
        f"▫️ Регистрация: {user['registered_at']}\n"
        f"▪️ Ранг игрока: {user['rank']}\n"
        f"▫️ Пара: {spouse_line}\n"
        "————————————\n"
        "💰 БАЛАНС:\n"
        f"💵 Доллары: {user['dollars']}\n"
        f"🔑 Ключи: {user['keys']}\n"
        f"🀄 Руны: {user['runes']}\n"
        f"⭐ Опыт: {user['xp']}\n"
        f"💍 Кольца: {user['rings']}\n"

        "————————————\n"
        f"🐺 Питомец — {pet_line}"
    )


def pet_text(pet: sqlite3.Row, satiety_now: int) -> str:
    if pet["level"] >= PET_MAX_LEVEL:
        level_line = f"⭐ Уровень: {pet['level']} (МАКС.)\n"
    else:
        level_line = f"⭐ Уровень: {pet['level']} ({pet['pet_xp']}/{xp_needed_for_level(pet['level'])} XP)\n"
    return (
        f"🐾 {pet['name']}\n"
        "————————————\n"
        f"{level_line}"
        f"🎭 Скин: {pet['skin']}\n"
        f"⚖️ Вес: {pet['weight']:.1f} / {MAX_PET_WEIGHT:.0f} кг\n"
        f"🥩 Сытость: {satiety_now}%\n"
        f"🦴 Косточки: {pet['bones']}\n"
        f"🪵 Дрова: {pet['firewood']:.1f}   🥩 Плоть: {pet['raw_meat']}   🍖 Готового мяса: {pet['cooked_meat']}"
    )


def campfire_text(pet: sqlite3.Row) -> str:
    lines = [
        "🔥 Костёр",
        "————————————",
        f"🪵 Дрова: {pet['firewood']:.2f}",
        f"🥩 Плоть: {pet['raw_meat']}",
        f"🍖 Готовое мясо: {pet['cooked_meat']}",
    ]
    if pet["cooking_until"]:
        until = datetime.fromisoformat(pet["cooking_until"])
        if datetime.utcnow() < until:
            remaining = until - datetime.utcnow()
            lines.append(f"\n♨️ Жаровня: мясо жарится (осталось {fmt_timedelta(remaining)})")
    return "\n".join(lines)


def potions_text(user_id: int, pet: sqlite3.Row) -> str:
    owned = get_all_potion_counts(user_id)
    lines = ["🧪 ЗЕЛЬЯ (инвентарь)", "————————————"]
    for ptype, info in POTION_DEFS.items():
        have = owned.get(ptype, 0)
        lines.append(f"{info['icon']} {info['title']}: {have} шт.\n   {info['desc']}")
    lines.append("")
    lines.append("Купить зелья можно в 🏪 Магазине → 🧪 Зелья.")
    lines.append(
        "Зелья не активируются заранее — бот сам предложит использовать нужное "
        "зелье прямо в момент действия: перед походом в 🌲 лес/🏹 охоту, перед ⚔️ боем "
        "или на 🔥 костре."
    )
    return "\n".join(lines)


def potions_kb(user_id: int, pet: sqlite3.Row) -> InlineKeyboardMarkup:
    owned = get_all_potion_counts(user_id)
    rows = []
    if owned.get("cooking", 0) > 0:
        rows.append([InlineKeyboardButton(text="🔥 Использовать зелье жарки", callback_data="campfire_potion_cook")])
    rows.append([InlineKeyboardButton(text="🏪 В магазин зелий", callback_data="shop_potions")])
    rows.append([InlineKeyboardButton(text="⬅ Назад к питомцу", callback_data="pet_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def safe_edit_or_send(target: Message, text: str, kb: InlineKeyboardMarkup | None = None,
                             owner_id: int | None = None):
    """Пытается изменить существующее сообщение (без флуда, ).
    Если не получилось (нет прав / сообщение не бота / устарело) — отправляет новое.

    БАГФИКС: раньше при повторном нажатии одной и той же кнопки (когда текст
    и клавиатура не меняются) Telegram отвечает ошибкой "message is not
    modified" — это НЕ настоящая ошибка, а просто "менять нечего". Старый код
    ловил её тем же except'ом, что и реальные ошибки, и в ответ слал НОВОЕ
    сообщение — отсюда и жалоба на "спам" новых сообщений при частых кликах
    по меню. Теперь такой случай считается успехом и новое сообщение не шлётся.
    """
    try:
        if target.photo:
            await target.edit_caption(caption=text, reply_markup=kb)
        else:
            await target.edit_text(text, reply_markup=kb)
        if owner_id is not None:
            remember_owner(target.chat.id, target.message_id, owner_id)
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            # содержимое и так уже актуально — новое сообщение слать не нужно
            if owner_id is not None:
                remember_owner(target.chat.id, target.message_id, owner_id)
            return
    except Exception:
        pass
    try:
        sent = await target.answer(text, reply_markup=kb)
        if owner_id is not None:
            remember_owner(sent.chat.id, sent.message_id, owner_id)
    except Exception as e:
        log.warning("Не удалось отправить сообщение: %s", e)


async def send_pet_screen(chat_id: int, user_id: int, edit_message: Message | None = None):
    pet = get_pet(user_id)

    if pet is None or (not pet["alive"] and not pet["name"]):
        text = (
            "🐾 У вас пока нет питомца. Придумайте имя вашему будущему волку "
            "и ОТВЕТЬТЕ РЕПЛАЕМ на это сообщение с именем:\n\n"
            "⚠️ Если у вас был волк, и мы его, к сожалению, потеряли, "
            "напишите — @n0irxs"
        )
        if edit_message is not None:
            sent = edit_message
            await safe_edit_or_send(edit_message, text, owner_id=user_id)
        else:
            sent = await bot.send_message(chat_id, text)
            remember_owner(sent.chat.id, sent.message_id, user_id)
        # запоминаем id этого сообщения, чтобы принять имя только ответом на него
        update_user(user_id, pending_action="tame", pending_msg_id=sent.message_id)
        return

    if not pet["alive"]:
        # питомец мёртв, но ранее имел имя -> предложить воскресить
        text = (
            f"Ваш питомец «{pet['name']}» мёртв. 💔\n"
            f"Вы можете воскресить его за {REVIVE_COST_RUNES} 🀄 рун, либо попрощаться и приручить нового."
        )
        if edit_message is not None:
            await safe_edit_or_send(edit_message, text, revive_kb(), owner_id=user_id)
        else:
            sent = await bot.send_message(chat_id, text, reply_markup=revive_kb())
            remember_owner(sent.chat.id, sent.message_id, user_id)
        return

    # меню волка (статы, опыт, ресурсы и т.п.) показывается ПОЛНОСТЬЮ
    # вне зависимости от того, занят питомец или нет; если занят — к статам
    # просто добавляется строка с занятием и временем возвращения.
    busy = pet_is_busy(pet)
    busy_note = ""
    if pet["game_pending"]:
        busy_note = "\n\n🐾 Питомец стоит у следов и ждёт вашего ответа в мини-игре «Чей след?» ⬆️"
    elif busy:
        until = datetime.fromisoformat(pet["busy_until"])
        remaining = until - datetime.utcnow()
        activity = pet["busy_activity"] or "делах"
        busy_note = f"\n\n⏳ Сейчас в занятии: {activity}\nВернётся через {fmt_timedelta(remaining)}"

    # сытость (и, соответственно, риск голодной смерти) считается всегда,
    # вне зависимости от занятости питомца
    satiety_now = apply_satiety_decay(pet)
    if satiety_now > 0:
        update_pet(user_id, satiety=satiety_now, starved_since=None)
    else:
        update_pet(user_id, satiety=satiety_now, starved_since=pet["starved_since"] or datetime.utcnow().isoformat())
    pet = get_pet(user_id)

    caption = pet_text(pet, satiety_now) + busy_note
    kb = pet_menu_kb(busy=busy, pet=pet)

    if edit_message is not None:
        await safe_edit_or_send(edit_message, caption, kb, owner_id=user_id)
        return

    await _send_skin_sticker(chat_id, pet["skin"])
    sent = await bot.send_message(chat_id, caption, reply_markup=kb)
    remember_owner(sent.chat.id, sent.message_id, user_id)


async def _send_skin_sticker(chat_id: int, skin: str):
    """стикер текущего скина отправляется отдельным сообщением сверху,
    всегда, вне зависимости от занятости питомца."""
    sticker_id = SKIN_STICKERS.get(skin) or get_custom_skin_sticker(skin) or SKIN_STICKERS.get("Wolf")
    if sticker_id:
        try:
            await bot.send_sticker(chat_id, sticker_id)
            return
        except Exception as e:
            log.warning("Не удалось отправить стикер скина: %s", e)
    if os.path.exists(WOLF_IMAGE_PATH):
        try:
            await bot.send_photo(chat_id, FSInputFile(WOLF_IMAGE_PATH))
        except Exception as e:
            log.warning("Не удалось отправить изображение волка: %s", e)


# =========================================================
#  КОМАНДЫ
# =========================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await reply_or_send(message, 
        "Добро пожаловать! 🐺\n"
        "«профиль» — ваша карточка игрока\n"
        "«волк» — питомец (и приручение, если его ещё нет)\n"
        "«бонус» — забрать периодический бонус\n"
        "«квесты» — список заданий\n\n"
        "Полный список команд: /help"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 ПОМОЩЬ ПО БОТУ\n"
        "————————————\n\n"
        "🐺 ОСНОВНОЕ\n"
        "«профиль» — карточка игрока\n"
        "«волк» — ваш питомец (приручение / уход)\n"
        "«твой волк» (ответом или @username) — посмотреть чужого питомца\n"
        "«бонус» / «подарок» — забрать периодический бонус (раз в 24ч)\n"
        "«квесты» / «задания» — список заданий\n\n"
        "🌲 ПИТОМЕЦ\n"
        "«лес» / «охота» — кнопки в меню волка, отправить питомца добывать ресурсы\n"
        "🔥 Костёр — готовка мяса (кнопка в меню волка)\n"
        "🧪 «зелья» — инвентарь и активация зелий\n"
        "⚔️ Арена — бои волков (кнопка в меню волка)\n"
        "«бой» (ответом на сообщение) — вызвать игрока на бой\n\n"
        "💍 БРАК\n"
        "«пожениться» (ответом) — сделать предложение\n"
        "«развод» — расторгнуть брак\n"
        "Совместные действия — через меню брака в профиле\n\n"
        "🎭 РП-КОМАНДЫ\n"
        "Полный список: /rphelp\n\n"
        "🔁 ПЕРЕДАЧА РЕСУРСОВ\n"
        "!передать <кол-во> <ресурс> @username — передать ресурс другому игроку\n"
        "(мясо, дерево, готовое мясо, зелья, руны, ключи, кольца)\n\n"
        "🏪 МАГАЗИН\n"
        "«магазин» — префиксы, скины, зелья, кольца, анти-таргет\n\n"
        "🛠 АДМИНИСТРАЦИЯ (для админов)\n"
        "!выдать <кол-во> <ресурс> @username — выдать ресурс\n"
        "!admin @username — выдать права модератора\n"
        "!рассылка <текст> — разослать сообщение во все чаты, где есть бот"
    )
    await reply_or_send(message, text)


@dp.message(Command("profile"))
@dp.message(F.text.func(lambda t: _normalize_ru(t) in {"профиль", "стая"} if t else False))
async def cmd_profile(message: Message):
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    sent = await reply_or_send(message, profile_text(user, pet), reply_markup=profile_kb())
    remember_owner(sent.chat.id, sent.message_id, u.id)


@dp.message(F.text.lower() == "волк")
async def cmd_wolf(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await send_pet_screen(message.chat.id, u.id)


@dp.message(F.text.lower().startswith("твой волк"))
async def cmd_other_wolf(message: Message):
    """Посмотреть волка другого игрока (ответом на сообщение или @username)."""
    args = message.text.strip().split()[2:]  # всё после "твой волк"
    target_id, _ = _resolve_target(args, message.reply_to_message)
    if target_id is None:
        await reply_or_send(message, 
            "Использование:\n"
            "• ответом на сообщение игрока: «твой волк»\n"
            "• «твой волк @username»"
        )
        return
    target_user = get_or_create_user(target_id, "", "")
    pet = get_pet(target_id)
    if pet is None:
        await reply_or_send(message, f"У игрока {target_user['display_name'] or target_id} ещё нет питомца.")
        return
    if not pet["alive"]:
        await reply_or_send(message, f"🐾 Питомец игрока {target_user['display_name'] or target_id} — «{pet['name']}» — мёртв. 💔")
        return
    satiety_now = apply_satiety_decay(pet)
    header = f"👀 Питомец игрока {target_user['display_name'] or target_id}:\n\n"
    # просмотр только для чтения — без кнопок управления чужим питомцем
    await reply_or_send(message, header + pet_text(pet, satiety_now))


@dp.message(F.text.lower().in_(["квесты", "задания"]))
async def cmd_quests(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await reply_or_send(message, quests_text(u.id))


async def claim_periodic_bonus(user_id: int) -> str:
    """единая система бонуса: и команда 'бонус', и кнопка 🎁 используют её."""
    user = get_or_create_user(user_id, "", "")
    if user["last_bonus"]:
        last = datetime.fromisoformat(user["last_bonus"])
        if datetime.utcnow() - last < BONUS_COOLDOWN:
            remaining = BONUS_COOLDOWN - (datetime.utcnow() - last)
            return f"⏳ Бонус уже забран. Возвращайтесь через {fmt_timedelta(remaining)}"

    xp_gain = 3
    keys_gain = 5
    runes_gain = random.randint(1, 5)
    coins_gain = random.randint(1, 5)  # по запросу: доллары за бонус теперь 1-5
    update_user(
        user_id,
        xp=user["xp"] + xp_gain,
        keys=user["keys"] + keys_gain,
        runes=user["runes"] + runes_gain,
        dollars=user["dollars"] + coins_gain,
        last_bonus=datetime.utcnow().isoformat(),
    )
    record_quest_progress(user_id, "use_bonus", 1)
    text = (
        "🌸 Бонус\n\nНачислено:\n"
        f"✨ +{xp_gain} XP игроку\n"
        f"💵 +{coins_gain}\n"
        f"🔑 +{keys_gain} ключей\n"
        f"🀄 +{runes_gain} рун"
    )
    return text


@dp.message(F.text.lower() == "бонус")
async def cmd_bonus(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await reply_or_send(message, await claim_periodic_bonus(u.id))


# Команда "ферма" удалена: это был неигровой источник долларов, а по
# запросу доллары теперь даются только за бонус, лабиринт и немного за квесты.


# --- админ-команда для быстрой выдачи ресурсов, только !выдать ---
# Доступные ресурсы: xp, руны, скин, ключи, доллары, кольца, дерево, мясо,
# готовое мясо, вес, кости/косточки, опыт брака.
# Опыт игроку этой командой заодно поднимает и уровень питомца (влияет и на игрока, и на волка).
# Префикс и анти-таргет НЕ выдаются этой командой — они покупаются в магазине
# и одобряются админами вручную.

def _normalize_ru(text: str | None) -> str:
    """Единая нормализация текстовых команд: регистр, ё/е, пробелы, знаки препинания.
    Чтобы «Покормить Партнёра!», «покормить партнера», «!выдать 5 готовое мясо» и т.п.
    распознавались независимо от точного написания."""
    if not text:
        return ""
    text = text.strip().lower().replace("ё", "е")
    text = re.sub(r"[.,!?;:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


RESOURCE_RU = {
    "доллар": "dollars", "доллары": "dollars", "долларов": "dollars",
    "ключ": "keys", "ключи": "keys", "ключей": "keys",
    "рун": "runes", "руна": "runes", "руны": "runes",
    "xp": "xp", "опыт": "xp", "опыта": "xp",
    "кольцо": "rings", "кольца": "rings", "колец": "rings",
    "скин": "skin",
    # опыт брака теперь тоже можно выдать: !выдать <кол-во> брак @username
    # (начисляется сразу обоим супругам)
    "брак": "marriage_xp", "брака": "marriage_xp", "браку": "marriage_xp",
    "опытбрака": "marriage_xp", "опыт брака": "marriage_xp",
    # ресурсы питомца (дрова/мясо/готовое мясо/вес/косточки) — тоже можно выдавать напрямую
    "дерево": "firewood", "дрова": "firewood", "дров": "firewood", "древесина": "firewood",
    "мясо": "raw_meat", "сырое мясо": "raw_meat", "сыромясо": "raw_meat", "мяса": "raw_meat",
    "готовое мясо": "cooked_meat", "готовоемясо": "cooked_meat", "приготовленное мясо": "cooked_meat",
    "вес": "weight", "веса": "weight", "кг": "weight",
    "кость": "bones", "кости": "bones", "костей": "bones",
    "косточка": "bones", "косточки": "bones", "косточек": "bones",
    "ларец": "larets", "ларцы": "larets", "ларцов": "larets", "ларца": "larets",
}
RESOURCE_RU_NORM = {_normalize_ru(k): v for k, v in RESOURCE_RU.items()}
# к какой таблице относится ресурс: users / pets / marriage (общий для пары)
RESOURCE_TARGET = {
    "dollars": "user", "keys": "user", "runes": "user", "xp": "user", "rings": "user",
    "firewood": "pet", "raw_meat": "pet", "cooked_meat": "pet", "weight": "pet", "bones": "pet",
    "marriage_xp": "marriage", "larets": "user",
}
RESOURCE_ICONS = {
    "dollars": "💵", "keys": "🔑", "runes": "🀄", "xp": "⭐", "rings": "💍",
    "marriage_xp": "💞", "firewood": "🪵", "raw_meat": "🥩", "cooked_meat": "🍖",
    "weight": "⚖️", "bones": "🦴", "larets": "🎁",
}
RESOURCE_NAMES_RU = {
    "dollars": "долларов", "keys": "ключей", "runes": "рун", "xp": "опыта",
    "rings": "колец", "marriage_xp": "опыта брака",
    "firewood": "дров", "raw_meat": "мяса", "cooked_meat": "готового мяса",
    "weight": "веса (кг)", "bones": "косточек", "larets": "ларцов",
}


def _vydat_usage_text() -> str:
    return (
        "🎁 ВЫДАЧА РЕСУРСОВ\n"
        "————————————\n"
        "Использование:\n"
        "• !выдать <кол-во> <ресурс> @username\n"
        "• ответом на сообщение игрока: !выдать <кол-во> <ресурс>\n"
        "• скин отдельно: !выдать скин <Имя> @username\n\n"
        "Доступные ресурсы:\n"
        "💵 доллар / доллары\n"
        "🔑 ключ / ключи\n"
        "🀄 рун / руны\n"
        "⭐ опыт (игроку и питомцу)\n"
        "💍 кольцо / кольца\n"
        "💞 опыт брака (начисляется обоим супругам)\n"
        "🪵 дерево / дрова\n"
        "🥩 мясо (сырое)\n"
        "🍖 готовое мясо\n"
        "⚖️ вес (кг)\n"
        "🦴 кости / косточки\n"
        "🎭 скин <Имя>\n\n"
        "Префикс и анти-таргет этой командой не выдаются — их покупают в магазине (заявка админу)."
    )


async def admin_grant(admin_message: Message, target_id: int, resource_phrase: str, amount: int) -> bool:
    """Выдача ресурса игроку по-русски; ресурс может быть из нескольких слов
    (например «опыт брака» или «готовое мясо»), а сам ресурс может относиться
    к игроку, к питомцу или начисляться сразу обоим супругам (брак)."""
    resource = RESOURCE_RU_NORM.get(_normalize_ru(resource_phrase))
    if resource is None:
        await reply_or_send(admin_message, f"❌ Неизвестный ресурс «{resource_phrase}».\n\n{_vydat_usage_text()}")
        return False

    target_user = get_or_create_user(target_id, "", "")
    target_kind = RESOURCE_TARGET[resource]
    icon = RESOURCE_ICONS.get(resource, "")
    name_ru = RESOURCE_NAMES_RU.get(resource, resource)

    if target_kind == "user":
        conn = db()
        conn.execute(f"UPDATE users SET {resource} = {resource} + ? WHERE user_id=?", (amount, target_id))
        conn.commit()
        conn.close()
        # выдача опыта заодно поднимает уровень питомца, чтобы волк не "застревал"
        if resource == "xp":
            pet = get_pet(target_id)
            if pet is not None and pet["alive"]:
                grant_pet_xp(pet, amount)
    elif target_kind == "pet":
        pet = get_pet(target_id)
        if pet is None or not pet["alive"]:
            await reply_or_send(admin_message, "❌ У игрока нет живого питомца — нельзя выдать этот ресурс.")
            return False
        new_value = pet[resource] + amount
        if resource == "weight":
            # тот же потолок, что и у обычного кормления — не даём случайно
            # повторить ситуацию с волками на 300-400 кг через выдачу админом
            new_value = min(MAX_PET_WEIGHT, new_value)
        update_pet(target_id, **{resource: new_value})
    elif target_kind == "marriage":
        update_user(target_id, marriage_xp=target_user["marriage_xp"] + amount)
        if target_user["spouse_id"]:
            spouse = get_or_create_user(target_user["spouse_id"], "", "")
            update_user(target_user["spouse_id"], marriage_xp=spouse["marriage_xp"] + amount)

    username_line = f"@{target_user['username']}" if target_user["username"] else "—"
    await reply_or_send(admin_message, 
        "✅ ВЫДАЧА ВЫПОЛНЕНА\n"
        "————————————\n"
        f"👤 Игрок: {username_line}\n"
        f"🆔 ID: {target_id}\n"
        f"📦 Ресурс: {icon} {name_ru}\n"
        f"➕ Количество: {amount}"
    )
    note = f"🎁 Администратор начислил вам: +{amount} {icon} {name_ru}"

    try:
        await bot.send_message(target_id, note)
    except Exception:
        pass
    return True


async def admin_grant_skin(admin_message: Message, target_id: int, skin_name: str) -> bool:
    if not skin_exists(skin_name):
        custom_names = ", ".join(get_all_custom_skin_names()) or "—"
        await reply_or_send(
            admin_message,
            f"Неизвестный скин. Базовые: {', '.join(SKINS)}\nКастомные: {custom_names}\n"
            "Новый кастомный скин можно создать командой «!скин_стикер» (реплаем на стикер).",
        )
        return False
    pet = get_pet(target_id)
    if pet is None or not pet["alive"]:
        await reply_or_send(admin_message, "У игрока нет живого питомца.")
        return False
    update_pet(target_id, skin=skin_name)
    grant_skin_ownership(target_id, skin_name)
    await reply_or_send(admin_message, f"✅ Установлен скин «{skin_name}» игроку {target_id} (в собственность навсегда)")
    try:
        await bot.send_message(target_id, f"🎭 Администратор выдал вам скин «{skin_name}»!")
    except Exception:
        pass
    return True


def _resolve_target(args: list[str], reply_msg: Message | None):
    """Цель — это reply, либо последний аргумент команды (числовой ID или @username).
    Остальные аргументы (количество, ресурс) идут перед целью."""
    if reply_msg is not None:
        return reply_msg.from_user.id, args
    if not args:
        return None, None
    last = args[-1]
    rest = args[:-1]
    if last.startswith("@"):
        row = find_user_by_username(last)
        if row is None:
            return None, None
        return row["user_id"], rest
    if last.isdigit():
        return int(last), rest
    return None, None


@dp.message(F.text.lower().startswith("!скин_стикер"))
async def cmd_skin_from_sticker(message: Message):
    """По запросу: позволяет выдавать в качестве скина питомца ЛЮБОЙ стикер, а не
    только 5 захардкоженных в SKIN_STICKERS. Использование: ответом на сообщение
    со стикером отправить `!скин_стикер <имя скина> @username` (или ID) —
    скин с этим file_id сохранится в базе (custom_skins) под указанным именем
    и сразу же будет выдан игроку в собственность."""
    u = message.from_user
    if not is_admin(u.id):
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await reply_or_send(
            message,
            "Ответьте этой командой на сообщение со стикером.\n"
            "Использование: !скин_стикер <имя скина> @username (реплай на стикер)",
        )
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await reply_or_send(
            message,
            "Использование: !скин_стикер <имя скина> @username (реплай на стикер)",
        )
        return

    *name_parts, target_arg = args
    skin_name = " ".join(name_parts).strip()
    if not skin_name:
        await reply_or_send(message, "Укажите имя скина.")
        return

    if target_arg.startswith("@"):
        target_user = find_user_by_username(target_arg)
        if target_user is None:
            await reply_or_send(message, "Пользователь не найден.")
            return
        target_id = target_user["user_id"]
    elif target_arg.isdigit():
        target_id = int(target_arg)
    else:
        await reply_or_send(message, "Последним аргументом укажите @username или ID игрока.")
        return

    sticker_id = message.reply_to_message.sticker.file_id
    register_custom_skin(skin_name, sticker_id, created_by=u.id)
    await reply_or_send(message, f"🎭 Скин «{skin_name}» сохранён (стикер привязан). Выдаю игроку...")
    await admin_grant_skin(message, target_id, skin_name)


@dp.message(F.text.lower().startswith("!выдать"))
async def cmd_vydat(message: Message):
    # "!выдать <кол-во> <ресурс> @username" или ответом на сообщение игрока;
    # <ресурс> может состоять из нескольких слов, например "опыт брака" или "готовое мясо"
    u = message.from_user
    if not is_admin(u.id):
        return

    args = message.text.split()[1:]
    target_id, rest = _resolve_target(args, message.reply_to_message)
    if target_id is None:
        await reply_or_send(message, "Пользователь не найден.")
        return
    if not rest:
        await reply_or_send(message, _vydat_usage_text())
        return

    if rest[0].lower() == "скин" and len(rest) >= 2:
        await admin_grant_skin(message, target_id, rest[1])
        return

    if len(rest) < 2:
        await reply_or_send(message, _vydat_usage_text())
        return

    amount_str, *resource_words = rest
    resource_phrase = " ".join(resource_words)
    try:
        amount = int(amount_str)
    except ValueError:
        await reply_or_send(message, "❌ Количество должно быть числом.")
        return

    await admin_grant(message, target_id, resource_phrase, amount)


# --- : !admin ID/user — выдать пользователю права админа-модератора,
# чтобы ему тоже приходили заявки на ручную выдачу префикса/анти-таргета ---
# =========================================================
#  !ПЕРЕДАТЬ — передача ресурсов между игроками
# =========================================================
TRANSFER_RESOURCE_RU = {
    "мясо": "raw_meat", "сырое мясо": "raw_meat", "мяса": "raw_meat", "сыромясо": "raw_meat",
    "дерево": "firewood", "дрова": "firewood", "дров": "firewood", "древесина": "firewood",
    "готовое мясо": "cooked_meat", "готовоемясо": "cooked_meat", "приготовленное мясо": "cooked_meat",
    "руна": "runes", "руны": "runes", "рун": "runes",
    "ключ": "keys", "ключи": "keys", "ключей": "keys",
    "кольцо": "rings", "кольца": "rings", "колец": "rings",
    "доллар": "dollars", "доллары": "dollars", "долларов": "dollars", "доллара": "dollars",
}
TRANSFER_RESOURCE_NORM = {_normalize_ru(k): v for k, v in TRANSFER_RESOURCE_RU.items()}
TRANSFER_TARGET = {
    "raw_meat": "pet", "firewood": "pet", "cooked_meat": "pet",
    "runes": "user", "keys": "user", "rings": "user", "dollars": "user",
}
POTION_RU_TO_TYPE = {
    "сила": "strength", "силы": "strength", "силу": "strength",
    "скорость": "speed", "скорости": "speed",
    "добыча": "loot", "добычи": "loot", "добычу": "loot",
    "жарка": "cooking", "жарки": "cooking", "жарку": "cooking",
}


def _transfer_usage_text() -> str:
    return (
        "🔁 ПЕРЕДАЧА РЕСУРСОВ\n"
        "————————————\n"
        "Использование:\n"
        "• !передать <кол-во> <ресурс> @username\n"
        "• ответом на сообщение игрока: !передать <кол-во> <ресурс>\n\n"
        "Доступные ресурсы:\n"
        "🥩 мясо (сырое)\n"
        "🪵 дерево / дрова\n"
        "🍖 готовое мясо\n"
        "🀄 рун / руны\n"
        "🔑 ключ / ключи\n"
        "💍 кольцо / кольца\n"
        "🧪 зелье <сила/скорость/добыча/жарка>\n\n"
        "Пример: !передать 3 мясо @friend\n"
        "Пример: !передать 1 зелье силы @friend"
    )


def _tag(name: str, username: str | None) -> str:
    """Имя с тегом для красивого отображения: 'Иван (@ivan)' или просто 'Иван'."""
    name = name or "Игрок"
    if username:
        return f"{name} (@{username})"
    return name


@dp.message(F.text.lower().startswith("!передать"))
async def cmd_transfer(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)

    args = message.text.split()[1:]
    target_id, rest = _resolve_target(args, message.reply_to_message)
    if target_id is None:
        await reply_or_send(message, _transfer_usage_text())
        return
    if target_id == u.id:
        await reply_or_send(message, "❌ Нельзя передать ресурсы самому себе.")
        return
    if not rest or len(rest) < 2:
        await reply_or_send(message, _transfer_usage_text())
        return

    amount_str, *resource_words = rest
    try:
        amount = int(amount_str)
    except ValueError:
        await reply_or_send(message, "❌ Количество должно быть целым числом.")
        return
    if amount <= 0:
        await reply_or_send(message, "❌ Количество должно быть больше нуля.")
        return

    resource_phrase = " ".join(resource_words)
    sender_tag = _tag(u.full_name, u.username)

    # --- зелья: "зелье <тип>" ---
    if resource_words and _normalize_ru(resource_words[0]) in ("зелье", "зелья", "зелий"):
        if len(resource_words) < 2:
            await reply_or_send(message, _transfer_usage_text())
            return
        ptype = POTION_RU_TO_TYPE.get(_normalize_ru(resource_words[1]))
        if ptype is None:
            await reply_or_send(message, f"❌ Неизвестное зелье «{resource_words[1]}».\n\n{_transfer_usage_text()}")
            return
        if get_potion_count(u.id, ptype) < amount:
            await reply_or_send(message, "❌ У вас недостаточно этого зелья.")
            return
        add_potion(u.id, ptype, -amount)
        add_potion(target_id, ptype, amount)
        info = POTION_DEFS[ptype]
        target_user = get_or_create_user(target_id, "", "")
        target_tag = _tag(target_user["display_name"], target_user["username"])
        await reply_or_send(message, f"✅ {sender_tag} передал(а) {amount} × {info['icon']} {info['title']} игроку {target_tag}.")
        try:
            await bot.send_message(target_id, f"🎁 {sender_tag} передал(а) вам {amount} × {info['icon']} {info['title']}!")
        except Exception:
            pass
        return

    # --- обычные ресурсы ---
    resource = TRANSFER_RESOURCE_NORM.get(_normalize_ru(resource_phrase))
    if resource is None:
        await reply_or_send(message, f"❌ Неизвестный ресурс «{resource_phrase}».\n\n{_transfer_usage_text()}")
        return

    icon = RESOURCE_ICONS.get(resource, "")
    name_ru = RESOURCE_NAMES_RU.get(resource, resource)
    target_kind = TRANSFER_TARGET[resource]

    if target_kind == "pet":
        sender_pet = get_pet(u.id)
        if sender_pet is None or not sender_pet["alive"]:
            await reply_or_send(message, "❌ У вас нет живого питомца.")
            return
        if sender_pet[resource] < amount:
            await reply_or_send(message, f"❌ У вас недостаточно ресурса «{name_ru}».")
            return
        target_pet = get_pet(target_id)
        if target_pet is None or not target_pet["alive"]:
            await reply_or_send(message, "❌ У получателя нет живого питомца — нельзя передать этот ресурс.")
            return
        update_pet(u.id, **{resource: sender_pet[resource] - amount})
        update_pet(target_id, **{resource: target_pet[resource] + amount})
    else:
        sender_user = get_or_create_user(u.id, u.username or "", u.full_name)
        if sender_user[resource] < amount:
            await reply_or_send(message, f"❌ У вас недостаточно ресурса «{name_ru}».")
            return
        target_user = get_or_create_user(target_id, "", "")
        update_user(u.id, **{resource: sender_user[resource] - amount})
        update_user(target_id, **{resource: target_user[resource] + amount})

    target_user = get_or_create_user(target_id, "", "")
    target_tag = _tag(target_user["display_name"], target_user["username"])
    await reply_or_send(message, f"✅ {sender_tag} передал(а) {amount} {icon} {name_ru} игроку {target_tag}.")
    try:
        await bot.send_message(target_id, f"🎁 {sender_tag} передал(а) вам {amount} {icon} {name_ru}!")
    except Exception:
        pass


@dp.message(F.text.lower().startswith("!рассылка"))
async def cmd_broadcast(message: Message):
    # рассылка сообщения во все чаты, где бот видел хоть одно сообщение
    u = message.from_user
    if not is_admin(u.id):
        return

    text = message.text[len("!рассылка"):].strip()
    if not text:
        await reply_or_send(message, "Использование: !рассылка <текст сообщения>")
        return

    chat_ids = get_all_known_chat_ids()
    if not chat_ids:
        await reply_or_send(message, "Бот пока не знает ни одного чата для рассылки.")
        return

    status = await reply_or_send(message, f"📣 Рассылаю в {len(chat_ids)} чат(ов)...")
    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, f"📣 ОБЪЯВЛЕНИЕ\n————————————\n{text}")
            sent += 1
        except Exception as e:
            failed += 1
            log.warning("Рассылка: не удалось отправить в чат %s: %s", chat_id, e)
        await asyncio.sleep(0.05)  # не превышаем лимиты Telegram на отправку сообщений
    await safe_edit_or_send(status, f"📣 Рассылка завершена: успешно {sent}, не удалось {failed}.")


@dp.message(F.text.lower().startswith("!admin"))
async def cmd_grant_admin(message: Message):
    u = message.from_user
    # выдавать админку могут только "коренные" админы из ADMIN_IDS (окружение),
    # чтобы права не расползались бесконтрольно через уже выданных админов
    if u.id not in ADMIN_IDS:
        return

    args = message.text.split()[1:]
    target_id, _ = _resolve_target(args, message.reply_to_message)
    if target_id is None:
        await reply_or_send(message, "Использование: !admin ID/username (или ответом на сообщение игрока)")
        return

    get_or_create_user(target_id, "", "")
    conn = db()
    conn.execute(
        "INSERT INTO admins (user_id, added_at, added_by) VALUES (?,?,?) "
        "ON CONFLICT (user_id) DO UPDATE SET added_at=excluded.added_at, added_by=excluded.added_by",
        (target_id, datetime.utcnow().isoformat(), u.id),
    )
    conn.commit()
    conn.close()
    await reply_or_send(message, f"✅ Пользователю {target_id} выданы права администратора. Теперь ему будут приходить заявки на ручную выдачу (префикс/анти-таргет).")
    try:
        await bot.send_message(target_id, "🛡 Вам выданы права администратора бота. Теперь вам будут приходить заявки на ручную выдачу товаров.")
    except Exception:
        pass


# =========================================================
#  БРАК и БОЙ — текстовыми командами и кнопками
# =========================================================

MARRIAGE_PROPOSE_WORDS = {"жениться", "выйти замуж", "пожениться", "женюсь", "сделать предложение"}
MARRIAGE_DIVORCE_WORDS = {"развод", "разводимся", "развестись"}
MARRIAGE_ACCEPT_WORDS = {"принять", "принять предложение", "согласна", "согласен"}
MARRIAGE_DECLINE_WORDS = {"отклонить", "отклонить предложение", "отказать", "не согласна", "не согласен"}
FIGHT_WORDS = {"бой", "драка", "сразиться"}

# =========================================================
#  СОВМЕСТНЫЕ ДЕЙСТВИЯ В БРАКЕ
# =========================================================


def _days_hours_to_sec(days: float = 0, hours: float = 0) -> int:
    return int(days * 86400 + hours * 3600)


# (id, название, XP, кулдаун в секундах, требуемый уровень брака для разблокировки)
MARRIAGE_ACTIONS = [
    ("compliment", "Сделать комплимент", 5, _days_hours_to_sec(hours=3.28), 1),
    ("hug", "Крепко обнять", 7, _days_hours_to_sec(hours=3.28), 1),
    ("flower", "Подарить цветок", 10, _days_hours_to_sec(hours=3.28), 1),
    ("dinner", "Приготовить ужин", 15, _days_hours_to_sec(hours=3.28), 1),
    ("movie", "Посмотреть фильм", 18, _days_hours_to_sec(hours=3.28), 1),
    ("walk_stars", "Прогуляться под звёздами", 20, _days_hours_to_sec(hours=3.28), 1),
    ("massage", "Сделать массаж", 25, _days_hours_to_sec(hours=3.28), 1),
    ("gift", "Сделать подарок", 30, _days_hours_to_sec(hours=3.28), 1),
    ("memories", "Вспомнить прошлое", 35, _days_hours_to_sec(hours=3.28), 1),
    ("trip", "Устроить поездку", 40, _days_hours_to_sec(hours=3.28), 1),
    ("poem", "Написать стих", 45, _days_hours_to_sec(hours=3.28), 1),
    ("tradition", "Создать традицию", 50, _days_hours_to_sec(hours=3.28), 1),
    ("plan_future", "Спланировать будущее", 60, _days_hours_to_sec(hours=3.28), 1),
    ("vow", "Написать клятву", 75, _days_hours_to_sec(hours=3.28), 1),
    ("talisman", "Создать талисман", 100, _days_hours_to_sec(hours=3.28), 1),
    ("stargaze", "Наблюдать за звездами", 120, _days_hours_to_sec(hours=3.28), 1),
    ("dance", "Танцевать вдвоем", 150, _days_hours_to_sec(hours=3.28), 1),
    ("ruins", "Исследовать руины", 180, _days_hours_to_sec(hours=3.28), 1),
    ("secret", "Поделиться секретом", 220, _days_hours_to_sec(hours=3.28), 1),
    ("time_capsule", "Создать капсулу времени", 250, _days_hours_to_sec(hours=3.28), 1),
    ("shelter", "Построить укрытие", 300, _days_hours_to_sec(hours=3.28), 1),
    ("name_star", "Назвать звезду", 350, _days_hours_to_sec(hours=3.28), 1),
    ("melody", "Сочинить мелодию", 400, _days_hours_to_sec(days=6, hours=12), 2),
    ("plant_tree", "Посадить древо", 450, _days_hours_to_sec(days=7), 3),
    ("eternal_promise", "Дать вечное обещание", 500, _days_hours_to_sec(days=8), 4),
    ("world_map", "Создать карту мира", 600, _days_hours_to_sec(days=9), 5),
    ("forge_rings", "Выковать кольца", 700, _days_hours_to_sec(days=10), 6),
    ("riddle_ages", "Разгадать загадку веков", 800, _days_hours_to_sec(days=11), 7),
    ("tame_beast", "Приручить зверя", 900, _days_hours_to_sec(days=12), 8),
    ("find_artifact", "Найти артефакт", 1000, _days_hours_to_sec(days=14), 9),
    ("draw_constellation", "Нарисовать созвездие", 1250, _days_hours_to_sec(days=16), 10),
    ("calm_storm", "Усмирить бурю", 1500, _days_hours_to_sec(days=18), 11),
    ("rewrite_fate", "Переписать судьбу", 1750, _days_hours_to_sec(days=20), 12),
    ("create_legend", "Создать легенду", 2000, _days_hours_to_sec(days=25), 13),
    ("reach_harmony", "Достичь гармонии", 2500, _days_hours_to_sec(days=30), 14),
]
MARRIAGE_ACTIONS_BY_TITLE = {_normalize_ru(a[1]): a for a in MARRIAGE_ACTIONS}


def marriage_level_from_xp(xp: int) -> int:
    return 1 + xp // MARRIAGE_XP_PER_LEVEL


def _fmt_days_hours(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д" + (f" {hours}ч" if hours else "")
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


def get_marriage_action_cooldown(user_id: int, action_id: str) -> datetime | None:
    conn = db()
    row = conn.execute(
        "SELECT available_at FROM marriage_action_cooldowns WHERE user_id=? AND action_id=?",
        (user_id, action_id),
    ).fetchone()
    conn.close()
    return datetime.fromisoformat(row["available_at"]) if row else None


def set_marriage_action_cooldown(user_id: int, action_id: str, available_at: datetime):
    conn = db()
    conn.execute(
        "INSERT INTO marriage_action_cooldowns (user_id, action_id, available_at) VALUES (?,?,?) "
        "ON CONFLICT (user_id, action_id) DO UPDATE SET available_at=excluded.available_at",
        (user_id, action_id, available_at.isoformat()),
    )
    conn.commit()
    conn.close()


def marriage_info_text(user_id: int) -> str:
    # «брак» — информационная карточка о браке: партнёр, уровень пары, опыт.
    # Полный список доступных действий — кнопка ниже или команда «брак действия».
    user = get_or_create_user(user_id, "", "")
    level = marriage_level_from_xp(user["marriage_xp"])
    xp_in_level = user["marriage_xp"] - (level - 1) * MARRIAGE_XP_PER_LEVEL
    spouse_line = "нет"
    if user["spouse_id"]:
        spouse = get_or_create_user(user["spouse_id"], "", "")
        spouse_line = spouse["display_name"]

    lines = [
        "💍 БРАК",
        f"👤 Партнёр: {spouse_line}",
        f"⭐ Уровень пары: {level}",
        f"✨ Опыт: {user['marriage_xp']} XP ({xp_in_level}/{MARRIAGE_XP_PER_LEVEL} до след. уровня)",
        "",
        "🌲🏹 «Лес»/«Охота» в меню волка — можно пойти вместе с партнёром.",
    ]
    return "\n".join(lines)


def marriage_info_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [[copy_text_button("🍖 Покормить партнёра", "покормить партнёра")]]
    rows.append([InlineKeyboardButton(text="💞 Действия в браке", callback_data="marriage_actions")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def marriage_actions_text(user_id: int) -> str:
    # показываем только те действия, которые реально разрешены прямо
    # сейчас (уровень открыт и нет кулдауна). Всё остальное просто скрыто.
    user = get_or_create_user(user_id, "", "")
    level = marriage_level_from_xp(user["marriage_xp"])
    now = datetime.utcnow()
    growth_mult = 1 + (level - 1) * MARRIAGE_ACTION_LEVEL_GROWTH_PERCENT / 100

    available = []
    for action_id, title, xp, cooldown, unlock_level in MARRIAGE_ACTIONS:
        if level < unlock_level:
            continue
        avail_at = get_marriage_action_cooldown(user_id, action_id)
        if avail_at and now < avail_at:
            continue
        xp_shown = round(xp * growth_mult)
        available.append(f"✅ «{title}» +{xp_shown}")

    lines = [
        "💍 ДЕЙСТВИЯ В БРАКЕ",
        "Нажмите на кнопку, чтобы скопировать команду, и отправьте её сообщением "
        "(отвечать на сообщение партнёра не обязательно):",
        "",
    ]
    if available:
        lines.extend(available)
    else:
        lines.append("Пока нет доступных действий — загляните чуть позже.")
    if level > 1:
        lines.append("")
        lines.append(f"💞 Бонус за уровень пары: +{MARRIAGE_ACTION_LEVEL_GROWTH_PERCENT * (level - 1)}% к опыту за все действия.")
    return "\n".join(lines)


def marriage_actions_kb(user_id: int) -> InlineKeyboardMarkup:
    # кнопки копируют текст команды по нажатию; показаны только
    # действия, доступные прямо сейчас (открыт уровень и нет кулдауна)
    user = get_or_create_user(user_id, "", "")
    level = marriage_level_from_xp(user["marriage_xp"])
    now = datetime.utcnow()

    rows = [[copy_text_button("🍖 Покормить партнёра", "покормить партнёра")]]
    for action_id, title, xp, cooldown, unlock_level in MARRIAGE_ACTIONS:
        if level < unlock_level:
            continue
        avail_at = get_marriage_action_cooldown(user_id, action_id)
        if avail_at and now < avail_at:
            continue
        rows.append([copy_text_button(title, title)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("marriage_hint_"))
async def cb_marriage_hint(callback: CallbackQuery):
    # запасной вариант на случай, если aiogram/клиент не поддерживает copy_text
    await callback.answer("Отправьте этот текст сообщением в чат 👆", show_alert=True)


@dp.message(F.text.func(lambda t: _normalize_ru(t) == "покормить партнера"))
async def cmd_feed_partner(message: Message):
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        await reply_or_send(message, "Вы не состоите в браке.")
        return
    pet = get_pet(u.id)
    if pet is None or not pet["alive"] or pet["cooked_meat"] < FEED_COOKED_MEAT_COST:
        await reply_or_send(message, f"Нужно {FEED_COOKED_MEAT_COST} 🍖 готового мяса у вашего волка, чтобы покормить партнёра.")
        return
    spouse_pet = get_pet(user["spouse_id"])
    if spouse_pet is None or not spouse_pet["alive"]:
        await reply_or_send(message, "У вашей пары нет живого питомца.")
        return
    spouse_satiety = apply_satiety_decay(spouse_pet)
    if spouse_satiety >= PARTNER_FEED_MAX_SATIETY:
        await reply_or_send(message, f"Питомец вашей пары не голоден (сытость {spouse_satiety}%) — кормить партнёра можно при сытости меньше {PARTNER_FEED_MAX_SATIETY}%.")
        return
    update_pet(u.id, cooked_meat=pet["cooked_meat"] - FEED_COOKED_MEAT_COST)
    satiety_now = min(100, spouse_satiety + FEED_SATIETY_GAIN)
    update_pet(user["spouse_id"], satiety=satiety_now, last_fed=datetime.utcnow().isoformat(), starved_since=None)
    record_quest_progress(u.id, "feed_pet", 1)
    await reply_or_send(message, "🍖 Вы покормили питомца вашей пары! Сытость восстановлена до 100%")
    try:
        await bot.send_message(user["spouse_id"], f"💞 «{user['display_name']}» покормил(а) вашего питомца! Сытость восстановлена до 100%")
    except Exception:
        pass


@dp.message(F.text.func(lambda t: _normalize_ru(t) in MARRIAGE_ACTIONS_BY_TITLE if t else False))
async def cmd_marriage_action(message: Message):
    # совместное действие в браке: отвечать на сообщение партнёра
    # больше не обязательно, достаточно состоять в браке
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        return  # не в браке — не мешаем другим хендлерам текста

    action_id, title, xp, cooldown, unlock_level = MARRIAGE_ACTIONS_BY_TITLE[_normalize_ru(message.text)]
    level = marriage_level_from_xp(user["marriage_xp"])
    if level < unlock_level:
        await reply_or_send(message, f"🔒 Действие «{title}» ещё не открыто — нужен {unlock_level} уровень брака (сейчас {level}).")
        return
    now = datetime.utcnow()
    avail_at = get_marriage_action_cooldown(u.id, action_id)
    if avail_at and now < avail_at:
        await reply_or_send(message, f"⏳ «{title}» уже недавно выполнялось. Доступно через {fmt_timedelta(avail_at - now)}.")
        return

    spouse_id = user["spouse_id"]
    spouse = get_or_create_user(spouse_id, "", "")
    # награда за совместные действия растёт вместе с уровнем пары
    growth_mult = 1 + (level - 1) * MARRIAGE_ACTION_LEVEL_GROWTH_PERCENT / 100
    xp_actual = round(xp * growth_mult)
    new_xp_user = user["marriage_xp"] + xp_actual
    new_xp_spouse = spouse["marriage_xp"] + xp_actual
    update_user(u.id, marriage_xp=new_xp_user)
    update_user(spouse_id, marriage_xp=new_xp_spouse)
    set_marriage_action_cooldown(u.id, action_id, now + timedelta(seconds=cooldown))

    new_level = marriage_level_from_xp(new_xp_user)
    bonus_note = f" (+{MARRIAGE_ACTION_LEVEL_GROWTH_PERCENT * (level - 1)}% за уровень пары)" if level > 1 else ""
    await reply_or_send(message, f"💞 «{title}»! +{xp_actual} опыта брака{bonus_note} (теперь {new_xp_user} XP, уровень {new_level}).")
    try:
        await bot.send_message(
            spouse_id,
            f"💞 «{user['display_name']}» выполнил(а) с вами действие «{title}»! +{xp_actual} опыта брака.",
        )
    except Exception:
        pass


@dp.message(F.text.func(lambda t: _normalize_ru(t) in {"брак", "меню брака", "мой брак", "статус брака"} if t else False))
async def cmd_marriage_menu_or_propose(message: Message):
    # «брак» — карточка с информацией о браке и кнопками действий
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["spouse_id"]:
        sent = await reply_or_send(message, marriage_info_text(u.id), reply_markup=marriage_info_kb(u.id))
        remember_owner(sent.chat.id, sent.message_id, u.id)
        return
    await cmd_marry(message)


@dp.message(F.text.func(lambda t: _normalize_ru(t) in {"брак действия", "действия брака", "действия в браке"} if t else False))
async def cmd_marriage_actions(message: Message):
    # отдельная команда: показывает только реально доступные сейчас
    # совместные действия (кнопками, которые копируют текст команды)
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        await reply_or_send(message, "Вы не состоите в браке.")
        return
    sent = await reply_or_send(message, marriage_actions_text(u.id), reply_markup=marriage_actions_kb(u.id))
    remember_owner(sent.chat.id, sent.message_id, u.id)


@dp.callback_query(F.data == "marriage_actions")
async def cb_marriage_actions(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        await callback.answer("Вы не состоите в браке.", show_alert=True)
        return
    await safe_edit_or_send(callback.message, marriage_actions_text(u.id), marriage_actions_kb(u.id), owner_id=u.id)
    await callback.answer()


def marriage_proposal_kb(proposer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💍 Принять", callback_data=f"marry_yes_{proposer_id}"),
        InlineKeyboardButton(text="💔 Отклонить", callback_data=f"marry_no_{proposer_id}"),
    ]])


@dp.message(F.text.func(lambda t: _normalize_ru(t) in MARRIAGE_PROPOSE_WORDS))
async def cmd_marry(message: Message):
    # предложение делается простым словом ответом на сообщение игрока
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["spouse_id"]:
        await reply_or_send(message, "Вы уже состоите в браке 💍 Сначала напишите «развод».")
        return
    if not message.reply_to_message:
        await reply_or_send(message, "Ответьте этим словом на сообщение того, кому хотите сделать предложение 💍")
        return
    target_user = message.reply_to_message.from_user
    if target_user.id == u.id:
        await reply_or_send(message, "Нельзя жениться на себе 😅")
        return
    target = get_or_create_user(target_user.id, target_user.username or "", target_user.full_name)
    if target["spouse_id"]:
        await reply_or_send(message, "Этот игрок уже состоит в браке 💔")
        return
    if user["rings"] < 1:
        await reply_or_send(message, "Нужно хотя бы 1 💍 кольцо, чтобы сделать предложение. Купите его в магазине.")
        return

    update_user(u.id, rings=user["rings"] - 1)
    try:
        await bot.send_message(
            target_user.id,
            f"💍 Игрок «{user['display_name']}» сделал вам предложение руки и сердца!\n"
            "Примите нажатием кнопки, либо напишите в чат «принять» или «отклонить».",
            reply_markup=marriage_proposal_kb(u.id),
        )
        update_user(target_user.id, pending_marriage_from=u.id)
        await reply_or_send(message, "💍 Предложение отправлено! Ожидайте ответа.")
    except Exception:
        update_user(u.id, rings=user["rings"])  # возвращаем кольцо, если не удалось написать
        await reply_or_send(message, "Не удалось отправить предложение этому игроку (он не писал боту).")


async def _finalize_marriage(proposer_id: int, target_id: int) -> str | None:
    proposer = get_or_create_user(proposer_id, "", "")
    target = get_or_create_user(target_id, "", "")
    if proposer["spouse_id"] or target["spouse_id"]:
        return None
    update_user(proposer_id, spouse_id=target_id, pending_marriage_from=None)
    update_user(target_id, spouse_id=proposer_id, pending_marriage_from=None)
    try:
        await bot.send_message(proposer_id, f"💍 «{target['display_name']}» приняла(-л) ваше предложение! Теперь вы в браке 🎉")
    except Exception:
        pass
    return f"💍 Брак заключён между вами и «{proposer['display_name']}»! Поздравляем!"


@dp.callback_query(F.data.startswith("marry_yes_"))
async def cb_marry_yes(callback: CallbackQuery):
    proposer_id = int(callback.data.split("_")[-1])
    u = callback.from_user
    result = await _finalize_marriage(proposer_id, u.id)
    if result is None:
        await callback.answer("Один из вас уже в браке 💔", show_alert=True)
        return
    await callback.message.edit_text(result)
    await callback.answer()


@dp.callback_query(F.data.startswith("marry_no_"))
async def cb_marry_no(callback: CallbackQuery):
    proposer_id = int(callback.data.split("_")[-1])
    update_user(callback.from_user.id, pending_marriage_from=None)
    await callback.message.edit_text("💔 Предложение отклонено.")
    try:
        await bot.send_message(proposer_id, "💔 Ваше предложение руки и сердца было отклонено.")
    except Exception:
        pass
    await callback.answer()


@dp.message(F.text.func(lambda t: _normalize_ru(t) in MARRIAGE_ACCEPT_WORDS))
async def cmd_marry_accept_chat(message: Message):
    # принять предложение брака прямо в чате, без нажатия кнопки
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["pending_marriage_from"]:
        return
    proposer_id = user["pending_marriage_from"]
    result = await _finalize_marriage(proposer_id, u.id)
    if result is None:
        update_user(u.id, pending_marriage_from=None)
        await reply_or_send(message, "Один из вас уже в браке 💔")
        return
    await reply_or_send(message, result)


@dp.message(F.text.func(lambda t: _normalize_ru(t) in MARRIAGE_DECLINE_WORDS))
async def cmd_marry_decline_chat(message: Message):
    # отклонить предложение брака прямо в чате
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["pending_marriage_from"]:
        return
    proposer_id = user["pending_marriage_from"]
    update_user(u.id, pending_marriage_from=None)
    await reply_or_send(message, "💔 Предложение отклонено.")
    try:
        await bot.send_message(proposer_id, "💔 Ваше предложение руки и сердца было отклонено.")
    except Exception:
        pass


@dp.message(F.text.func(lambda t: _normalize_ru(t) in MARRIAGE_DIVORCE_WORDS))
async def cmd_divorce(message: Message):
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        await reply_or_send(message, "Вы не состоите в браке.")
        return
    spouse_id = user["spouse_id"]
    update_user(u.id, spouse_id=None)
    update_user(spouse_id, spouse_id=None)
    await reply_or_send(message, "💔 Вы развелись.")
    try:
        await bot.send_message(spouse_id, f"💔 «{user['display_name']}» инициировал(а) развод. Вы больше не в браке.")
    except Exception:
        pass


@dp.message(F.text.func(lambda t: _normalize_ru(t) in FIGHT_WORDS))
async def cmd_fight_cmd(message: Message):
    # /14 — команда "бой" ответом на сообщение игрока — волки сражаются 30 минут
    u = message.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await reply_or_send(message, "Сначала приручите волка (команда «волк»).")
        return
    if not message.reply_to_message:
        await reply_or_send(message, "Ответьте словом «бой» на сообщение того, с кем хотите сразиться ⚔️")
        return
    target_id = message.reply_to_message.from_user.id
    if target_id == u.id:
        await reply_or_send(message, "Нельзя биться со своим же волком 😅")
        return
    if pet_is_busy(pet):
        await reply_or_send(message, "Ваш питомец сейчас занят.")
        return
    # зелье силы активируется прямо в момент боя
    if get_potion_count(u.id, "strength") > 0:
        sent = await reply_or_send(message, 
            f"⚔️ Как сразиться с {message.reply_to_message.from_user.full_name}?",
            reply_markup=fight_confirm_kb(target_id),
        )
        remember_owner(sent.chat.id, sent.message_id, u.id)
        return
    result = await start_wolf_fight(u.id, target_id)
    if result is None:
        await reply_or_send(message, "У соперника нет живого питомца.")
        return
    await reply_or_send(message, result)


def fight_confirm_kb(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Обычный бой", callback_data=f"dofight_{target_id}")],
        [InlineKeyboardButton(
            text=f"💪 С зельем силы (+{POTION_STRENGTH_BONUS_PERCENT}%)",
            callback_data=f"dofightpotion_{target_id}",
        )],
    ])


@dp.callback_query(F.data.startswith("dofight_"))
async def cb_dofight(callback: CallbackQuery):
    u = callback.from_user
    target_id = int(callback.data[len("dofight_"):])
    result = await start_wolf_fight(u.id, target_id)
    if result is None:
        await callback.answer("Соперник недоступен", show_alert=True)
        return
    await callback.answer(result, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


@dp.callback_query(F.data.startswith("dofightpotion_"))
async def cb_dofight_potion(callback: CallbackQuery):
    u = callback.from_user
    target_id = int(callback.data[len("dofightpotion_"):])
    if get_potion_count(u.id, "strength") <= 0:
        await callback.answer("Зелье силы закончилось", show_alert=True)
        return
    add_potion(u.id, "strength", -1)
    update_pet(u.id, pending_potion_strength=1)
    result = await start_wolf_fight(u.id, target_id)
    if result is None:
        add_potion(u.id, "strength", 1)
        update_pet(u.id, pending_potion_strength=0)
        await callback.answer("Соперник недоступен", show_alert=True)
        return
    await callback.answer(f"🧪💪 Зелье силы активировано! {result}", show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


# =========================================================
#  РП-КОМАНДЫ (пнуть / обнять и т.п.)
# =========================================================
# ключ: (глагол в форме действия, эмодзи) — используются ответом на сообщение игрока
RP_ACTIONS = {
    "обнять": ("нежно обнял(а)", "🤗"),
    "поцеловать": ("поцеловал(а)", "😘"),
    "пнуть": ("пнул(а)", "🦵"),
    "ударить": ("ударил(а)", "👊"),
    "погладить": ("погладил(а) по голове", "🥰"),
    "укусить": ("укусил(а)", "😬"),
    "толкнуть": ("толкнул(а)", "🤜"),
    "напугать": ("напугал(а)", "😱"),
    "поддержать": ("поддержал(а) добрым словом", "🤝"),
    "подмигнуть": ("подмигнул(а)", "😉"),
    "потанцевать": ("пригласил(а) на танец", "💃"),
    "пожать руку": ("пожал(а) руку", "🤝"),
    "щелкнуть": ("щёлкнул(а) по носу", "👆"),
    "ущипнуть": ("ущипнул(а)", "🤏"),
    "поцарапать": ("поцарапал(а)", "🐾"),
    "облизать": ("облизал(а)", "😝"),
    "кинуть тортом": ("кинул(а) тортом в", "🎂"),
    "похвалить": ("похвалил(а)", "👏"),
    "утешить": ("утешил(а)", "🫂"),
    "разбудить": ("разбудил(а)", "⏰"),
}
RP_ACTIONS_NORM = {_normalize_ru(k): (k, v, e) for k, (v, e) in RP_ACTIONS.items()}


def _match_rp_command(text: str):
    """Возвращает (ключ_команды, @username_или_None), если текст — это РП-команда.
    Работает и ответом на сообщение ("пнуть"), и тегом в самом сообщении
    ("пнуть @username")."""
    if not text:
        return None
    norm_full = _normalize_ru(text.strip())
    if norm_full in RP_ACTIONS_NORM:
        return RP_ACTIONS_NORM[norm_full][0], None
    parts = text.strip().split()
    if len(parts) >= 2 and parts[-1].startswith("@"):
        norm_action = _normalize_ru(" ".join(parts[:-1]))
        if norm_action in RP_ACTIONS_NORM:
            return RP_ACTIONS_NORM[norm_action][0], parts[-1]
    return None


@dp.message(F.text.func(lambda t: _match_rp_command(t) is not None))
async def cmd_rp_action(message: Message):
    # по запросу: РП-команды работают и ответом на сообщение, и тегом:
    # "пнуть @username" — и оформлены так же, как "!передать": имя с тегом у обоих
    u = message.from_user
    key, username = _match_rp_command(message.text)
    verb, emoji = RP_ACTIONS[key]

    if username:
        row = find_user_by_username(username)
        if row is None:
            await reply_or_send(message, f"Не нашёл игрока {username} — возможно, он ещё не писал этому боту.")
            return
        target_id = row["user_id"]
        target_tag = _tag(row["display_name"], row["username"])
    elif message.reply_to_message:
        target = message.reply_to_message.from_user
        target_id = target.id
        target_tag = _tag(target.full_name, target.username)
    else:
        await reply_or_send(message, 
            f"Ответьте словом «{key}» на сообщение игрока, либо напишите «{key} @username» {emoji}\n"
            f"Список всех РП-команд: /rphelp"
        )
        return

    if target_id == u.id:
        await reply_or_send(message, f"{emoji} Нельзя сделать это самому себе!")
        return
    actor_tag = _tag(u.full_name, u.username)
    await reply_or_send(message, f"{emoji} {actor_tag} {verb} {target_tag}!")


@dp.message(Command("rphelp"))
async def cmd_rphelp(message: Message):
    lines = ["🎭 РП-КОМАНДЫ", "————————————",
             "Ответьте нужным словом на сообщение игрока, либо напишите «команда @username»:", ""]
    for key in RP_ACTIONS:
        emoji = RP_ACTIONS[key][1]
        lines.append(f"{emoji} {key}")
    lines.append("")
    lines.append("Примеры: «обнять» ответом на сообщение друга, или «обнять @friend».")
    await reply_or_send(message, "\n".join(lines))


# =========================================================
#  CALLBACK: ПРОФИЛЬ / ПИТОМЕЦ
# =========================================================

@dp.callback_query(F.data == "pet_menu")
async def cb_pet_menu(callback: CallbackQuery):
    # тройная защита на самом частом экране: явная проверка прямо в хендлере,
    # в дополнение к middleware и router-filter выше
    if not await _owner_allowed(callback):
        return
    await send_pet_screen(callback.message.chat.id, callback.from_user.id, edit_message=callback.message)
    await callback.answer()


@dp.callback_query(F.data == "back_profile")
async def cb_back_profile(callback: CallbackQuery):
    if not await _owner_allowed(callback):
        return
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    await safe_edit_or_send(callback.message, profile_text(user, pet), profile_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "quests")
async def cb_quests(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, quests_text(callback.from_user.id), quests_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "revive")
async def cb_revive(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or pet["alive"]:
        await callback.answer("Питомец уже жив", show_alert=True)
        return
    if user["runes"] < REVIVE_COST_RUNES:
        await callback.answer(f"Недостаточно рун ({REVIVE_COST_RUNES}🀄 нужно)", show_alert=True)
        return
    update_user(u.id, runes=user["runes"] - REVIVE_COST_RUNES)
    # БАГФИКС (вес): раньше при воскрешении вес питомца НЕ сбрасывался и оставался
    # таким же низким, каким был в момент смерти (часто около минимума в 1.0 кг —
    # из-за постепенной потери веса в походах/охоте). В итоге воскрешённый волк
    # навсегда оставался истощённым и слабым на арене (боевая мощь зависит от веса),
    # а набрать вес обратно кормлением (+0.1-0.3 кг за раз) было практически нереально.
    # Теперь воскрешение возвращает вес к стартовому значению нового питомца.
    update_pet(u.id, alive=1, satiety=50, weight=PET_DEFAULT_WEIGHT, starved_since=None,
               busy_until=None, busy_activity=None)
    await reply_or_send(callback.message, 
        f"✨ «{pet['name']}» восстал из мертвых и снова с вами! Вес восстановлен до {PET_DEFAULT_WEIGHT:.1f} кг."
    )
    await callback.answer()


@dp.callback_query(F.data == "decline_revive")
async def cb_decline_revive(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    name = pet["name"] if pet else "питомца"
    update_pet(u.id, name=None)
    update_user(u.id, pending_action="tame")
    await reply_or_send(callback.message, 
        f"Вы решили не воскрешать «{name}». Он отправляется в лучший мир... 💔\n\n"
        "🐾 Придумайте имя вашему новому питомцу:"
    )
    await callback.answer()


@dp.callback_query(F.data == "feed")
async def cb_feed(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Питомца нет", show_alert=True)
        return
    # по запросу: кормить можно даже пока питомец в лесу/на охоте
    if pet["cooked_meat"] < FEED_COOKED_MEAT_COST:
        await callback.answer("Нет готового мяса! Разведите костёр 🔥 и приготовьте еду.", show_alert=True)
        return
    satiety_before = apply_satiety_decay(pet)
    if satiety_before >= FEED_MAX_SATIETY:
        await callback.answer(f"Питомец не голоден (сытость {satiety_before}%) — кормить можно только при сытости меньше {FEED_MAX_SATIETY}%.", show_alert=True)
        return

    satiety_now = min(100, satiety_before + FEED_SATIETY_GAIN)
    # по запросу: вес от кормления должен расти намного медленнее
    # (было 0.1-0.3 кг за одно кормление — набегало слишком быстро)
    weight_gain = round(random.uniform(FEED_WEIGHT_GAIN_MIN, FEED_WEIGHT_GAIN_MAX), 2)
    new_weight = min(MAX_PET_WEIGHT, pet["weight"] + weight_gain)
    update_pet(
        u.id,
        satiety=satiety_now,
        weight=new_weight,
        cooked_meat=pet["cooked_meat"] - FEED_COOKED_MEAT_COST,
        last_fed=datetime.utcnow().isoformat(),
        starved_since=None,
    )
    record_quest_progress(u.id, "feed_pet", 1)
    if new_weight >= MAX_PET_WEIGHT:
        await callback.answer(f"🍖 Сытость восстановлена до 100%! Вес уже на максимуме ({MAX_PET_WEIGHT:.0f} кг)", show_alert=True)
    else:
        await callback.answer(f"🍖 Сытость восстановлена до 100%! Вес +{weight_gain} кг", show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


# =========================================================
#  ЛЕС / ОХОТА
# =========================================================

ACTIVITY_INFO = {
    "лес": ("на прогулку в тёмный лес 🌲", FOREST_MIN_SEC, FOREST_MAX_SEC),
    "охота": ("на охоту! 🏹", HUNT_MIN_SEC, HUNT_MAX_SEC),
}


async def _start_activity_for_user(user_id: int, activity_key: str, chat_id: int | None = None,
                                    joint: bool = False) -> str | None:
    """Запускает поход в лес/на охоту для одного игрока. Возвращает текст для отправки
    (или None, если не удалось — питомца нет/занят)."""
    activity_label, min_sec, max_sec = ACTIVITY_INFO[activity_key]
    pet = get_pet(user_id)
    if pet is None or not pet["alive"]:
        return None
    if pet_is_busy(pet):
        return None

    satiety_now = apply_satiety_decay(pet)
    duration = random.randint(min_sec, max_sec)
    if joint:
        # вместе с партнёром волк возвращается быстрее
        duration = max(60, int(duration * (1 - JOINT_ACTIVITY_SPEED_BONUS_PERCENT / 100)))
    used_speed_potion = bool(pet["pending_potion_speed"])
    if used_speed_potion:
        # зелье скорости — расходуется сразу при отправке в поход
        duration = max(60, int(duration * (1 - POTION_SPEED_BONUS_PERCENT / 100)))
    busy_until = datetime.utcnow() + timedelta(seconds=duration)

    update_pet(
        user_id,
        satiety=satiety_now,
        busy_until=busy_until.isoformat(),
        busy_activity=activity_key,
        joint_activity_bonus=1 if joint else 0,
        pending_potion_speed=0 if used_speed_potion else pet["pending_potion_speed"],
    )
    if activity_key == "лес":
        record_quest_progress(user_id, "send_forest", 1)
    else:
        record_quest_progress(user_id, "send_hunt", 1)
        record_quest_progress(user_id, "hunt_3_times", 1)

    text = (
        f"Вы отправили «{pet['name']}» {activity_label}\n"
        f"Он вернётся через {fmt_timedelta(timedelta(seconds=duration))}"
        + (f"\n💞 Совместный поход — быстрее на {JOINT_ACTIVITY_SPEED_BONUS_PERCENT}%, "
           f"+{JOINT_ACTIVITY_BONUS_XP_PERCENT}% опыта и +{JOINT_ACTIVITY_LOOT_BONUS_PERCENT}% добычи!" if joint else "")
        + (f"\n🧪🏃 Зелье скорости сработало — быстрее на {POTION_SPEED_BONUS_PERCENT}%!" if used_speed_potion else "")
    )
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass
    return text


def activity_choice_kb(activity_key: str, married: bool, has_speed: bool, has_loot: bool) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text="🚶 Один", callback_data=f"actchoice_solo_{activity_key}")]
    if married:
        row1.append(InlineKeyboardButton(text="💞 Вместе", callback_data=f"actchoice_together_{activity_key}"))
    rows = [row1]
    if has_speed:
        rows.append([InlineKeyboardButton(
            text=f"🏃 Один + зелье скорости (-{POTION_SPEED_BONUS_PERCENT}% времени)",
            callback_data=f"actchoice_speed_{activity_key}",
        )])
    if has_loot:
        rows.append([InlineKeyboardButton(
            text=f"🎒 Один + зелье добычи (+{POTION_LOOT_BONUS_PERCENT}% добычи)",
            callback_data=f"actchoice_loot_{activity_key}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def joint_activity_proposal_kb(activity_key: str, proposer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Идти", callback_data=f"joint_yes_{activity_key}_{proposer_id}"),
        InlineKeyboardButton(text="❌ Не идти", callback_data=f"joint_no_{activity_key}_{proposer_id}"),
    ]])


async def _send_pet_activity(callback: CallbackQuery, activity_key: str, activity_label: str,
                              min_sec: int, max_sec: int):
    # если игрок в браке ИЛИ владеет зельями скорости/добычи, сперва предлагаем выбор
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    if pet_is_busy(pet):
        await callback.answer("Питомец уже занят", show_alert=True)
        return

    user = get_or_create_user(u.id, u.username or "", u.full_name)
    has_speed = get_potion_count(u.id, "speed") > 0
    has_loot = get_potion_count(u.id, "loot") > 0
    if user["spouse_id"] or has_speed or has_loot:
        label = "лес 🌲" if activity_key == "лес" else "охоту 🏹"
        await safe_edit_or_send(
            callback.message, f"Пойти в {label}?",
            activity_choice_kb(activity_key, bool(user["spouse_id"]), has_speed, has_loot),
            owner_id=callback.from_user.id,
        )
        await callback.answer()
        return

    text = await _start_activity_for_user(u.id, activity_key)
    if text is None:
        await callback.answer("Не удалось отправить питомца", show_alert=True)
        return
    # уход отправляется всплывающим сообщением сверху экрана, а не в чат
    await callback.answer(text, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


async def _send_pet_activity_msg(message: Message, activity_key: str, activity_label: str,
                                  min_sec: int, max_sec: int):
    """Тот же поход в лес/охоту, но запущенный прямо текстовой командой в чате."""
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await reply_or_send(message, "Сначала приручите волка (команда «волк»).")
        return
    if pet_is_busy(pet):
        await reply_or_send(message, "Питомец уже занят.")
        return

    user = get_or_create_user(u.id, u.username or "", u.full_name)
    has_speed = get_potion_count(u.id, "speed") > 0
    has_loot = get_potion_count(u.id, "loot") > 0
    if user["spouse_id"] or has_speed or has_loot:
        label = "лес 🌲" if activity_key == "лес" else "охоту 🏹"
        sent = await reply_or_send(message, 
            f"Пойти в {label}?",
            reply_markup=activity_choice_kb(activity_key, bool(user["spouse_id"]), has_speed, has_loot),
        )
        remember_owner(sent.chat.id, sent.message_id, u.id)
        return

    text = await _start_activity_for_user(u.id, activity_key)
    if text is None:
        await reply_or_send(message, "Не удалось отправить питомца.")
        return
    await reply_or_send(message, text)


@dp.callback_query(F.data == "forest")
async def cb_forest(callback: CallbackQuery):
    await _send_pet_activity(callback, "лес", "на прогулку в тёмный лес 🌲", FOREST_MIN_SEC, FOREST_MAX_SEC)


@dp.callback_query(F.data == "hunt")
async def cb_hunt(callback: CallbackQuery):
    await _send_pet_activity(callback, "охота", "на охоту! 🏹", HUNT_MIN_SEC, HUNT_MAX_SEC)


@dp.message(F.text.lower() == "лес")
async def cmd_forest_text(message: Message):
    # по запросу: команду "лес" теперь можно писать прямо в чат
    await _send_pet_activity_msg(message, "лес", "на прогулку в тёмный лес 🌲", FOREST_MIN_SEC, FOREST_MAX_SEC)


@dp.message(F.text.lower() == "охота")
async def cmd_hunt_text(message: Message):
    # по запросу: команду "охота" теперь можно писать прямо в чат
    await _send_pet_activity_msg(message, "охота", "на охоту! 🏹", HUNT_MIN_SEC, HUNT_MAX_SEC)


@dp.callback_query(F.data.startswith("actchoice_solo_"))
async def cb_activity_solo(callback: CallbackQuery):
    activity_key = callback.data[len("actchoice_solo_"):]
    u = callback.from_user
    text = await _start_activity_for_user(u.id, activity_key)
    if text is None:
        await callback.answer("Не удалось отправить питомца", show_alert=True)
        return
    # уход отправляется всплывающим сообщением сверху экрана, а не в чат
    await callback.answer(text, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


@dp.callback_query(F.data.startswith("actchoice_speed_"))
async def cb_activity_speed(callback: CallbackQuery):
    # зелье скорости активируется прямо в момент отправки в поход
    activity_key = callback.data[len("actchoice_speed_"):]
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"] or pet_is_busy(pet):
        await callback.answer("Питомец недоступен", show_alert=True)
        return
    if get_potion_count(u.id, "speed") <= 0:
        await callback.answer("Зелье скорости закончилось", show_alert=True)
        return
    add_potion(u.id, "speed", -1)
    update_pet(u.id, pending_potion_speed=1)
    text = await _start_activity_for_user(u.id, activity_key)
    if text is None:
        add_potion(u.id, "speed", 1)
        update_pet(u.id, pending_potion_speed=0)
        await callback.answer("Не удалось отправить питомца", show_alert=True)
        return
    await callback.answer(text, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


@dp.callback_query(F.data.startswith("actchoice_loot_"))
async def cb_activity_loot(callback: CallbackQuery):
    # зелье добычи активируется прямо в момент отправки в поход
    activity_key = callback.data[len("actchoice_loot_"):]
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"] or pet_is_busy(pet):
        await callback.answer("Питомец недоступен", show_alert=True)
        return
    if get_potion_count(u.id, "loot") <= 0:
        await callback.answer("Зелье добычи закончилось", show_alert=True)
        return
    add_potion(u.id, "loot", -1)
    update_pet(u.id, pending_potion_loot=1)
    text = await _start_activity_for_user(u.id, activity_key)
    if text is None:
        add_potion(u.id, "loot", 1)
        update_pet(u.id, pending_potion_loot=0)
        await callback.answer("Не удалось отправить питомца", show_alert=True)
        return
    await callback.answer(text, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


@dp.callback_query(F.data.startswith("actchoice_together_"))
async def cb_activity_together(callback: CallbackQuery):
    activity_key = callback.data[len("actchoice_together_"):]
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["spouse_id"]:
        await callback.answer("Вы не в браке", show_alert=True)
        return
    pet = get_pet(u.id)
    spouse_pet = get_pet(user["spouse_id"])
    if pet is None or not pet["alive"] or pet_is_busy(pet):
        await callback.answer("Ваш питомец недоступен", show_alert=True)
        return
    if spouse_pet is None or not spouse_pet["alive"] or pet_is_busy(spouse_pet):
        await callback.answer("Питомец вашей пары сейчас недоступен", show_alert=True)
        return

    label = "лес 🌲" if activity_key == "лес" else "охоту 🏹"
    try:
        await bot.send_message(
            user["spouse_id"],
            f"💞 «{user['display_name']}» предлагает вместе пойти в {label} Пойдёте?",
            reply_markup=joint_activity_proposal_kb(activity_key, u.id),
        )
        await safe_edit_or_send(callback.message, f"💞 Предложение отправлено партнёру! Ожидайте ответа.", owner_id=callback.from_user.id)
    except Exception:
        await callback.answer("Не удалось отправить предложение партнёру", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("joint_yes_"))
async def cb_joint_activity_yes(callback: CallbackQuery):
    rest = callback.data[len("joint_yes_"):]
    activity_key, proposer_id_str = rest.rsplit("_", 1)
    proposer_id = int(proposer_id_str)
    u = callback.from_user

    text_a = await _start_activity_for_user(proposer_id, activity_key, chat_id=proposer_id, joint=True)
    text_b = await _start_activity_for_user(u.id, activity_key, joint=True)
    if text_a is None or text_b is None:
        await callback.message.edit_text("Не получилось отправиться вместе — у кого-то из вас питомец занят или мёртв.")
        await callback.answer()
        return
    await callback.message.edit_text(f"💞 Вы отправились вместе!\n\n{text_b}")
    await callback.answer()


@dp.callback_query(F.data.startswith("joint_no_"))
async def cb_joint_activity_no(callback: CallbackQuery):
    rest = callback.data[len("joint_no_"):]
    activity_key, proposer_id_str = rest.rsplit("_", 1)
    proposer_id = int(proposer_id_str)
    await callback.message.edit_text("❌ Вы отказались идти вместе.")
    try:
        await bot.send_message(proposer_id, "❌ Партнёр отказался идти вместе.")
    except Exception:
        pass
    await callback.answer()


# =========================================================
#  АРЕНА / БОИ
# =========================================================

def _pet_power(pet: sqlite3.Row) -> float:
    """Условная 'боевая мощь' волка: зависит от веса, сытости и уровня.
    Зелье силы (если активировано заранее) даёт +25% к этой мощи на один бой."""
    base = pet["weight"] * 1.5 + pet["satiety"] * 0.3 + pet["level"] * 8 + random.uniform(0, 10)
    if pet["pending_potion_strength"]:
        base *= 1 + POTION_STRENGTH_BONUS_PERCENT / 100
    return base


async def _show_arena(target: Message, owner_id: int, pet: sqlite3.Row):
    conn = db()
    rows = conn.execute(
        """SELECT p.*, u.display_name FROM pets p
           JOIN users u ON u.user_id = p.user_id
           WHERE p.alive=1 AND p.in_arena=1 AND p.user_id != ? AND p.busy_until IS NULL
           ORDER BY RANDOM() LIMIT 8""",
        (pet["user_id"],),
    ).fetchall()
    conn.close()
    if not rows:
        await safe_edit_or_send(
            target,
            # теперь на арене видны только те, кто сам нажал «Войти на арену»
            "⚔️ Арена пуста — пока никто не вошёл на арену для боя.\nНажмите «🚩 Войти на арену», чтобы появиться там самому.",
            arena_kb([], in_arena=bool(pet["in_arena"])),
            owner_id=owner_id,
        )
        return
    await safe_edit_or_send(target, "⚔️ АРЕНА — выберите соперника:", arena_kb(rows, in_arena=bool(pet["in_arena"])),
                             owner_id=owner_id)


@dp.callback_query(F.data == "arena")
async def cb_arena(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    if pet_is_busy(pet):
        await callback.answer("Питомец занят", show_alert=True)
        return
    await _show_arena(callback.message, u.id, pet)
    await callback.answer()


@dp.message(F.text.lower() == "арена")
async def cmd_arena_text(message: Message):
    # по запросу: команду "арена" теперь можно писать прямо в чат
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await reply_or_send(message, "Сначала приручите волка (команда «волк»).")
        return
    if pet_is_busy(pet):
        await reply_or_send(message, "Питомец сейчас занят.")
        return
    await _show_arena(message, u.id, pet)


@dp.callback_query(F.data == "arena_toggle")
async def cb_arena_toggle(callback: CallbackQuery):
    # /«Войти на арену» живёт внутри меню арены и переключает
    # видимость питомца как цели для других игроков, оставаясь на этом же экране
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    new_state = 0 if pet["in_arena"] else 1
    update_pet(u.id, in_arena=new_state)
    # уведомление всплывающим сообщением сверху экрана, без флуда в чате
    await callback.answer(
        "🚩 Вы вошли на арену! Теперь другие игроки видят вас как цель для боя."
        if new_state else "🚪 Вы покинули арену. Вас больше не видно как цель для боя.",
        show_alert=True,
    )
    pet = get_pet(u.id)
    await _show_arena(callback.message, u.id, pet)


@dp.callback_query(F.data.startswith("fight_"))
async def cb_fight_target(callback: CallbackQuery):
    u = callback.from_user
    target_id = int(callback.data.split("_", 1)[1])
    # зелье силы активируется прямо в момент боя
    if get_potion_count(u.id, "strength") > 0:
        await safe_edit_or_send(callback.message, "⚔️ Как сразиться?", fight_confirm_kb(target_id), owner_id=u.id)
        await callback.answer()
        return
    result = await start_wolf_fight(u.id, target_id)
    if result is None:
        await callback.answer("Соперник недоступен", show_alert=True)
        return
    await callback.answer(result, show_alert=True)
    await send_pet_screen(callback.message.chat.id, u.id, edit_message=callback.message)


def fight_challenge_kb(attacker_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять бой", callback_data=f"fightacc_{attacker_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"fightdec_{attacker_id}"),
    ]])


async def start_wolf_fight(attacker_id: int, defender_id: int) -> str | None:
    """По запросу: бой больше не начинается сразу по клику атакующего — сопернику
    в чат приходит вызов с кнопками "Принять"/"Отклонить", и настоящий 30-минутный
    бой стартует только после согласия (см. cb_fight_accept). Если не ответит за
    FIGHT_CHALLENGE_TIMEOUT_SECONDS — вызов сгорает автоматически."""
    if attacker_id == defender_id:
        return "Нельзя биться со своим же волком 😅"
    attacker = get_pet(attacker_id)
    defender = get_pet(defender_id)
    if attacker is None or not attacker["alive"] or defender is None or not defender["alive"]:
        return None
    if pet_is_busy(attacker):
        return "Ваш питомец сейчас занят."
    if pet_is_busy(defender):
        return "Питомец соперника сейчас занят."

    until = datetime.utcnow() + timedelta(seconds=FIGHT_CHALLENGE_TIMEOUT_SECONDS)
    update_pet(attacker_id, busy_until=until.isoformat(), busy_activity=FIGHT_CHALLENGE_ACTIVITY,
               fight_opponent_id=defender_id, fight_is_attacker=1)
    update_pet(defender_id, busy_until=until.isoformat(), busy_activity=FIGHT_CHALLENGE_ACTIVITY,
               fight_opponent_id=attacker_id, fight_is_attacker=0)

    try:
        sent = await bot.send_message(
            defender_id,
            f"⚔️ Волк «{attacker['name']}» вызывает вашего «{defender['name']}» на бой!\n"
            f"Согласны сразиться? У вас есть {fmt_timedelta(timedelta(seconds=FIGHT_CHALLENGE_TIMEOUT_SECONDS))}, чтобы ответить.",
            reply_markup=fight_challenge_kb(attacker_id),
        )
        remember_owner(sent.chat.id, sent.message_id, defender_id)
    except Exception:
        pass
    return f"⚔️ Вызов на бой отправлен «{defender['name']}»! Ждём согласия соперника (до {fmt_timedelta(timedelta(seconds=FIGHT_CHALLENGE_TIMEOUT_SECONDS))})."


@dp.callback_query(F.data.startswith("fightacc_"))
async def cb_fight_accept(callback: CallbackQuery):
    u = callback.from_user
    attacker_id = int(callback.data[len("fightacc_"):])
    defender = get_pet(u.id)
    attacker = get_pet(attacker_id)
    valid = (
        defender is not None and defender["alive"]
        and defender["busy_activity"] == FIGHT_CHALLENGE_ACTIVITY
        and defender["fight_opponent_id"] == attacker_id
        and attacker is not None and attacker["alive"]
        and attacker["fight_opponent_id"] == u.id
    )
    if not valid:
        await callback.answer("Этот вызов уже неактуален.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    until = datetime.utcnow() + timedelta(seconds=FIGHT_DURATION_SECONDS)
    update_pet(attacker_id, busy_until=until.isoformat(), busy_activity="бой")
    update_pet(u.id, busy_until=until.isoformat(), busy_activity="бой")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("⚔️ Вы приняли бой!", show_alert=True)
    try:
        await bot.send_message(
            attacker_id,
            f"⚔️ «{defender['name']}» принял ваш вызов! Итог будет известен через "
            f"{fmt_timedelta(timedelta(seconds=FIGHT_DURATION_SECONDS))}.",
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("fightdec_"))
async def cb_fight_decline(callback: CallbackQuery):
    u = callback.from_user
    attacker_id = int(callback.data[len("fightdec_"):])
    defender = get_pet(u.id)
    valid = (
        defender is not None
        and defender["busy_activity"] == FIGHT_CHALLENGE_ACTIVITY
        and defender["fight_opponent_id"] == attacker_id
    )
    if not valid:
        await callback.answer("Этот вызов уже неактуален.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    update_pet(attacker_id, busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0)
    update_pet(u.id, busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Вы отклонили вызов.", show_alert=True)
    try:
        await bot.send_message(attacker_id, f"❌ «{defender['name']}» отклонил ваш вызов на бой.")
    except Exception:
        pass


async def resolve_fight_challenge_timeout(attacker: sqlite3.Row):
    """Вызов на бой сгорел — соперник не ответил вовремя."""
    attacker_id = attacker["user_id"]
    defender_id = attacker["fight_opponent_id"]
    update_pet(attacker_id, busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0)
    if defender_id:
        update_pet(defender_id, busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0)
    try:
        await bot.send_message(attacker_id, "⏰ Соперник не ответил на вызов вовремя — бой отменён.")
    except Exception:
        pass
    if defender_id:
        try:
            await bot.send_message(defender_id, "⏰ Время на ответ по вызову на бой истекло.")
        except Exception:
            pass


async def resolve_wolf_fight(attacker: sqlite3.Row):
    """Вызывается фоновым циклом, когда 30-минутный бой завершился."""
    attacker_id = attacker["user_id"]
    defender_id = attacker["fight_opponent_id"]
    defender = get_pet(defender_id)

    if defender is None or not defender["alive"]:
        update_pet(attacker_id, busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0, in_arena=0, pending_potion_strength=0)
        try:
            await bot.send_message(attacker_id, "⚔️ Соперник выбыл — бой отменён.")
        except Exception:
            pass
        return

    power_a = _pet_power(attacker)
    power_b = _pet_power(defender)
    total = power_a + power_b
    attacker_wins = random.random() < (power_a / total if total > 0 else 0.5)

    sat_loss_a = random.randint(5, 12)
    sat_loss_b = random.randint(5, 12)

    if attacker_wins:
        bones_won = random.randint(2, 6)
        xp_won = random.randint(10, 25)
        update_pet(attacker_id, satiety=max(0, apply_satiety_decay(attacker) - sat_loss_a),
                   bones=attacker["bones"] + bones_won, busy_until=None, busy_activity=None,
                   fight_opponent_id=None, fight_is_attacker=0, in_arena=0, pending_potion_strength=0)
        update_pet(defender_id, satiety=max(0, apply_satiety_decay(defender) - sat_loss_b),
                   busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0, in_arena=0,
                   pending_potion_strength=0)
        grant_pet_xp(get_pet(attacker_id), xp_won)
        record_quest_progress(attacker_id, "win_fight", 1)
        win_text = (f"🏆 Победа! «{attacker['name']}» одолел «{defender['name']}»!\n"
                    f"+{bones_won} 🦴, +{xp_won} ✨ опыта питомцу")
        lose_text = f"⚔️ Ваш «{defender['name']}» проиграл бой волку «{attacker['name']}»! Сытость -{sat_loss_b}%"
    else:
        update_pet(attacker_id, satiety=max(0, apply_satiety_decay(attacker) - sat_loss_a),
                   busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0, in_arena=0,
                   pending_potion_strength=0)
        update_pet(defender_id, satiety=max(0, apply_satiety_decay(defender) - sat_loss_b),
                   busy_until=None, busy_activity=None, fight_opponent_id=None, fight_is_attacker=0, in_arena=0,
                   pending_potion_strength=0)
        record_quest_progress(defender_id, "win_fight", 1)
        win_text = f"⚔️ Ваш «{defender['name']}» отбился от волка «{attacker['name']}» и победил!"
        lose_text = f"💔 Поражение... «{defender['name']}» оказался сильнее. Сытость -{sat_loss_a}%"

    try:
        await bot.send_message(attacker_id, win_text if attacker_wins else lose_text)
    except Exception as e:
        log.warning("Не удалось уведомить о результате боя %s: %s", attacker_id, e)
    try:
        await bot.send_message(defender_id, lose_text if attacker_wins else win_text)
    except Exception as e:
        log.warning("Не удалось уведомить о результате боя %s: %s", defender_id, e)


# =========================================================
#  КОСТЁР
# =========================================================

@dp.callback_query(F.data == "campfire")
async def cb_campfire(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    await safe_edit_or_send(callback.message, campfire_text(pet), campfire_kb(u.id), owner_id=callback.from_user.id)
    await callback.answer()


@dp.message(F.text.lower().in_(["костёр", "костер"]))
async def cmd_campfire_text(message: Message):
    # по запросу: команду "костёр" теперь можно писать прямо в чат
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await reply_or_send(message, "Сначала приручите волка (команда «волк»).")
        return
    await safe_edit_or_send(message, campfire_text(pet), campfire_kb(u.id), owner_id=u.id)


@dp.callback_query(F.data == "campfire_cook")
async def cb_campfire_cook(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    if pet["cooking_until"] and datetime.utcnow() < datetime.fromisoformat(pet["cooking_until"]):
        await callback.answer("Мясо уже готовится на костре", show_alert=True)
        return
    if pet["firewood"] < CAMPFIRE_WOOD_COST_MIN or pet["raw_meat"] < CAMPFIRE_MEAT_COST:
        await callback.answer(
            f"Не хватает ресурсов! Нужно хотя бы {CAMPFIRE_WOOD_COST_MIN} 🪵 дров и {CAMPFIRE_MEAT_COST} 🥩 плоти.",
            show_alert=True,
        )
        return

    # По запросу: расход дров на готовку теперь случайный каждый раз (0.25–2),
    # но не больше, чем реально есть у игрока
    wood_cost = round(min(random.uniform(CAMPFIRE_WOOD_COST_MIN, CAMPFIRE_WOOD_COST_MAX), pet["firewood"]), 2)
    cooking_until = datetime.utcnow() + timedelta(seconds=CAMPFIRE_COOK_SECONDS)
    update_pet(
        u.id,
        firewood=pet["firewood"] - wood_cost,
        raw_meat=pet["raw_meat"] - CAMPFIRE_MEAT_COST,
        cooking_until=cooking_until.isoformat(),
    )
    # уведомление всплывающим сообщением сверху экрана, без флуда в чате
    await callback.answer(
        f"🔥 Костёр разожжён! Потрачено {wood_cost:.2f} 🪵 дров. "
        f"Мясо будет готово через {fmt_timedelta(timedelta(seconds=CAMPFIRE_COOK_SECONDS))}",
        show_alert=True,
    )
    pet = get_pet(u.id)
    await safe_edit_or_send(callback.message, campfire_text(pet), campfire_kb(u.id), owner_id=callback.from_user.id)


@dp.callback_query(F.data == "campfire_potion_cook")
async def cb_campfire_potion_cook(callback: CallbackQuery):
    # По запросу: зелье жарки — мгновенно, без затрат дров, 1 мясо -> 2 готового
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    if get_potion_count(u.id, "cooking") <= 0:
        await callback.answer("У вас нет зелья жарки. Купите его в магазине 🏪 → 🧪 Зелья.", show_alert=True)
        return
    if pet["raw_meat"] < 1:
        await callback.answer("Нет сырого мяса для готовки.", show_alert=True)
        return
    add_potion(u.id, "cooking", -1)
    update_pet(
        u.id,
        raw_meat=pet["raw_meat"] - 1,
        cooked_meat=pet["cooked_meat"] + POTION_COOKING_OUTPUT,
    )
    record_quest_progress(u.id, "cook_meat", POTION_COOKING_OUTPUT)
    await callback.answer(
        f"🧪🔥 Зелье жарки использовано! Мгновенно получено {POTION_COOKING_OUTPUT} 🍖 готового мяса, дрова не потрачены.",
        show_alert=True,
    )
    pet = get_pet(u.id)
    await safe_edit_or_send(callback.message, campfire_text(pet), campfire_kb(u.id), owner_id=callback.from_user.id)


# =========================================================
#  ПЕРЕИМЕНОВАНИЕ
# =========================================================

@dp.callback_query(F.data == "rename")
async def cb_rename(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    if user["runes"] < RENAME_COST_RUNES:
        await callback.answer(f"Недостаточно рун ({RENAME_COST_RUNES}🀄 нужно)", show_alert=True)
        return
    sent = await reply_or_send(callback.message, 
        "✏️ Пришлите новое имя для питомца ОТВЕТОМ (реплаем) на это сообщение."
    )
    update_user(u.id, pending_action="rename", pending_msg_id=sent.message_id)
    await callback.answer()


# =========================================================
#  ПОДАРОК / ЛАБИРИНТ / МАГАЗИН
# =========================================================

@dp.callback_query(F.data == "gift")
async def cb_gift(callback: CallbackQuery):
    # кнопка 🎁 это тот же бонус, что и команда "бонус" (одна система)
    u = callback.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await callback.answer(await claim_periodic_bonus(u.id), show_alert=True)


# =========================================================
#  ЛАБИРИНТ — усложнённая версия
#  Нужно 10 верных шагов подряд по прогрессу (макс. 3 ошибки), приз крупный
# =========================================================

def maze_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=MAZE_DIRECTION_LABELS["left"], callback_data="maze_left"),
        InlineKeyboardButton(text=MAZE_DIRECTION_LABELS["straight"], callback_data="maze_straight"),
        InlineKeyboardButton(text=MAZE_DIRECTION_LABELS["right"], callback_data="maze_right"),
    ]])


def maze_step_text(progress: int, mistakes: int) -> str:
    return (
        "🔑 ЛАБИРИНТ\n"
        "————————————\n"
        f"Пройдено шагов: {progress}/{MAZE_STEPS_TO_WIN}\n"
        f"Ошибок: {mistakes}/{MAZE_MAX_MISTAKES}\n\n"
        "Куда пойдёт ваш волк?"
    )


@dp.callback_query(F.data == "maze")
async def cb_maze(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["maze_active"]:
        await safe_edit_or_send(
            callback.message,
            maze_step_text(user["maze_progress"], user["maze_mistakes"]),
            maze_kb(),
        owner_id=callback.from_user.id)
        await callback.answer()
        return
    if user["keys"] < MAZE_KEY_COST:
        await callback.answer(f"Нужен хотя бы {MAZE_KEY_COST} ключ(а) 🔑", show_alert=True)
        return
    correct = random.choice(MAZE_DIRECTIONS)
    update_user(
        u.id,
        keys=user["keys"] - MAZE_KEY_COST,
        maze_active=1,
        maze_progress=0,
        maze_mistakes=0,
        maze_correct=correct,
    )
    await safe_edit_or_send(callback.message, maze_step_text(0, 0), maze_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.in_(["maze_left", "maze_straight", "maze_right"]))
async def cb_maze_move(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if not user["maze_active"]:
        await callback.answer("Сначала начните лабиринт (🔑 Лабиринт)", show_alert=True)
        return

    chosen = callback.data.split("_", 1)[1]
    correct = user["maze_correct"]
    progress = user["maze_progress"]
    mistakes = user["maze_mistakes"]
    was_correct = chosen == correct

    if was_correct:
        progress += 1
    else:
        mistakes += 1

    if progress >= MAZE_STEPS_TO_WIN:
        maze_reward = dict(dollars=random.randint(MAZE_WIN_DOLLARS_MIN, MAZE_WIN_DOLLARS_MAX), runes=MAZE_WIN_RUNES)
        update_user(u.id, maze_active=0, maze_progress=0, maze_mistakes=0, maze_correct=None)
        grant_rewards(u.id, **maze_reward)
        # по запросу: очень редкий шанс найти ларец прямо в лабиринте
        found_larets = random.random() < MAZE_LARETS_DROP_CHANCE
        extra_line = ""
        if found_larets:
            fresh = get_or_create_user(u.id, "", "")
            update_user(u.id, larets=fresh["larets"] + 1)
            extra_line = "\n\n🎁 Невероятная удача — среди стен лабиринта вы нашли ЛАРЕЦ!"
        await safe_edit_or_send(
            callback.message,
            "🎉 ЛАБИРИНТ ПРОЙДЕН!\n\nВы нашли выход и главный приз:\n"
            f"🏆 {_reward_str(maze_reward)}" + extra_line,
            None,
        owner_id=callback.from_user.id)
        await callback.answer()
        return

    if mistakes >= MAZE_MAX_MISTAKES:
        update_user(u.id, maze_active=0, maze_progress=0, maze_mistakes=0, maze_correct=None)
        await safe_edit_or_send(
            callback.message,
            f"💀 Лабиринт провален — слишком много ошибок ({MAZE_MAX_MISTAKES}/{MAZE_MAX_MISTAKES}).\n"
            "Попробуйте ещё раз позже!",
            None,
        owner_id=callback.from_user.id)
        await callback.answer()
        return

    # при ошибке верное направление НЕ меняется, чтобы игрок мог
    # спокойно перебрать оставшиеся варианты; новое направление выбирается
    # только после того, как шаг пройден верно.
    next_correct = random.choice(MAZE_DIRECTIONS) if was_correct else correct
    update_user(u.id, maze_progress=progress, maze_mistakes=mistakes, maze_correct=next_correct)
    note = "✅ Верно!" if was_correct else "❌ Ошибка! Путь не изменился — попробуйте другое направление."
    await callback.answer(note)
    await safe_edit_or_send(callback.message, maze_step_text(progress, mistakes), maze_kb(), owner_id=callback.from_user.id)


# =========================================================
#  ЛАРЕЦ — команды и колбэки (по запросу)
# =========================================================
def larets_status_kb(can_open: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_open:
        rows.append([InlineKeyboardButton(
            text=f"🎁 Открыть ларец ({LARETS_OPEN_KEY_COST}🔑)", callback_data="larets_open")])
    rows.append([InlineKeyboardButton(text="⬅ Назад к питомцу", callback_data="pet_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def larets_grid_kb() -> InlineKeyboardMarkup:
    rows = []
    idx = 0
    for _r in range(3):
        row = []
        for _c in range(3):
            row.append(InlineKeyboardButton(text="❓", callback_data=f"larpick_{idx}"))
            idx += 1
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_larets_screen(message: Message, user_id: int):
    user = get_or_create_user(user_id, "", "")
    can_open = user["larets"] >= 1 and user["keys"] >= LARETS_OPEN_KEY_COST
    text = (
        "🎁 ЛАРЕЦ\n————————————\n"
        f"У вас: {user['larets']} 🎁 ларцов, {user['keys']} 🔑 ключей\n"
        f"Открытие стоит {LARETS_OPEN_KEY_COST} 🔑 ключей + 1 🎁 ларец.\n\n"
        "Ларец — очень редкая находка (можно найти в лабиринте либо получить "
        "от администратора). Внутри — случайный ценный приз."
    )
    if user["larets"] < 1:
        text += "\n\n❌ У вас пока нет ларцов."
    elif user["keys"] < LARETS_OPEN_KEY_COST:
        text += f"\n\n❌ Не хватает ключей (нужно {LARETS_OPEN_KEY_COST})."
    sent = await reply_or_send(message, text, reply_markup=larets_status_kb(can_open))
    remember_owner(sent.chat.id, sent.message_id, user_id)


@dp.message(F.text.lower() == "ларец")
async def cmd_larets_text(message: Message):
    u = message.from_user
    await _send_larets_screen(message, u.id)


@dp.callback_query(F.data == "larets_open")
async def cb_larets_open(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["larets"] < 1:
        await callback.answer("У вас нет ларцов.", show_alert=True)
        return
    if user["keys"] < LARETS_OPEN_KEY_COST:
        await callback.answer(f"Нужно {LARETS_OPEN_KEY_COST} 🔑 ключей.", show_alert=True)
        return
    update_user(u.id, larets=user["larets"] - 1, keys=user["keys"] - LARETS_OPEN_KEY_COST)
    await safe_edit_or_send(
        callback.message,
        "🎁 Ларец открыт! Выберите одну из 9 ячеек:",
        larets_grid_kb(),
    owner_id=u.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("larpick_"))
async def cb_larets_pick(callback: CallbackQuery):
    u = callback.from_user
    prize = random.choices(LARETS_PRIZES, weights=[p["weight"] for p in LARETS_PRIZES], k=1)[0]
    grant = prize["grant"]

    user = get_or_create_user(u.id, u.username or "", u.full_name)
    updates = {}
    if "dollars" in grant:
        updates["dollars"] = user["dollars"] + grant["dollars"]
    if "runes" in grant:
        updates["runes"] = user["runes"] + grant["runes"]
    if "rings" in grant:
        updates["rings"] = user["rings"] + grant["rings"]
    if updates:
        update_user(u.id, **updates)
    if "potion" in grant:
        kind, qty = grant["potion"]
        add_potion(u.id, kind, qty)
    if "skin" in grant:
        grant_skin_ownership(u.id, grant["skin"])

    try:
        await callback.message.edit_text(f"🎁 Вы открыли ларец!\n\nВыпало: {prize['label']} 🎉")
    except Exception:
        await reply_or_send(callback.message, f"🎁 Вы открыли ларец!\n\nВыпало: {prize['label']} 🎉")
    await callback.answer("🎉 Приз получен!", show_alert=True)


@dp.callback_query(F.data == "shop")
async def cb_shop(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "🏪 Магазин — выберите раздел:", shop_main_kb(), owner_id=callback.from_user.id)
    await callback.answer()@dp.message(F.text.lower() == "магазин")
async def cmd_shop_text(message: Message):
    # по запросу: команду "магазин" теперь можно писать прямо в чат
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    await safe_edit_or_send(message, "🏪 Магазин — выберите раздел:", shop_main_kb(), owner_id=u.id)


@dp.callback_query(F.data == "shop_prefix")
async def cb_shop_prefix(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "📛 Префикс — временное цветное имя в чате.", shop_prefix_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "shop_antitarget")
async def cb_shop_antitarget(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "🛡 Анти-таргет — защита от негативных ролей/действий на время.",
                             shop_antitarget_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "shop_skins")
async def cb_shop_skins(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "🐺 Скины питомца:", shop_skins_kb(callback.from_user.id), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "shop_rings")
async def cb_shop_rings(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "💍 Кольца используются для брака между игроками (см. «!брак»).",
                             shop_rings_kb(), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "skin_menu")
async def cb_skin_menu(callback: CallbackQuery):
    # кнопка 🎭 в меню питомца открывает смену скина
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    await safe_edit_or_send(
        callback.message,
        f"🎭 Скины питомца (сейчас: «{pet['skin']}»):",
        pet_skins_kb(u.id, pet["skin"]),
    owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "potions_menu")
async def cb_potions_menu(callback: CallbackQuery):
    u = callback.from_user
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return
    await safe_edit_or_send(callback.message, potions_text(u.id, pet), potions_kb(u.id, pet), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "shop_potions")
async def cb_shop_potions(callback: CallbackQuery):
    await safe_edit_or_send(callback.message, "🧪 Зелья — покупаются за доллары и хранятся в инвентаре:",
                             shop_potions_kb(callback.from_user.id), owner_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_potion_"))
async def cb_buy_potion(callback: CallbackQuery):
    ptype = callback.data[len("buy_potion_"):]
    info = POTION_DEFS.get(ptype)
    if info is None:
        await callback.answer("Зелье не найдено", show_alert=True)
        return
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["dollars"] < info["price"]:
        await callback.answer("Недостаточно долларов 💵", show_alert=True)
        return
    update_user(u.id, dollars=user["dollars"] - info["price"])
    add_potion(u.id, ptype, 1)
    await callback.answer(f"✅ Куплено: {info['icon']} {info['title']}!", show_alert=True)
    await safe_edit_or_send(callback.message, "🧪 Зелья — покупаются за доллары и хранятся в инвентаре:",
                             shop_potions_kb(u.id), owner_id=callback.from_user.id)


@dp.message(F.text.lower() == "зелья")
async def cmd_potions(message: Message):
    u = message.from_user
    get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await reply_or_send(message, "Сначала приручите волка (команда «волк»).")
        return
    sent = await reply_or_send(message, potions_text(u.id, pet), reply_markup=potions_kb(u.id, pet))
    remember_owner(sent.chat.id, sent.message_id, u.id)


@dp.callback_query(F.data.startswith("buy_prefix_"))
async def cb_buy_prefix(callback: CallbackQuery):
    # покупка списывает деньги сразу, но выдача идёт заявкой админу вручную
    days = int(callback.data.split("_")[-1])
    price = next(p for d, p in PREFIX_PRICES.values() if d == days)
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["dollars"] < price:
        await callback.answer("Недостаточно долларов 💵", show_alert=True)
        return
    update_user(u.id, dollars=user["dollars"] - price)
    product = f"Префикс на {days} дней"
    order_id = await create_order(get_or_create_user(u.id, u.username or "", u.full_name), product,
                                   {"type": "prefix", "days": days, "price": price})
    await callback.answer(f"✅ Заказ #{order_id} создан! «{product}» будет выдан администратором вручную.", show_alert=True)


@dp.callback_query(F.data.startswith("buy_at_"))
async def cb_buy_antitarget(callback: CallbackQuery):
    # покупка списывает деньги сразу, но выдача идёт заявкой админу вручную
    days = int(callback.data.split("_")[-1])
    price = next(p for d, p in ANTITARGET_PRICES.values() if d == days)
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["dollars"] < price:
        await callback.answer("Недостаточно долларов 💵", show_alert=True)
        return
    update_user(u.id, dollars=user["dollars"] - price)
    product = f"Антитаргет на {days} дней"
    order_id = await create_order(get_or_create_user(u.id, u.username or "", u.full_name), product,
                                   {"type": "antitarget", "days": days, "price": price})
    await callback.answer(f"✅ Заказ #{order_id} создан! «{product}» будет выдан администратором вручную.", show_alert=True)


# =========================================================
#  ЗАЯВКИ АДМИНАМ НА РУЧНУЮ ВЫДАЧУ
# =========================================================

async def create_order(user: sqlite3.Row, product: str, payload: dict) -> int:
    conn = db()
    cur = conn.cursor()
    insert_sql = (
        "INSERT INTO orders (user_id, username, display_name, product, status, created_at, payload) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    params = (user["user_id"], user["username"] or "", user["display_name"] or "", product, "pending",
              datetime.utcnow().isoformat(), json.dumps(payload))
    if USE_POSTGRES:
        # у Postgres нет lastrowid — забираем сгенерированный id через RETURNING
        cur.execute(insert_sql + " RETURNING order_id", params)
        order_id = cur.fetchone()["order_id"]
    else:
        cur.execute(insert_sql, params)
        order_id = cur.lastrowid
    conn.commit()
    conn.close()
    await notify_admins_new_order(order_id, user, product)
    return order_id


async def notify_admins_new_order(order_id: int, user: sqlite3.Row, product: str):
    username = user["username"] or ""
    display = (user["display_name"] or str(user["user_id"])).upper()
    text = (
        f"⚠️ Новый заказ #{order_id} на ручную выдачу ⚠️\n\n"
        f"Товар: {product}\n"
        f"Пользователь: {display}" + (f" (@{username})" if username else "") + "\n"
        f"ID: {user['user_id']}\n\n"
        "Связаться с пользователем:\n"
        f"Android: {'https://t.me/' + username if username else 'tg://openmessage?user_id=' + str(user['user_id'])}\n"
        f"iOS: tg://user?id={user['user_id']}\n\n"
        "Используйте /list, чтобы взять заказ в работу."
    )
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            log.warning("Не удалось уведомить админа %s о заказе: %s", admin_id, e)


def get_pending_orders(limit: int = 50) -> list[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status='pending' ORDER BY order_id ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


async def fulfill_order(order_id: int, admin_id: int) -> tuple[bool, str]:
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    conn.close()
    if o is None or o["status"] != "pending":
        return False, "Этот заказ уже обработан."

    payload = json.loads(o["payload"]) if o["payload"] else {}
    user_id = o["user_id"]
    user = get_or_create_user(user_id, "", "")
    ptype = payload.get("type")
    base = datetime.utcnow()

    if ptype == "prefix":
        days = payload["days"]
        current = datetime.fromisoformat(user["prefix_until"]) if user["prefix_until"] else base
        if current < base:
            current = base
        new_until = current + timedelta(days=days)
        update_user(user_id, prefix_until=new_until.isoformat())
        result_text = f"✅ Ваш заказ «{o['product']}» выполнен! Префикс активен до {new_until.strftime('%d.%m.%Y %H:%M')}"
    elif ptype == "antitarget":
        days = payload["days"]
        current = datetime.fromisoformat(user["antitarget_until"]) if user["antitarget_until"] else base
        if current < base:
            current = base
        new_until = current + timedelta(days=days)
        update_user(user_id, antitarget_until=new_until.isoformat())
        result_text = f"✅ Ваш заказ «{o['product']}» выполнен! Анти-таргет активен до {new_until.strftime('%d.%m.%Y %H:%M')}"
    else:
        result_text = f"✅ Ваш заказ «{o['product']}» выполнен!"

    conn = db()
    conn.execute("UPDATE orders SET status='done', taken_by=? WHERE order_id=?", (admin_id, order_id))
    conn.commit()
    conn.close()
    try:
        await bot.send_message(user_id, result_text)
    except Exception:
        pass
    return True, result_text


async def decline_order(order_id: int, admin_id: int) -> tuple[bool, str]:
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if o is None or o["status"] != "pending":
        conn.close()
        return False, "Этот заказ уже обработан."
    payload = json.loads(o["payload"]) if o["payload"] else {}
    price = payload.get("price", 0)
    if price:
        conn.execute("UPDATE users SET dollars = dollars + ? WHERE user_id=?", (price, o["user_id"]))
    conn.execute("UPDATE orders SET status='declined', taken_by=? WHERE order_id=?", (admin_id, order_id))
    conn.commit()
    conn.close()
    try:
        await bot.send_message(o["user_id"], f"❌ Ваш заказ «{o['product']}» отклонён администратором. Деньги возвращены.")
    except Exception:
        pass
    return True, "Заказ отклонён, деньги возвращены."


def order_summary_kb(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Взять в работу ВСЕ ({count} шт.)", callback_data="order_take_all")
    ]])


def order_item_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"order_take_{order_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"order_decline_{order_id}"),
    ]])


@dp.message(Command("list"))
@dp.message(F.text.lower() == "список")
async def cmd_list_orders(message: Message):
    u = message.from_user
    if not is_admin(u.id):
        return
    orders = get_pending_orders()
    if not orders:
        await reply_or_send(message, "📋 Активных заказов нет.")
        return
    await reply_or_send(message, "📋 Список активных заказов:", reply_markup=order_summary_kb(len(orders)))
    for o in orders:
        await reply_or_send(message, 
            f"• Заказ #{o['order_id']} | {o['product']} | от {o['display_name']}",
            reply_markup=order_item_kb(o["order_id"]),
        )


@dp.callback_query(F.data.startswith("order_take_"))
async def cb_order_take(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    rest = callback.data[len("order_take_"):]
    if rest == "all":
        orders = get_pending_orders()
        done = 0
        for o in orders:
            ok, _ = await fulfill_order(o["order_id"], callback.from_user.id)
            if ok:
                done += 1
        await reply_or_send(callback.message, f"🔄 Результат пакетной обработки:\n\n✅ Успешно выдано: {done} шт.")
        await callback.answer()
        return
    order_id = int(rest)
    ok, text = await fulfill_order(order_id, callback.from_user.id)
    if not ok:
        await callback.answer(text, show_alert=True)
        return
    await callback.message.edit_text(f"✅ Заказ #{order_id} успешно выполнен и закрыт.")
    await callback.answer()


@dp.callback_query(F.data.startswith("order_decline_"))
async def cb_order_decline(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    order_id = int(callback.data[len("order_decline_"):])
    ok, text = await decline_order(order_id, callback.from_user.id)
    if not ok:
        await callback.answer(text, show_alert=True)
        return
    await callback.message.edit_text(f"❌ Заказ #{order_id} отклонён.")
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_skin_"))
async def cb_buy_skin(callback: CallbackQuery):
    # скины покупаются один раз навсегда; уже купленный скин просто надевается бесплатно.
    # По запросу: теперь бывают и кастомные скины (не из SKINS, а зарегистрированные
    # через "!скин_стикер") — их нельзя купить, только получить от админа/из ларца,
    # поэтому сначала проверяем владение и только потом ищем цену.
    skin_name = callback.data[len("buy_skin_"):]
    if not skin_exists(skin_name):
        await callback.answer("Скин не найден", show_alert=True)
        return
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pet = get_pet(u.id)
    if pet is None or not pet["alive"]:
        await callback.answer("Сначала приручите волка", show_alert=True)
        return

    if owns_skin(u.id, skin_name):
        update_pet(u.id, skin=skin_name)
        await callback.answer(f"🐺 Скин «{skin_name}» надет!", show_alert=True)
        # кнопка и меню скинов сразу обновляются на новый надетый скин
        await safe_edit_or_send(
            callback.message,
            f"🎭 Скины питомца (сейчас: «{skin_name}»):",
            pet_skins_kb(u.id, skin_name),
        owner_id=callback.from_user.id)
        return

    price = SKINS.get(skin_name)
    if price is None:
        # кастомный скин, которым игрок не владеет — купить в магазине нельзя
        await callback.answer("Этот скин можно получить только от администратора или из ларца.", show_alert=True)
        return

    if user["dollars"] < price:
        await callback.answer("Недостаточно долларов 💵", show_alert=True)
        return
    update_user(u.id, dollars=user["dollars"] - price)
    grant_skin_ownership(u.id, skin_name)
    update_pet(u.id, skin=skin_name)
    await callback.answer(f"✅ Скин «{skin_name}» куплен навсегда и надет!", show_alert=True)
    await safe_edit_or_send(
        callback.message,
        f"🎭 Скины питомца (сейчас: «{skin_name}»):",
        pet_skins_kb(u.id, skin_name),
    owner_id=callback.from_user.id)


@dp.callback_query(F.data == "buy_ring")
async def cb_buy_ring(callback: CallbackQuery):
    u = callback.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    if user["dollars"] < RING_COST_DOLLARS:
        await callback.answer("Недостаточно долларов 💵", show_alert=True)
        return
    update_user(u.id, dollars=user["dollars"] - RING_COST_DOLLARS, rings=user["rings"] + 1)
    await callback.answer("💍 Кольцо куплено!", show_alert=True)



# =========================================================
#  ФОНОВЫЕ ЗАДАЧИ
# =========================================================

async def background_loop():
    while True:
        await asyncio.sleep(30)
        await resolve_finished_activities()
        await resolve_stale_activity_games()
        await resolve_finished_campfires()
        await check_starvation_deaths()


async def resolve_finished_activities():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM pets WHERE busy_until IS NOT NULL AND alive=1"
    ).fetchall()
    conn.close()

    now = datetime.utcnow()
    for pet in rows:
        until = datetime.fromisoformat(pet["busy_until"])
        if now < until:
            continue
        if pet["busy_activity"] == "бой":
            # только запись атакующего запускает разрешение боя,
            # чтобы не обработать одну и ту же схватку дважды
            if pet["fight_is_attacker"]:
                await resolve_wolf_fight(pet)
            continue
        if pet["busy_activity"] == FIGHT_CHALLENGE_ACTIVITY:
            # вызов на бой не был принят вовремя — сгорает (обрабатываем один раз,
            # тоже по записи атакующего)
            if pet["fight_is_attacker"]:
                await resolve_fight_challenge_timeout(pet)
            continue
        await resolve_activity(pet)


async def resolve_activity(pet: sqlite3.Row):
    user_id = pet["user_id"]
    activity = pet["busy_activity"] or "лес"
    satiety = pet["satiety"]

    if activity == "охота":
        # добычи стало меньше
        raw_meat_gain = random.randint(1, 2)
        bones_gain = random.randint(0, 2)
        firewood_gain = 0
        xp_gain = random.randint(15, 40)
    else:
        # добычи стало меньше
        firewood_gain = round(random.uniform(0.5, 2), 2)
        bones_gain = random.randint(0, 1)
        # по запросу: с шансом 30% поход в лес тоже может принести немного мяса
        raw_meat_gain = random.randint(1, 2) if random.random() < FOREST_MEAT_CHANCE else 0
        xp_gain = random.randint(5, 20)
    # по запросу: доллары больше не даются за лес/охоту — только за бонус,
    # лабиринт и немного за квесты
    dollars_gain = 0

    if pet["joint_activity_bonus"]:
        # /совместный поход с супругом даёт бонус опыта питомцу и добычи
        xp_gain = int(xp_gain * (1 + JOINT_ACTIVITY_BONUS_XP_PERCENT / 100))
        loot_mult = 1 + JOINT_ACTIVITY_LOOT_BONUS_PERCENT / 100
        raw_meat_gain = round(raw_meat_gain * loot_mult)
        bones_gain = round(bones_gain * loot_mult)
        firewood_gain = round(firewood_gain * loot_mult, 2)

    # зелье добычи — активируется заранее, расходуется при возвращении из похода
    used_loot_potion = bool(pet["pending_potion_loot"])
    if used_loot_potion:
        loot_mult = 1 + POTION_LOOT_BONUS_PERCENT / 100
        raw_meat_gain = round(raw_meat_gain * loot_mult)
        bones_gain = round(bones_gain * loot_mult)
        firewood_gain = round(firewood_gain * loot_mult, 2)

    # по запросу: при низкой сытости (<30%) волк приносит на 70% меньше дров/мяса
    low_satiety = satiety < LOW_SATIETY_THRESHOLD
    if low_satiety:
        penalty_mult = 1 - LOW_SATIETY_PENALTY_PERCENT / 100
        raw_meat_gain = round(raw_meat_gain * penalty_mult)
        firewood_gain = round(firewood_gain * penalty_mult, 2)

    # по запросу: похудение увеличено и теперь разное для леса/охоты
    # (охота тяжелее физически — теряется больше веса)
    if activity == "охота":
        weight_loss = round(random.uniform(HUNT_WEIGHT_LOSS_MIN, HUNT_WEIGHT_LOSS_MAX), 2)
    else:
        weight_loss = round(random.uniform(FOREST_WEIGHT_LOSS_MIN, FOREST_WEIGHT_LOSS_MAX), 2)
    new_satiety = max(0, satiety - random.randint(5, 15))

    ambushed = random.random() < AMBUSH_CHANCE
    died = False
    ambush_msg = ""
    bones_spent = 0

    if ambushed:
        if pet["bones"] >= AMBUSH_BONES_COST:
            bones_spent = AMBUSH_BONES_COST
            # отбились, но и это стоило сил — доп. потеря веса
            weight_loss = round(weight_loss + AMBUSH_EXTRA_WEIGHT_LOSS_DEFENDED, 2)
            ambush_msg = (
                f"\n\n🐺 На обратном пути на вас напала стая! "
                f"Потратив {AMBUSH_BONES_COST} 🦴, ваш питомец смог отбиться и сохранил всю добычу "
                f"(но потратил на это чуть больше сил — вес -{AMBUSH_EXTRA_WEIGHT_LOSS_DEFENDED} кг доп.)."
            )
        else:
            raw_meat_gain = raw_meat_gain // 2
            firewood_gain = round(firewood_gain / 2, 2)
            new_satiety = max(0, new_satiety - 10)
            # убегал в панике — потерял ещё больше веса
            weight_loss = round(weight_loss + AMBUSH_EXTRA_WEIGHT_LOSS_FLED, 2)
            ambush_msg = (
                "\n\n🐺 На обратном пути на вас напала стая! У питомца не хватило косточек, "
                "чтобы отбиться — часть добычи потеряна, сытость упала сильнее, "
                f"а паническое бегство отняло ещё -{AMBUSH_EXTRA_WEIGHT_LOSS_FLED} кг веса."
            )
            if random.random() < AMBUSH_DEATH_CHANCE_IF_FAIL:
                died = True

    if died:
        update_pet(user_id, alive=0, busy_until=None, busy_activity=None, joint_activity_bonus=0,
                   pending_potion_loot=0)
        try:
            sent = await bot.send_message(
                user_id,
                f"💔 Ваш питомец «{pet['name']}» погиб в стычке со стаей.{ambush_msg}\n\n"
                f"Вы можете воскресить его за {REVIVE_COST_RUNES} 🀄 рун.",
                reply_markup=revive_kb(),
            )
            remember_owner(sent.chat.id, sent.message_id, user_id)
        except Exception as e:
            log.warning("Не удалось уведомить пользователя %s: %s", user_id, e)
        return

    # По запросу: мини-игра "чей след?" — вместо немедленного начисления добычи
    # игроку предлагается угадать след на обратном пути. Угадал — бонус к добыче/опыту
    # и меньшая потеря веса; результат "заморожен" в game_payload до ответа или таймаута.
    payload = {
        "activity": activity,
        "raw_meat_gain": raw_meat_gain,
        "firewood_gain": firewood_gain,
        "bones_gain": bones_gain,
        "xp_gain": xp_gain,
        "dollars_gain": dollars_gain,
        "weight_loss": weight_loss,
        "new_satiety": new_satiety,
        "used_loot_potion": used_loot_potion,
        "low_satiety": low_satiety,
        "ambush_msg": ambush_msg,
        "bones_spent": bones_spent,
        "pet_name": pet["name"],
    }
    correct = random.choice(list(ANIMAL_TRACK_LABELS.keys()))
    now_iso = datetime.utcnow().isoformat()
    update_pet(
        user_id,
        busy_until=None,
        busy_activity=None,
        joint_activity_bonus=0,
        game_pending=1,
        game_correct=correct,
        game_payload=json.dumps(payload, ensure_ascii=False),
        game_offered_at=now_iso,
    )
    label = "с охоты" if activity == "охота" else "из леса"
    try:
        sent = await bot.send_message(
            user_id,
            f"🐺 «{pet['name']}» возвращается {label} и остановился у следов на земле...\n"
            "Чьи это следы? Угадаете — получите бонус к добыче, опыту и меньше устанете:",
            reply_markup=activity_game_kb(user_id),
        )
        remember_owner(sent.chat.id, sent.message_id, user_id)
    except Exception as e:
        log.warning("Не удалось отправить мини-игру пользователю %s: %s", user_id, e)


def activity_game_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"actgame_{key}_{user_id}")
        for key, label in ANIMAL_TRACK_LABELS.items()
    ]])


async def _finalize_activity_result(pet: sqlite3.Row, won: bool | None, note_extra: str = ""):
    """Начисляет добычу/опыт/вес по замороженному payload мини-игры "чей след?".
    won=True — угадал (бонус), won=False — не угадал (без бонуса),
    won=None — не ответил вовремя, результат забирается автоматически."""
    user_id = pet["user_id"]
    payload = json.loads(pet["game_payload"])
    raw_meat_gain = payload["raw_meat_gain"]
    firewood_gain = payload["firewood_gain"]
    bones_gain = payload["bones_gain"]
    xp_gain = payload["xp_gain"]
    dollars_gain = payload["dollars_gain"]
    weight_loss = payload["weight_loss"]
    new_satiety = payload["new_satiety"]
    used_loot_potion = payload["used_loot_potion"]
    low_satiety = payload["low_satiety"]
    ambush_msg = payload["ambush_msg"]
    bones_spent = payload["bones_spent"]
    activity = payload["activity"]

    game_note = ""
    if won:
        bonus_mult = 1 + GAME_WIN_LOOT_BONUS_PERCENT / 100
        raw_meat_gain = round(raw_meat_gain * bonus_mult)
        bones_gain = round(bones_gain * bonus_mult)
        firewood_gain = round(firewood_gain * bonus_mult, 2)
        xp_gain = int(xp_gain * bonus_mult)
        weight_loss = round(weight_loss * GAME_WIN_WEIGHT_LOSS_MULT, 2)
        game_note = f"\n🎯 След угадан верно! +{GAME_WIN_LOOT_BONUS_PERCENT}% к добыче и опыту, меньше усталости."
    elif won is False:
        game_note = "\n❌ След угадан неверно — бонуса нет, но добыча остаётся вашей."
    else:
        game_note = "\n⏰ Вы не успели ответить — результат забран автоматически, без бонуса."

    grant_pet_xp(pet, xp_gain)
    update_pet(
        user_id,
        firewood=pet["firewood"] + firewood_gain,
        raw_meat=pet["raw_meat"] + raw_meat_gain,
        bones=pet["bones"] - bones_spent + bones_gain,
        weight=max(1.0, pet["weight"] - weight_loss),
        satiety=new_satiety,
        game_pending=0,
        game_correct=None,
        game_payload=None,
        game_offered_at=None,
        pending_potion_loot=0,
    )
    if bones_gain:
        record_quest_progress(user_id, "collect_bones_5", bones_gain)
    if dollars_gain:
        owner = get_or_create_user(user_id, "", "")
        update_user(user_id, dollars=owner["dollars"] + dollars_gain)

    label = "с охоты" if activity == "охота" else "из леса"
    gain_lines = []
    if raw_meat_gain:
        gain_lines.append(f"Добыто плоти: {raw_meat_gain} 🥩")
    if firewood_gain:
        gain_lines.append(f"Добыто дров: {firewood_gain:.2f} 🪵")
    if bones_gain:
        gain_lines.append(f"Найдено косточек: {bones_gain} 🦴")
    gain_lines.append(f"Опыт питомца: +{xp_gain} ✨")
    if dollars_gain:
        gain_lines.append(f"Заработано долларов: {dollars_gain} 💵")
    if used_loot_potion:
        gain_lines.append(f"🧪🎒 Зелье добычи сработало (+{POTION_LOOT_BONUS_PERCENT}%)")
    if low_satiety:
        gain_lines.append(f"⚠️ Низкая сытость — добыча снижена на {LOW_SATIETY_PENALTY_PERCENT}%")
    # по запросу: теперь явно показываем, сколько веса потерял питомец за поход
    gain_lines.append(f"Потеря веса: -{weight_loss:.2f} кг ⚖️")

    try:
        await bot.send_message(
            user_id,
            f"🐺 Ваш «{payload['pet_name']}» вернулся {label}!\n"
            + "\n".join(gain_lines) + ambush_msg + game_note,
        )
    except Exception as e:
        log.warning("Не удалось уведомить пользователя %s: %s", user_id, e)


@dp.callback_query(F.data.startswith("actgame_"))
async def cb_activity_game_answer(callback: CallbackQuery):
    rest = callback.data[len("actgame_"):]
    choice, user_id_str = rest.rsplit("_", 1)
    user_id = int(user_id_str)
    if callback.from_user.id != user_id:
        # двойная защита — то же самое уже проверяет owner_guard_middleware
        await callback.answer("Это не ваша мини-игра!", show_alert=True)
        return
    pet = get_pet(user_id)
    if pet is None or not pet["alive"] or not pet["game_pending"]:
        await callback.answer("Эта мини-игра уже неактуальна.", show_alert=True)
        return
    won = choice == pet["game_correct"]
    correct_label = ANIMAL_TRACK_LABELS.get(pet["game_correct"], pet["game_correct"])
    await _finalize_activity_result(pet, won=won)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("🎯 Верно!" if won else f"❌ Неверно, это был след: {correct_label}", show_alert=True)


async def resolve_stale_activity_games():
    """Если игрок не ответил на мини-игру "чей след?" за GAME_TIMEOUT_SECONDS —
    результат похода забирается автоматически, без бонуса за угадывание."""
    conn = db()
    rows = conn.execute("SELECT * FROM pets WHERE game_pending=1 AND alive=1").fetchall()
    conn.close()

    now = datetime.utcnow()
    for pet in rows:
        offered_at = pet["game_offered_at"]
        if not offered_at:
            continue
        if (now - datetime.fromisoformat(offered_at)).total_seconds() < GAME_TIMEOUT_SECONDS:
            continue
        await _finalize_activity_result(pet, won=None)


async def resolve_finished_campfires():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM pets WHERE cooking_until IS NOT NULL"
    ).fetchall()
    conn.close()

    now = datetime.utcnow()
    for pet in rows:
        until = datetime.fromisoformat(pet["cooking_until"])
        if now < until:
            continue
        output = random.randint(CAMPFIRE_MEAT_OUTPUT_MIN, CAMPFIRE_MEAT_OUTPUT_MAX)
        update_pet(pet["user_id"], cooked_meat=pet["cooked_meat"] + output, cooking_until=None)
        record_quest_progress(pet["user_id"], "cook_meat", output)
        try:
            await bot.send_message(
                pet["user_id"],
                f"🍖 Мясо на костре готово! Получено {output} порций готового мяса.",
            )
        except Exception as e:
            log.warning("Не удалось уведомить пользователя %s: %s", pet["user_id"], e)


async def check_starvation_deaths():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM pets WHERE alive=1 AND satiety<=0"
    ).fetchall()
    conn.close()

    now = datetime.utcnow()
    for pet in rows:
        if not pet["starved_since"]:
            update_pet(pet["user_id"], starved_since=now.isoformat())
            continue
        started = datetime.fromisoformat(pet["starved_since"])
        grace = timedelta(seconds=random.randint(STARVE_GRACE_MIN_SEC, STARVE_GRACE_MAX_SEC))
        if now - started < grace:
            continue
        update_pet(pet["user_id"], alive=0, busy_until=None, busy_activity=None)
        try:
            sent = await bot.send_message(
                pet["user_id"],
                f"Ваш питомец «{pet['name']}» погиб от голода... 💔\n"
                f"Желаете воскресить его?\nСтоимость: {REVIVE_COST_RUNES} рун 🀄",
                reply_markup=revive_kb(),
            )
            remember_owner(sent.chat.id, sent.message_id, pet["user_id"])
        except Exception as e:
            log.warning("Не удалось уведомить пользователя %s: %s", pet["user_id"], e)


# =========================================================
#  ОБРАБОТКА ОЖИДАЕМОГО ТЕКСТА (имя питомца) — ДОЛЖНА ИДТИ ПОСЛЕДНЕЙ!
# =========================================================
# Важно: этот хендлер ловит ЛЮБОЙ обычный текст (не начинающийся с "/" или "!"),
# поэтому он обязан быть зарегистрирован ПОСЛЕ всех остальных текстовых команд
# (лес/охота/арена/зелья/костёр/магазин и т.п.) — иначе aiogram отдаёт ему
# сообщение первым и до конкретных команд дело просто не доходит (баг ).

@dp.message(F.text & ~F.text.startswith("/") & ~F.text.startswith("!"))
async def handle_pending_text(message: Message):
    """Ловит имя питомца, если пользователь в состоянии 'tame' или 'rename'.
    Если пользователь ни в каком ожидании не находится — просто ничего не делает
    (остальные точные текстовые команды уже перехвачены хендлерами выше)."""
    u = message.from_user
    user = get_or_create_user(u.id, u.username or "", u.full_name)
    pending = user["pending_action"]
    if not pending:
        return

    # имя питомца засчитывается только реплаем на сообщение-запрос,
    # чтобы случайные сообщения в чате не улетали в качестве имени
    pending_msg_id = user["pending_msg_id"]
    if not message.reply_to_message or (
        pending_msg_id and message.reply_to_message.message_id != pending_msg_id
    ):
        # БАГФИКС (флуд): раньше это напоминание срабатывало на КАЖДОЕ обычное
        # сообщение пользователя в чате, пока он не назовёт питомца — в группе
        # это превращалось в спам при любой не относящейся к делу переписке.
        # Теперь: 1) в группах вообще молчим на посторонние сообщения (инструкция
        # уже была в исходном запросе на имя), 2) даже в личке — не чаще
        # раза в PENDING_NUDGE_COOLDOWN.
        if message.chat.type != "private":
            return
        last_nudge = _last_pending_nudge.get(u.id)
        now = datetime.utcnow()
        if last_nudge and (now - last_nudge) < PENDING_NUDGE_COOLDOWN:
            return
        _last_pending_nudge[u.id] = now
        await reply_or_send(message, "Пожалуйста, ответьте (реплаем) на сообщение с запросом имени.")
        return

    name = message.text.strip()[:24] if message.text else ""
    if not name:
        await reply_or_send(message, "Имя не может быть пустым. Попробуйте ещё раз.")
        return

    if pending == "tame":
        create_new_pet(u.id, name)
        update_user(u.id, pending_action=None, pending_msg_id=None)
        await reply_or_send(message, f"🐺 Питомец «{name}» успешно приручён!")
        await send_pet_screen(message.chat.id, u.id)

    elif pending == "rename":
        fresh_user = get_or_create_user(u.id, u.username or "", u.full_name)
        if fresh_user["runes"] < RENAME_COST_RUNES:
            update_user(u.id, pending_action=None, pending_msg_id=None)
            await reply_or_send(message, "Недостаточно рун, переименование отменено.")
            return
        update_user(u.id, runes=fresh_user["runes"] - RENAME_COST_RUNES, pending_action=None, pending_msg_id=None)
        update_pet(u.id, name=name)
        await reply_or_send(message, f"✅ Питомца теперь зовут «{name}»")


# =========================================================
#  ЗАПУСК
# =========================================================

@dp.errors()
async def global_error_handler(event):
    """Ловит ЛЮБОЕ необработанное исключение в хендлерах и печатает полный
    traceback в лог. Раньше такого перехватчика не было — если хендлер падал
    с исключением, которое aiogram по каким-то причинам не логировал сам,
    бот просто "молчал" на сообщение без единой строки в логе, и понять,
    что вообще пошло не так, было невозможно."""
    log.error(
        "Необработанная ошибка при обработке апдейта %s: %s",
        event.update.update_id if event.update else "?",
        event.exception,
        exc_info=True,
    )
    return True


async def main():
    init_db()
    asyncio.create_task(background_loop())
    log.info("Бот запущен, начинаем polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
