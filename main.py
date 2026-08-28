import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, 
    InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# ==========================================
# --- НАСТРОЙКИ ---
# ==========================================
TOKEN = "8867703303:AAFCLl8XjoIc9iHG3mgJysTTFdEsEPV9QjE"
ADMIN_ID = 6362369788  # ВАШ ID АДМИНА

DB_PATH = "bot_database.db"
MAX_BIO_LENGTH = 700

router = Router()

async def background_send(bot: Bot, user_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception:
        pass

# --- Инициализация Базы Данных ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                bio TEXT,
                photo_ids TEXT,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                reports INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0,
                username TEXT,
                first_name TEXT,
                waiting_for_new INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS views (
                viewer_id INTEGER,
                target_id INTEGER,
                PRIMARY KEY (viewer_id, target_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS all_time_views (
                viewer_id INTEGER,
                target_id INTEGER,
                PRIMARY KEY (viewer_id, target_id)
            )
        """)
        await db.execute("INSERT OR IGNORE INTO all_time_views SELECT viewer_id, target_id FROM views")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                liker_id INTEGER,
                target_id INTEGER,
                PRIMARY KEY (liker_id, target_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reputation_votes (
                voter_id INTEGER,
                target_id INTEGER,
                PRIMARY KEY (voter_id, target_id)
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_views_viewer ON views(viewer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_matches_target ON matches(target_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(banned)")
        await db.commit()

# --- Работа с БД ---
async def save_profile(user_id: int, bio: str, photo_ids: list, username: str, first_name: str):
    photo_str = ",".join(photo_ids)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, bio, photo_ids, username, first_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                bio = excluded.bio,
                photo_ids = excluded.photo_ids,
                username = excluded.username,
                first_name = excluded.first_name,
                banned = 0,
                reports = 0
        """, (user_id, bio, photo_str, username, first_name))
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_next_profile(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.*, 
                   EXISTS(SELECT 1 FROM all_time_views v2 WHERE v2.viewer_id = ? AND v2.target_id = u.user_id) as is_rewatch
            FROM users u 
            WHERE u.user_id != ? 
              AND u.banned = 0 
              AND NOT EXISTS (
                  SELECT 1 FROM views v WHERE v.viewer_id = ? AND v.target_id = u.user_id
              )
            ORDER BY RANDOM() LIMIT 1
        """, (user_id, user_id, user_id)) as cursor:
            return await cursor.fetchone()

async def get_liker_profile(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.*,
                   EXISTS(SELECT 1 FROM all_time_views v2 WHERE v2.viewer_id = ? AND v2.target_id = u.user_id) as is_rewatch
            FROM users u
            JOIN matches m ON u.user_id = m.liker_id
            WHERE m.target_id = ?
              AND u.banned = 0
              AND NOT EXISTS (
                  SELECT 1 FROM views v WHERE v.viewer_id = ? AND v.target_id = u.user_id
              )
            LIMIT 1
        """, (user_id, user_id, user_id)) as cursor:
            return await cursor.fetchone()

async def get_reputation_tops():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT *, (likes - dislikes) as rep FROM users 
            WHERE banned = 0 
            ORDER BY rep DESC, likes DESC 
            LIMIT 4
        """) as cursor:
            top_good = await cursor.fetchall()

        async with db.execute("""
            SELECT *, (likes - dislikes) as rep FROM users 
            WHERE banned = 0 
            ORDER BY rep ASC, dislikes DESC 
            LIMIT 4
        """) as cursor:
            top_bad = await cursor.fetchall()
            
        return top_good, top_bad

async def reset_views(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM views WHERE viewer_id = ?", (user_id,))
        await db.commit()

# --- Реакции ---
async def process_match_action(viewer_id: int, target_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.execute("INSERT OR IGNORE INTO all_time_views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        
        cursor = await db.execute("INSERT OR IGNORE INTO matches (liker_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        is_new_like = cursor.rowcount > 0
        
        async with db.execute("SELECT 1 FROM matches WHERE liker_id = ? AND target_id = ?", (target_id, viewer_id)) as match_cursor:
            is_match = await match_cursor.fetchone() is not None
            
        await db.commit()
        return is_match, is_new_like

async def process_rep_action(viewer_id: int, target_id: int, is_plus: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.execute("INSERT OR IGNORE INTO all_time_views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        
        cursor = await db.execute("INSERT OR IGNORE INTO reputation_votes (voter_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        if cursor.rowcount == 0:
            await db.commit()
            return False
            
        if is_plus:
            await db.execute("UPDATE users SET likes = likes + 1 WHERE user_id = ?", (target_id,))
        else:
            await db.execute("UPDATE users SET dislikes = dislikes + 1 WHERE user_id = ?", (target_id,))
        await db.commit()
        return True

async def process_skip_action(viewer_id: int, target_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.execute("INSERT OR IGNORE INTO all_time_views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.commit()

async def process_report_action(viewer_id: int, target_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.execute("INSERT OR IGNORE INTO all_time_views (viewer_id, target_id) VALUES (?, ?)", (viewer_id, target_id))
        await db.execute("UPDATE users SET reports = reports + 1 WHERE user_id = ?", (target_id,))
        await db.commit()

# --- Уведомления ---
async def set_waiting_for_new(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET waiting_for_new = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def check_and_notify_waiting_users(bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE waiting_for_new = 1") as cursor:
            waiting_users = await cursor.fetchall()
            
        for u in waiting_users:
            uid = u["user_id"]
            async with db.execute("""
                SELECT COUNT(*) as cnt FROM users u2 
                WHERE u2.user_id != ? AND u2.banned = 0 
                  AND NOT EXISTS (SELECT 1 FROM views v WHERE v.viewer_id = ? AND v.target_id = u2.user_id)
            """, (uid, uid)) as count_cursor:
                res = await count_cursor.fetchone()
                
            if res and res["cnt"] >= 3:
                asyncio.create_task(background_send(bot, uid, "🔔 Появилось много новых анкет! Жми «🔍 Искать анкеты»."))
                await db.execute("UPDATE users SET waiting_for_new = 0 WHERE user_id = ?", (uid,))
        await db.commit()

def get_match_button(user_data):
    if user_data['username']:
        url = f"https://t.me/{user_data['username']}"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💬 Написать {user_data['first_name']}", url=url)]
        ])
    else:
        return None

def get_admin_ban_button(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Забанить пользователя", callback_data=f"admin_ban_anon_{user_id}")]
    ])

# ==========================================
# --- КЛАВИАТУРЫ ---
# ==========================================
def get_main_keyboard(user_id: int):
    b = ReplyKeyboardBuilder()
    b.button(text="🔍 Искать анкеты")
    b.button(text="👀 Кто лайкнул")
    b.button(text="👁️ Посмотреть мою анкету")
    b.button(text="📈 Топ по репутации")
    b.button(text="✏️ Изменить")
    b.button(text="💬 Связь с админом")
    if user_id == ADMIN_ID:
        b.button(text="👑 Админ-панель")
    b.adjust(2, 2, 2, 1 if user_id != ADMIN_ID else 2)
    return b.as_markup(resize_keyboard=True)

def get_search_keyboard(is_rewatch: bool = False):
    b = ReplyKeyboardBuilder()
    b.button(text="❤️ Лайк")
    b.button(text="⏭ Пропустить")
    if not is_rewatch:
        b.button(text="👍 Реп+")
        b.button(text="👎 Реп-")
    b.button(text="🚨 Жалоба")
    b.button(text="🛑 Главное меню")
    
    if is_rewatch:
        b.adjust(2, 2)
    else:
        b.adjust(2, 2, 2)
    return b.as_markup(resize_keyboard=True)

def get_empty_search_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="🔄 Смотреть заново")
    b.button(text="🛑 Главное меню")
    b.adjust(1, 1)
    return b.as_markup(resize_keyboard=True)

def get_photo_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="✅ Готово")
    b.button(text="💬 Связь с админом")
    b.adjust(1, 1)
    return b.as_markup(resize_keyboard=True)

def get_start_registration_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📝 Зарегистрировать анкету")
    b.button(text="💬 Связь с админом")
    b.adjust(1, 1)
    return b.as_markup(resize_keyboard=True)

def get_admin_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📊 Статистика")
    b.button(text="🔎 Поиск анкеты")
    b.button(text="📢 Рассылка")
    b.button(text="🚨 Жалобы")
    b.button(text="📋 Забаненные")
    b.button(text="🔓 Разбанить")
    b.button(text="🛑 Главное меню")
    b.adjust(2, 1, 2, 1, 1)
    return b.as_markup(resize_keyboard=True)

def get_admin_report_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="⛔ Забанить")
    b.button(text="✅ Отклонить/Пропустить")
    b.button(text="🛑 Главное меню")
    b.adjust(2, 1)
    return b.as_markup(resize_keyboard=True)

# ==========================================
# --- СОСТОЯНИЯ (FSM) ---
# ==========================================
class Registration(StatesGroup):
    waiting_for_bio = State()
    waiting_for_photo = State()

class AdminContactStates(StatesGroup):
    waiting_for_message = State()

class SearchStates(StatesGroup):
    viewing = State()

class AdminStates(StatesGroup):
    viewing_reports = State()
    waiting_for_unban_id = State()
    waiting_for_search_id = State()
    waiting_for_broadcast_msg = State()

# ==========================================
# --- 1. РЕГИСТРАЦИЯ И ГЛАВНОЕ МЕНЮ ---
# ==========================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user and user["banned"]:
        await message.answer("Вы заблокированы.", reply_markup=ReplyKeyboardRemove())
        return

    if user:
        await message.answer(
            "Вы уже зарегистрированы! Выберите действие в меню 👇",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer(
            "Привет! Добро пожаловать. Нажмите кнопку ниже, чтобы начать заполнение анкеты 👇",
            reply_markup=get_start_registration_keyboard()
        )

@router.message(F.text == "📝 Зарегистрировать анкету")
async def start_registration_button(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user and user["banned"]:
        await message.answer("Вы заблокированы.", reply_markup=ReplyKeyboardRemove())
        return

    await message.answer(
        f"Напиши о себе (максимум {MAX_BIO_LENGTH} символов):", 
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Registration.waiting_for_bio)

@router.message(F.text == "✏️ Изменить")
async def edit_profile(message: Message, state: FSMContext):
    await message.answer("Напиши новый текст о себе:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_bio)

@router.message(Registration.waiting_for_bio, F.text)
async def process_bio(message: Message, state: FSMContext):
    if len(message.text) > MAX_BIO_LENGTH:
        await message.answer("Текст слишком длинный! Попробуй еще раз.")
        return
    await state.update_data(bio=message.text, photos=[])
    await message.answer(
        "Отлично! Теперь отправь фото (до 3 штук).\n\n"
        "Как отправишь все нужные, нажми «✅ Готово» внизу.",
        reply_markup=get_photo_keyboard()
    )
    await state.set_state(Registration.waiting_for_photo)

@router.message(Registration.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(message.photo[-1].file_id)
    
    if len(photos) >= 3:
        await finish_registration(message, state, photos)
    else:
        await state.update_data(photos=photos)
        await message.answer(
            f"📸 Фото {len(photos)}/3 загружено. Отправь еще или нажми «✅ Готово».",
            reply_markup=get_photo_keyboard()
        )

@router.message(Registration.waiting_for_photo, F.text == "✅ Готово")
async def process_photo_done(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("Сначала отправь хотя бы одно фото!", reply_markup=get_photo_keyboard())
        return
    await finish_registration(message, state, photos)

async def finish_registration(message: Message, state: FSMContext, photos: list):
    data = await state.get_data()
    await save_profile(
        user_id=message.from_user.id,
        bio=data["bio"],
        photo_ids=photos,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    
    await message.answer(
        "Анкета сохранена! Выбери действие в меню 👇", 
        reply_markup=get_main_keyboard(message.from_user.id)
    )
    await state.clear()
    asyncio.create_task(check_and_notify_waiting_users(message.bot))

@router.message(F.text == "👁️ Посмотреть мою анкету")
async def view_my_profile(message: Message):
    user_data = await get_user(message.from_user.id)
    if not user_data:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_start_registration_keyboard())
        return
        
    photos = user_data["photo_ids"].split(",")
    total_reputation = user_data['likes'] - user_data['dislikes']
    
    caption = (
        f"Твоя анкета:\n\n{user_data['bio']}\n\n"
        f"🌟 Репутация: {total_reputation}"
    )
    
    if len(photos) == 1:
        await message.answer_photo(photos[0], caption=caption, reply_markup=get_main_keyboard(message.from_user.id))
    else:
        media = [InputMediaPhoto(media=photos[0], caption=caption)] + [InputMediaPhoto(media=p) for p in photos[1:]]
        await message.answer_media_group(media)
        await message.answer("🏠 Вы в главном меню.", reply_markup=get_main_keyboard(message.from_user.id))

@router.message(F.text == "📈 Топ по репутации")
async def show_reputation_tops(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_start_registration_keyboard())
        return
    if user["banned"]: return

    top_good, top_bad = await get_reputation_tops()

    text = "📈 <b>Топ высокой репутации:</b>\n"
    if top_good:
        for idx, u in enumerate(top_good, 1):
            short_bio = u['bio'][:35] + ("..." if len(u['bio']) > 35 else "")
            text += f"{idx}. {short_bio} — 🌟 {u['rep']}\n"
    else:
        text += "Пока пусто.\n"

    text += "\n📉 <b>Топ низкой репутации:</b>\n"
    if top_bad:
        for idx, u in enumerate(top_bad, 1):
            short_bio = u['bio'][:35] + ("..." if len(u['bio']) > 35 else "")
            text += f"{idx}. {short_bio} — 🌟 {u['rep']}\n"
    else:
        text += "Пока пусто.\n"

    builder = InlineKeyboardBuilder()
    if top_good:
        builder.row(InlineKeyboardButton(text="Анкета с самой высокой репутацией", callback_data=f"show_top_profile_{top_good[0]['user_id']}"))
    if top_bad:
        builder.row(InlineKeyboardButton(text="Анкета с самой низкой репутацией", callback_data=f"show_top_profile_{top_bad[0]['user_id']}"))

    await message.answer(text, reply_markup=builder.as_markup())
    await message.answer("🏠 Вы в главном меню.", reply_markup=get_main_keyboard(message.from_user.id))

@router.callback_query(F.data.startswith("show_top_profile_"))
async def show_top_profile_callback(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[3])
    target_data = await get_user(target_id)
    
    if not target_data or target_data["banned"]:
        await callback.answer("Эта анкета больше недоступна.", show_alert=True)
        return

    total_reputation = target_data['likes'] - target_data['dislikes']
    caption = f"👤 <b>Анкета из топа:</b>\n\n{target_data['bio']}\n\n🌟 Репутация: {total_reputation}"
    photos = target_data["photo_ids"].split(",")
    
    if len(photos) == 1:
        await callback.message.answer_photo(photo=photos[0], caption=caption)
    else:
        media = [InputMediaPhoto(media=photos[0], caption=caption)] + [InputMediaPhoto(media=p) for p in photos[1:]]
        await callback.message.answer_media_group(media)
        
    await callback.answer()

@router.message(F.text == "💬 Связь с админом")
async def ask_admin_start(message: Message, state: FSMContext):
    current_state = await state.get_state()
    source = "из меню регистрации" if current_state and current_state.startswith("Registration:") else "из главного меню"
    
    await state.update_data(admin_msg_source=source)
    await message.answer(
        "💬 Отправьте текст, фото, видео или голосовое сообщение администратору:",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminContactStates.waiting_for_message)

@router.message(AdminContactStates.waiting_for_message)
async def process_admin_message(message: Message, state: FSMContext):
    data = await state.get_data()
    source = data.get("admin_msg_source", "из главного меню")
    
    user = message.from_user
    username_str = f"@{user.username}" if user.username else "Нет username"
    
    try:
        if message.text:
            admin_text = (
                f"📩 <b>Сообщение от пользователя ({source})!</b>\n\n"
                f"<b>Имя:</b> {user.first_name}\n"
                f"<b>ID:</b> <code>{user.id}</code>\n"
                f"<b>Username:</b> {username_str}\n\n"
                f"<b>Текст сообщения:</b>\n{message.text}"
            )
            await message.bot.send_message(
                ADMIN_ID, 
                admin_text, 
                reply_markup=get_admin_ban_button(user.id)
            )
        else:
            admin_header = (
                f"📩 <b>Медиа-сообщение от пользователя ({source})!</b>\n\n"
                f"<b>Имя:</b> {user.first_name}\n"
                f"<b>ID:</b> <code>{user.id}</code>\n"
                f"<b>Username:</b> {username_str}"
            )
            await message.bot.send_message(
                ADMIN_ID, 
                admin_header, 
                reply_markup=get_admin_ban_button(user.id)
            )
            await message.bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=get_admin_ban_button(user.id)
            )
            
        await message.answer("✅ Ваше сообщение успешно отправлено администратору!", reply_markup=get_main_keyboard(user.id))
    except Exception as e:
        print(f"Error sending to admin: {e}")
        await message.answer("❌ Не удалось отправить сообщение администратору. Попробуйте позже.", reply_markup=get_main_keyboard(user.id))

    if source == "из меню регистрации":
        await message.answer("Вы находитесь в меню регистрации. Отправьте фото или нажмите «✅ Готово».", reply_markup=get_photo_keyboard())
        await state.set_state(Registration.waiting_for_photo)
    else:
        user_db = await get_user(user.id)
        if user_db:
            kb = get_main_keyboard(user.id)
        else:
            kb = get_start_registration_keyboard()
        await state.clear()
        await message.answer("🏠 Вы в главном меню.", reply_markup=kb)

@router.callback_query(F.data.startswith("admin_ban_anon_"))
async def admin_ban_anon_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет прав.", show_alert=True)
        return
    
    target_id = int(callback.data.split("_")[3])
    await ban_user_db(target_id)
    asyncio.create_task(background_send(callback.bot, target_id, "Ваш аккаунт заблокирован за нарушение правил."))
    await callback.answer("Пользователь забанен! ⛔", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@router.message(F.text.in_(["🛑 Главное меню"]))
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    user_db = await get_user(message.from_user.id)
    if user_db:
        kb = get_main_keyboard(message.from_user.id)
    else:
        kb = get_start_registration_keyboard()
    await message.answer("🏠 Вы в главном меню.", reply_markup=kb)

# ==========================================
# --- 2. ПРОСМОТР АНКЕТ ---
# ==========================================
@router.message(F.text == "🔄 Смотреть заново")
async def watch_again(message: Message, state: FSMContext):
    await reset_views(message.from_user.id)
    await message.answer("История просмотров сброшена! Ищем анкеты...")
    await state.update_data(search_mode="random")
    await show_random_profile(message, state)

@router.message(F.text == "🔍 Искать анкеты")
async def start_search(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_start_registration_keyboard())
        return
    if user["banned"]: return
    await state.update_data(search_mode="random")
    await show_random_profile(message, state)

@router.message(F.text == "👀 Кто лайкнул")
async def start_liker_search(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_start_registration_keyboard())
        return
    if user["banned"]: return
    await state.update_data(search_mode="likers")
    await show_next_liker(message, state)

async def show_random_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    target_data = await get_next_profile(user_id)
    
    if not target_data:
        await set_waiting_for_new(user_id)
        await message.answer(
            "Вы посмотрели все анкеты! Мы пришлём уведомление, когда появится несколько новых людей.\n\n"
            "<i>(Или можете просмотреть старые заново)</i>", 
            reply_markup=get_empty_search_keyboard()
        )
        await state.clear()
        return

    await send_profile_card(message, state, target_data, prefix_text="👤 Анкета:")

async def show_next_liker(message: Message, state: FSMContext):
    user_id = message.from_user.id
    target_data = await get_liker_profile(user_id)
    
    if not target_data:
        await message.answer(
            "У вас пока нет новых непросмотренных симпатий 😔", 
            reply_markup=get_main_keyboard(user_id)
        )
        await state.clear()
        return

    await send_profile_card(message, state, target_data, prefix_text="💘 <b>Этот человек лайкнул вас!</b>\n\n👤 Анкета:")

async def send_profile_card(message: Message, state: FSMContext, target_data: dict, prefix_text: str):
    await state.update_data(current_target_id=target_data["user_id"])
    await state.set_state(SearchStates.viewing)
    
    total_reputation = target_data['likes'] - target_data['dislikes']
    
    admin_block = ""
    if message.from_user.id == ADMIN_ID:
        u_name = f"@{target_data['username']}" if target_data['username'] else "Нет"
        admin_block = f"\n\n👑 <b>Инфо админа:</b>\nID: <code>{target_data['user_id']}</code> | {u_name}"
        
    caption = f"{prefix_text}\n{target_data['bio']}\n\n🌟 Репутация: {total_reputation}{admin_block}"
    photos = target_data["photo_ids"].split(",")
    
    is_rewatch = bool(target_data["is_rewatch"])
    keyboard = get_search_keyboard(is_rewatch)
    
    if len(photos) == 1:
        await message.answer_photo(photo=photos[0], caption=caption, reply_markup=keyboard)
    else:
        media = [InputMediaPhoto(media=photos[0], caption=caption)] + [InputMediaPhoto(media=p) for p in photos[1:]]
        await message.answer_media_group(media)

@router.message(SearchStates.viewing, F.text.in_(["❤️ Лайк", "⏭ Пропустить", "👍 Реп+", "👎 Реп-", "🚨 Жалоба"]))
async def handle_profile_action(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("current_target_id")
    search_mode = data.get("search_mode", "random")
    
    if not target_id:
        return
    await state.update_data(current_target_id=None)

    action = message.text
    user_id = message.from_user.id

    if action == "❤️ Лайк":
        is_match, is_new_like = await process_match_action(user_id, target_id)
        if is_match and is_new_like:
            target_data = await get_user(target_id)
            match_kb = get_match_button(target_data)
            
            if match_kb:
                await message.answer("🎉 <b>Взаимная симпатия!</b>\n\nМожете начинать общаться:", reply_markup=match_kb)
            else:
                await message.answer(f"🎉 <b>Взаимная симпатия!</b>\n\nУ пользователя {target_data['first_name']} скрыт аккаунт, но он сможет написать вам первый, если увидит вашу анкету.")

            # Уведомление для второй стороны
            user_data = await get_user(user_id)
            target_match_kb = get_match_button(user_data)
            if target_match_kb:
                asyncio.create_task(background_send(
                    message.bot, target_id, "🎉 <b>Взаимная симпатия!</b>\n\nМожете начинать общаться:", target_match_kb
                ))
            else:
                asyncio.create_task(background_send(
                    message.bot, target_id, f"🎉 <b>Взаимная симпатия!</b>\n\nУ пользователя {user_data['first_name']} скрыт аккаунт, но вы можете написать ему в ответ через поиск/лайки."
                ))
        elif is_new_like:
            await message.answer("Лайк отправлен! ❤️")
            asyncio.create_task(background_send(message.bot, target_id, "❤️ Кому-то понравилась ваша анкета!\nЖми «👀 Кто лайкнул»."))
        else:
            await message.answer("Вы уже лайкали эту анкету ранее ❤️")

    elif action in ["👍 Реп+", "👎 Реп-"]:
        is_plus = (action == "👍 Реп+")
        success = await process_rep_action(user_id, target_id, is_plus)
        if success:
            await message.answer("Репутация обновлена! " + ("👍" if is_plus else "👎"))

    elif action == "⏭ Пропустить":
        await process_skip_action(user_id, target_id)

    elif action == "🚨 Жалоба":
        await process_report_action(user_id, target_id)
        await message.answer("Жалоба отправлена модераторам 🚨")

    if search_mode == "likers":
        await show_next_liker(message, state)
    else:
        await show_random_profile(message, state)


# ==========================================
# --- 3. АДМИН-ПАНЕЛЬ ---
# ==========================================
async def get_reported_user():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE reports > 0 AND banned = 0 ORDER BY reports DESC LIMIT 1") as cursor:
            return await cursor.fetchone()

async def ban_user_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def dismiss_reports_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reports = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

async def run_broadcast(bot: Bot, msg: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE banned = 0") as cursor:
            users = await cursor.fetchall()
            
    for u in users:
        try:
            await bot.copy_message(
                chat_id=u[0],
                from_chat_id=msg.chat.id,
                message_id=msg.message_id
            )
            await asyncio.sleep(0.05)
        except Exception:
            pass

@router.message(F.text == "👑 Админ-панель")
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return 
    await state.clear()
    await message.answer("👑 Панель админа:", reply_markup=get_admin_keyboard())

@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned = 1") as cursor:
            banned_users = (await cursor.fetchone())[0]
            
    await message.answer(f"📊 <b>Статистика бота:</b>\n\nВсего пользователей: {total_users}\nЗаблокировано: {banned_users}", reply_markup=get_admin_keyboard())

@router.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(
        "Отправьте сообщение для рассылки всем пользователям (можно с фото, видео, гифкой):",
        reply_markup=get_admin_keyboard()
    )
    await state.set_state(AdminStates.waiting_for_broadcast_msg)

@router.message(AdminStates.waiting_for_broadcast_msg)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    asyncio.create_task(run_broadcast(message.bot, message))
    
    await message.answer("✅ Рассылка успешно запущена в фоновом режиме!", reply_markup=get_admin_keyboard())
    await state.clear()

@router.message(F.text == "🔎 Поиск анкеты")
async def admin_search_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите ID пользователя (только цифры):", reply_markup=get_admin_keyboard())
    await state.set_state(AdminStates.waiting_for_search_id)

@router.message(AdminStates.waiting_for_search_id)
async def admin_search_process(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("Ошибка: нужно ввести числовой ID.", reply_markup=get_admin_keyboard())
        return
        
    target_data = await get_user(int(message.text))
    if not target_data:
        await message.answer("Пользователь с таким ID не найден в базе.", reply_markup=get_admin_keyboard())
        return
        
    await show_admin_user_card(message, state, target_data, is_report=False)

@router.message(F.text == "🚨 Жалобы")
async def admin_view_reports(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    reported_data = await get_reported_user()
    if not reported_data:
        await message.answer("✅ Все жалобы разобраны!", reply_markup=get_admin_keyboard())
        await state.clear()
        return
    await show_admin_user_card(message, state, reported_data, is_report=True)

async def show_admin_user_card(message: Message, state: FSMContext, target_data, is_report: bool):
    target_id = target_data["user_id"]
    total_reputation = target_data['likes'] - target_data['dislikes']
    
    await state.update_data(report_target_id=target_id, is_report_mode=is_report)
    await state.set_state(AdminStates.viewing_reports)
    
    u_name = f"@{target_data['username']}" if target_data['username'] else "Нет"
    title = "🚨 ЖАЛОБА НА АНКЕТУ" if is_report else "🔎 НАЙДЕНА АНКЕТА"
    
    caption = (
        f"<b>{title}</b>\n\n"
        f"<b>ID:</b> <code>{target_id}</code> | {u_name}\n"
        f"<b>Жалоб:</b> {target_data['reports']}\n"
        f"<b>Анкета:</b> {target_data['bio']}\n"
        f"<b>Репутация:</b> {total_reputation}"
    )
    
    photos = target_data["photo_ids"].split(",")
    await message.answer_photo(photo=photos[0], caption=caption, reply_markup=get_admin_report_keyboard())

@router.message(AdminStates.viewing_reports, F.text.in_(["⛔ Забанить", "✅ Отклонить/Пропустить"]))
async def handle_admin_report_action(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    target_id = data.get("report_target_id")
    is_report_mode = data.get("is_report_mode", False)

    if message.text == "⛔ Забанить":
        await ban_user_db(target_id)
        await message.answer("Пользователь забанен ⛔")
        asyncio.create_task(background_send(message.bot, target_id, "Ваш аккаунт заблокирован за нарушение правил."))
            
    elif message.text == "✅ Отклонить/Пропустить":
        await dismiss_reports_db(target_id)
        await message.answer("Действие пропущено ✅")

    if is_report_mode:
        await admin_view_reports(message, state) 
    else:
        await cmd_admin(message, state) 

@router.message(F.text == "📋 Забаненные")
async def admin_list_banned(message: Message):
    if message.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE banned = 1") as cursor:
            banned_users = await cursor.fetchall()
            
    if banned_users:
        text = "📋 <b>Забаненные:</b>\n\n" + "\n".join(
            [f"ID: <code>{u['user_id']}</code> | {u['first_name']}" for u in banned_users]
        )
    else:
        text = "✅ Забаненных нет."
    await message.answer(text, reply_markup=get_admin_keyboard())

@router.message(F.text == "🔓 Разбанить")
async def admin_unban_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите ID пользователя для разблокировки:", reply_markup=get_admin_keyboard())
    await state.set_state(AdminStates.waiting_for_unban_id)

@router.message(AdminStates.waiting_for_unban_id)
async def process_unban_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit():
        await message.answer("Введите корректный числовой ID.", reply_markup=get_admin_keyboard())
        return

    target_id = int(message.text)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE users SET banned = 0, reports = 0 WHERE user_id = ?", (target_id,))
        await db.commit()
        if cursor.rowcount > 0:
            await message.answer(f"✅ Пользователь {target_id} разблокирован!", reply_markup=get_admin_keyboard())
        else:
            await message.answer("❌ Пользователь не найден.", reply_markup=get_admin_keyboard())
    await state.clear()


# ==========================================
# --- ЗАПУСК ---
# ==========================================
async def main():
    await init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    
    print("Бот успешно запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
