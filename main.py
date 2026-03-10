import logging
import json
import os
import asyncio
import fcntl
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
from telegram.error import BadRequest, TelegramError

# ══════════════════════════════════════════
#  الإعدادات — اقراها من environment variables
# ══════════════════════════════════════════
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
DATA_FILE  = os.environ.get("DATA_FILE", "bot_data.json")
PANEL_GIF  = os.environ.get("PANEL_GIF", "https://i.postimg.cc/wxV3PspQ/1756574872401.gif")
PARSE_MODE = "HTML"

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN مش موجود! حطّه في environment variables")
if not ADMIN_ID:
    raise RuntimeError("❌ ADMIN_ID مش موجود! حطّه في environment variables")
# ══════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
#  قاعدة البيانات مع File Locking
# ──────────────────────────────────────────
_data_lock = asyncio.Lock()   # lock للـ async
_file_lock_fd = None          # file descriptor للـ file lock

def _acquire_file_lock():
    global _file_lock_fd
    _file_lock_fd = open(DATA_FILE + ".lock", "w")
    fcntl.flock(_file_lock_fd, fcntl.LOCK_EX)

def _release_file_lock():
    global _file_lock_fd
    if _file_lock_fd:
        fcntl.flock(_file_lock_fd, fcntl.LOCK_UN)
        _file_lock_fd.close()
        _file_lock_fd = None

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # تأكد إن الـ keys الأساسية موجودة
            data.setdefault("channels", {})
            data.setdefault("hearts", {})
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"load_data error: {e} — هيبدأ بداتا فاضية")
    return {
        "channels": {
            "قناة FY_TF": {
                "id": -1001716446682,
                "username": "@FY_TF",
                "group": "https://t.me/+6HsTzJNvxcFiMWQ0"
            }
        },
        "hearts": {}
    }

def save_data():
    """حفظ آمن مع file locking لمنع تعارض الكتابة."""
    try:
        _acquire_file_lock()
        tmp_file = DATA_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, DATA_FILE)  # atomic replace
    except IOError as e:
        logger.error(f"save_data error: {e}")
    finally:
        _release_file_lock()

DB = load_data()

admin_panel_msg: dict[int, int] = {}
pending:         dict[int, dict] = {}
input_state:     dict[int, dict] = {}

# تنظيف الـ pending القديمة (أكتر من ساعة)
import time
pending_time: dict[int, float] = {}

def cleanup_pending():
    """امسح الـ pending القديمة عشان ما يتراكموش في الـ memory."""
    now = time.time()
    old_keys = [k for k, t in pending_time.items() if now - t > 3600]
    for k in old_keys:
        pending.pop(k, None)
        pending_time.pop(k, None)
    if old_keys:
        logger.info(f"cleanup_pending: مسحنا {len(old_keys)} عنصر قديم")


# ──────────────────────────────────────────
#  جلب معلومات القناة تلقائياً
# ──────────────────────────────────────────
async def fetch_channel_info(bot, ch_id: int) -> dict | None:
    try:
        chat = await bot.get_chat(ch_id)
        return {
            "id":       ch_id,
            "username": "@" + chat.username if chat.username else "ID:" + str(ch_id),
            "title":    chat.title or "قناة بدون اسم",
        }
    except TelegramError as e:
        logger.error(f"fetch_channel_info [{ch_id}]: {e}")
        return None
    except Exception as e:
        logger.error(f"fetch_channel_info unexpected [{ch_id}]: {e}")
        return None


# ──────────────────────────────────────────
#  مساعدات القلب
# ──────────────────────────────────────────
def hkey(ch_id: int, msg_id: int) -> str:
    return f"{ch_id}:{msg_id}"

def heart_count(ch_id: int, msg_id: int) -> int:
    return len(DB["hearts"].get(hkey(ch_id, msg_id), []))

def heart_kb(ch_id: int, msg_id: int) -> InlineKeyboardMarkup:
    c     = heart_count(ch_id, msg_id)
    label = f"❤️  {c}" if c else "🤍"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"heart_{ch_id}_{msg_id}")
    ]])


# ──────────────────────────────────────────
#  نصوص لوحة التحكم
# ──────────────────────────────────────────
def panel_home() -> tuple:
    ch_count     = len(DB["channels"])
    total_hearts = sum(len(v) for v in DB["hearts"].values())
    text = (
        "🤖 <b>لوحة تحكم البوت</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f" القنوات المضافة: <b>{ch_count}</b>\n"
        f" إجمالي القلوب: <b>{total_hearts}</b>\n\n"
        "اختار من القائمة "
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(" إدارة القنوات",    callback_data="go_channels")],
        [InlineKeyboardButton(" نشر رسالة جديدة", callback_data="go_publish")],
        [InlineKeyboardButton(" الإحصائيات",       callback_data="go_stats")],
    ])
    return text, kb

def panel_channels() -> tuple:
    if not DB["channels"]:
        text = " <b>إدارة القنوات</b>\n━━━━━━━━━━━━━━━━━━━━\n\n⚠️ لا توجد قنوات مضافة بعد."
    else:
        parts = []
        for i, (name, info) in enumerate(DB["channels"].items()):
            grp = " ✔ جروب" if info.get("group") else " ✘ بدون جروب"
            parts.append(f"  {i+1}. <b>{name}</b>  {info['username']}{grp}")
        text = " <b>إدارة القنوات</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(parts)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(" إضافة قناة",       callback_data="ch_add")],
        [InlineKeyboardButton(" إضافة/تعديل جروب", callback_data="ch_group_list")],
        [InlineKeyboardButton("حذف قناة",         callback_data="ch_del_list")],
        [InlineKeyboardButton(" الرئيسية",          callback_data="go_home")],
    ])
    return text, kb

def panel_group_list() -> tuple:
    buttons = []
    for name, info in DB["channels"].items():
        icon = "✔ " if info.get("group") else "➕ "
        buttons.append([InlineKeyboardButton(
            icon + name, callback_data="ch_grpset_" + str(info["id"])
        )])
    buttons.append([InlineKeyboardButton(" رجوع", callback_data="go_channels")])
    return " <b>اختار القناة لإضافة/تعديل الجروب:</b>\n━━━━━━━━━━━━━━━━━━━━", InlineKeyboardMarkup(buttons)

def panel_delete_list() -> tuple:
    buttons = [
        [InlineKeyboardButton(
            f"✔ {name}  ({info['username']})",
            callback_data="ch_del_" + str(info["id"])
        )]
        for name, info in DB["channels"].items()
    ]
    buttons.append([InlineKeyboardButton(" رجوع", callback_data="go_channels")])
    return "✔ <b>اختار القناة اللي تحذفها:</b>\n━━━━━━━━━━━━━━━━━━━━", InlineKeyboardMarkup(buttons)

def panel_publish() -> tuple:
    if not DB["channels"]:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(" الرئيسية", callback_data="go_home")]])
        return " <b>نشر رسالة</b>\n━━━━━━━━━━━━━━━━━━━━\n\n⚠️ لازم تضيف قناة الأول!", kb
    text = (
        " <b>نشر رسالة جديدة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "ابعتلي الرسالة اللي عاوز تنشرها\n"
        "<i>نص / صورة / فيديو / ملف / صوت</i>\n\n"
        "⏳ في انتظار رسالتك..."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(" الرئيسية", callback_data="go_home")]])
    return text, kb

def panel_select_channel(admin_msg_id: int) -> tuple:
    buttons = [
        [InlineKeyboardButton(
            f" {name}",
            callback_data=f"pub_{admin_msg_id}_{info['id']}"
        )]
        for name, info in DB["channels"].items()
    ]
    if len(DB["channels"]) > 1:
        buttons.append([InlineKeyboardButton(
            "📣 نشر في كل القنوات",
            callback_data=f"pub_{admin_msg_id}_ALL"
        )])
    buttons.append([InlineKeyboardButton(
        "✘ إلغاء",
        callback_data=f"pub_cancel_{admin_msg_id}"
    )])
    return "📤 <b>اختار القناة للنشر:</b>\n━━━━━━━━━━━━━━━━━━━━", InlineKeyboardMarkup(buttons)

def panel_stats() -> tuple:
    if not DB["hearts"]:
        text = "📊 <b>الإحصائيات</b>\n━━━━━━━━━━━━━━━━━━━━\n\nلا توجد بيانات بعد."
    else:
        lines = []
        for key, users in DB["hearts"].items():
            ch_id_s, msg_id_s = key.split(":")
            ch_name = next(
                (n for n, i in DB["channels"].items() if str(i["id"]) == ch_id_s),
                ch_id_s
            )
            lines.append(f"  • <b>{ch_name}</b>  رسالة {msg_id_s}  ❤️ {len(users)}")
        text = "📊 <b>الإحصائيات</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(" الرئيسية", callback_data="go_home")]])
    return text, kb

def panel_add_step() -> tuple:
    text = (
        "➕ <b>إضافة قناة جديدة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "ابعتلي <b>ID القناة</b> بس\n"
        "والبوت هيجيب اسمها تلقائياً ✔\n\n"
        "📌 عشان تعرف الـ ID:\n"
        "فوّرد أي رسالة من القناة لـ @userinfobot\n\n"
        "مثال: <code>-1001234567890</code>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
    return text, kb


# ──────────────────────────────────────────
#  تحديث / إرسال لوحة التحكم
# ──────────────────────────────────────────
async def update_panel(query, text: str, kb: InlineKeyboardMarkup):
    try:
        await query.edit_message_caption(caption=text, parse_mode=PARSE_MODE, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"update_panel BadRequest: {e}")
    except TelegramError as e:
        logger.warning(f"update_panel TelegramError: {e}")

async def send_new_panel(context, chat_id: int, text: str, kb: InlineKeyboardMarkup):
    try:
        msg = await context.bot.send_animation(
            chat_id=chat_id, animation=PANEL_GIF,
            caption=text, parse_mode=PARSE_MODE, reply_markup=kb
        )
        admin_panel_msg[chat_id] = msg.message_id
    except TelegramError as e:
        logger.error(f"send_animation failed: {e} — هبعت message عادية")
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=PARSE_MODE, reply_markup=kb
            )
            admin_panel_msg[chat_id] = msg.message_id
        except TelegramError as e2:
            logger.error(f"send_message also failed: {e2}")

async def edit_panel_by_id(context, chat_id: int, text: str, kb: InlineKeyboardMarkup):
    msg_id = admin_panel_msg.get(chat_id)
    if msg_id:
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=text, parse_mode=PARSE_MODE, reply_markup=kb
            )
            return
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"edit_panel_by_id BadRequest: {e} — هبعت panel جديدة")
        except TelegramError as e:
            logger.warning(f"edit_panel_by_id TelegramError: {e} — هبعت panel جديدة")
    await send_new_panel(context, chat_id, text, kb)


# ──────────────────────────────────────────
#  نشر رسالة في قناة
# ──────────────────────────────────────────
async def publish_to_channel(bot, ch_id: int, info: dict):
    cap   = info.get("caption", "")
    dummy = heart_kb(ch_id, 0)
    sent  = None

    try:
        if   info["type"] == "text":     sent = await bot.send_message(ch_id, info["text"], reply_markup=dummy)
        elif info["type"] == "photo":    sent = await bot.send_photo(ch_id, info["file_id"], caption=cap, reply_markup=dummy)
        elif info["type"] == "video":    sent = await bot.send_video(ch_id, info["file_id"], caption=cap, reply_markup=dummy)
        elif info["type"] == "document": sent = await bot.send_document(ch_id, info["file_id"], caption=cap, reply_markup=dummy)
        elif info["type"] == "audio":    sent = await bot.send_audio(ch_id, info["file_id"], caption=cap, reply_markup=dummy)
        elif info["type"] == "voice":    sent = await bot.send_voice(ch_id, info["file_id"], caption=cap, reply_markup=dummy)
        elif info["type"] == "sticker":  sent = await bot.send_sticker(ch_id, info["file_id"])
    except TelegramError as e:
        logger.error(f"publish_to_channel [{ch_id}]: {e}")
        return None

    if sent:
        key = hkey(ch_id, sent.message_id)
        async with _data_lock:
            DB["hearts"][key] = []
            save_data()
        if info["type"] != "sticker":
            try:
                await sent.edit_reply_markup(reply_markup=heart_kb(ch_id, sent.message_id))
            except TelegramError as e:
                logger.warning(f"edit_reply_markup after publish: {e}")
        return sent.message_id
    return None


# ──────────────────────────────────────────
#  /start
# ──────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        chs = " | ".join(i["username"] for i in DB["channels"].values())
        await update.message.reply_text("👋🏻 أهلاً!\n👀 قنواتنا: " + (chs or "لا يوجد قنوات بعد"))
        return

    old_id = admin_panel_msg.pop(user_id, None)
    if old_id:
        try:
            await context.bot.delete_message(user_id, old_id)
        except TelegramError as e:
            logger.warning(f"delete old panel: {e}")
    try:
        await update.message.delete()
    except TelegramError as e:
        logger.warning(f"delete /start message: {e}")

    await send_new_panel(context, user_id, *panel_home())


# ──────────────────────────────────────────
#  معالجة الأزرار
# ──────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data

    if not data.startswith("heart_"):
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning(f"query.answer: {e}")

    # ── التنقل بين الشاشات ──
    if data == "go_home":
        await update_panel(query, *panel_home())

    elif data == "go_channels":
        input_state.pop(user_id, None)
        await update_panel(query, *panel_channels())

    elif data == "go_publish":
        await update_panel(query, *panel_publish())

    elif data == "go_stats":
        await update_panel(query, *panel_stats())

    elif data == "ch_add":
        input_state[user_id] = {"step": "id", "data": {}}
        await update_panel(query, *panel_add_step())

    elif data == "ch_group_list":
        if not DB["channels"]:
            await update_panel(query, *panel_channels())
        else:
            await update_panel(query, *panel_group_list())

    elif data.startswith("ch_grpset_"):
        try:
            ch_id = int(data.split("_")[2])
        except (IndexError, ValueError):
            await update_panel(query, *panel_channels())
            return
        ch_name = next((n for n, i in DB["channels"].items() if i["id"] == ch_id), "")
        cur_grp = DB["channels"].get(ch_name, {}).get("group", "")
        cur_txt = f"\nالجروب الحالي: {cur_grp}" if cur_grp else "\n⚠️ لا يوجد جروب حالياً"
        input_state[user_id] = {"step": "group", "data": {"ch_id": ch_id, "ch_name": ch_name}}
        text = (
            " <b>إضافة/تعديل جروب</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"القناة: <b>{ch_name}</b>{cur_txt}\n\n"
            "ابعتلي لينك الجروب الجديد:\n"
            "مثال: <code>https://t.me/+xxxxxxxxx</code>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
        await update_panel(query, text, kb)

    elif data == "ch_del_list":
        if not DB["channels"]:
            await update_panel(query, *panel_channels())
        else:
            await update_panel(query, *panel_delete_list())

    elif data.startswith("ch_del_"):
        try:
            ch_id = int(data.split("_")[2])
        except (IndexError, ValueError):
            await update_panel(query, *panel_channels())
            return
        ch_name = next((n for n, i in DB["channels"].items() if i["id"] == ch_id), None)
        if ch_name:
            async with _data_lock:
                del DB["channels"][ch_name]
                save_data()
        await update_panel(query, *panel_channels())

    elif data.startswith("pub_cancel_"):
        try:
            msg_id = int(data.split("_")[2])
            pending.pop(msg_id, None)
            pending_time.pop(msg_id, None)
        except (IndexError, ValueError):
            pass
        await update_panel(query, *panel_home())

    elif data.startswith("pub_"):
        if user_id != ADMIN_ID:
            return
        parts = data.split("_", 2)
        if len(parts) < 3:
            await update_panel(query, *panel_home())
            return
        try:
            admin_msg_id = int(parts[1])
        except ValueError:
            await update_panel(query, *panel_home())
            return
        target = parts[2]
        info   = pending.get(admin_msg_id)
        if not info:
            await update_panel(query, *panel_home())
            return
        try:
            if target == "ALL":
                results = []
                for name, ch in DB["channels"].items():
                    mid = await publish_to_channel(context.bot, ch["id"], info)
                    results.append(("✔ " if mid else "✘ ") + name)
                async with _data_lock:
                    pending.pop(admin_msg_id, None)
                    pending_time.pop(admin_msg_id, None)
                result_text = "✔ <b>تم النشر بنجاح!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(results)
            else:
                try:
                    ch_id = int(target)
                except ValueError:
                    await update_panel(query, *panel_home())
                    return
                ch_name = next((n for n, i in DB["channels"].items() if i["id"] == ch_id), "القناة")
                mid     = await publish_to_channel(context.bot, ch_id, info)
                async with _data_lock:
                    pending.pop(admin_msg_id, None)
                    pending_time.pop(admin_msg_id, None)
                result_text = (
                    f"✔ <b>تم النشر في {ch_name}!</b>\n━━━━━━━━━━━━━━━━━━━━\n\nID: {mid}"
                    if mid else f"✘ فشل النشر في {ch_name}"
                )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="go_home")]])
            await update_panel(query, result_text, kb)
        except TelegramError as e:
            logger.error(f"publish error: {e}")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="go_home")]])
            await update_panel(query, f"✘ خطأ في النشر: {e}", kb)
        except Exception as e:
            logger.error(f"publish unexpected error: {e}")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="go_home")]])
            await update_panel(query, "✘ حصل خطأ غير متوقع، جرب تاني", kb)

    # ── زر القلب ──
    elif data.startswith("heart_"):
        parts = data.split("_")
        if len(parts) < 3:
            return
        try:
            ch_id  = int(parts[1])
            msg_id = int(parts[2])
        except ValueError:
            return

        key = hkey(ch_id, msg_id)

        # تحقق من الاشتراك
        try:
            member    = await context.bot.get_chat_member(ch_id, user_id)
            is_member = member.status in ("member", "administrator", "creator")
        except TelegramError as e:
            logger.warning(f"get_chat_member [{ch_id}]: {e}")
            is_member = False

        if not is_member:
            try:
                chat    = await context.bot.get_chat(ch_id)
                ch_name = (chat.title or "القناة")[:30]
                ch_link = f"t.me/{chat.username}" if chat.username else ""
                alert   = f"✘ مش مشترك!\n {ch_name}\n🔗 {ch_link}\nاشترك وبعدين اضغط ❤️"
                alert   = alert[:200]
            except TelegramError:
                alert = "✘ اشترك في القناة الأول!"
            try:
                await query.answer(alert, show_alert=True)
            except TelegramError as e:
                logger.warning(f"query.answer (not member): {e}")
            return

        # منع التكرار والـ race condition
        async with _data_lock:
            if key not in DB["hearts"]:
                DB["hearts"][key] = []

            if user_id in DB["hearts"][key]:
                try:
                    await query.answer("💔 مـخلاص يـنجم هـي،لعـبه", show_alert=True)
                except TelegramError:
                    pass
                return

            DB["hearts"][key].append(user_id)
            save_data()
            count = len(DB["hearts"][key])

        # تحديث العداد
        try:
            await query.edit_message_reply_markup(reply_markup=heart_kb(ch_id, msg_id))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.error(f"edit heart markup: {e}")
        except TelegramError as e:
            logger.error(f"edit heart markup TelegramError: {e}")

        # جيب الجروب وابعت للمستخدم
        ch_group = next((i.get("group") for i in DB["channels"].values() if i["id"] == ch_id), None)

        if ch_group:
            kb_group = InlineKeyboardMarkup([[
                InlineKeyboardButton(" انضم للجروب", url=ch_group)
            ]])
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="❤️ <b>شكراً! تم </b>\n\nانضم لجروب الدردشة 👇",
                    parse_mode=PARSE_MODE,
                    reply_markup=kb_group
                )
            except TelegramError as e:
                logger.warning(f"send heart DM to {user_id}: {e}")
            try:
                await query.answer(f"صلي على النبي {count}", show_alert=False)
            except TelegramError:
                pass
        else:
            try:
                await query.answer(f"صلي على النبي {count}", show_alert=True)
            except TelegramError:
                pass


# ──────────────────────────────────────────
#  استقبال رسائل الأدمن
# ──────────────────────────────────────────
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg     = update.message
    if not msg:
        return

    # تنظيف الـ pending القديمة
    cleanup_pending()

    if user_id in input_state:
        state = input_state[user_id]
        text  = msg.text.strip() if msg.text else ""
        try:
            await msg.delete()
        except TelegramError as e:
            logger.warning(f"delete admin msg: {e}")

        # إضافة قناة — خطوة الـ ID
        if state["step"] == "id":
            try:
                ch_id = int(text)
            except ValueError:
                err = "➕ <b>إضافة قناة</b>\n━━━━━━━━━━━━━━━━━━━━\n\n✘ الـ ID لازم يكون رقم!\nمثال: <code>-1001234567890</code>"
                kb  = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
                await edit_panel_by_id(context, user_id, err, kb)
                return

            loading = "⏳ <b>جاري جلب معلومات القناة...</b>"
            kb_load = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
            await edit_panel_by_id(context, user_id, loading, kb_load)

            ch_info = await fetch_channel_info(context.bot, ch_id)
            if not ch_info:
                err = "➕ <b>إضافة قناة</b>\n━━━━━━━━━━━━━━━━━━━━\n\n✘ مقدرتش أجيب معلومات القناة!\nتأكد إن البوت أدمن فيها وحاول تاني:"
                kb  = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
                await edit_panel_by_id(context, user_id, err, kb)
                return

            ch_name = ch_info["title"]
            async with _data_lock:
                DB["channels"][ch_name] = {"id": ch_id, "username": ch_info["username"], "group": ""}
                save_data()
            input_state.pop(user_id, None)

            success = (
                f"✔ <b>تمت إضافة القناة!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f" <b>{ch_name}</b>  {ch_info['username']}\n"
                f"🆔 <code>{ch_id}</code>\n\n"
                "💡 تقدر تضيف جروب من إدارة القنوات"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(" إضافة جروب الآن", callback_data=f"ch_grpset_{ch_id}")],
                [InlineKeyboardButton("🏠 الرئيسية", callback_data="go_home")],
            ])
            await edit_panel_by_id(context, user_id, success, kb)

        # إضافة/تعديل جروب
        elif state["step"] == "group":
            ch_id   = state["data"]["ch_id"]
            ch_name = state["data"]["ch_name"]
            link    = text

            if not (link.startswith("https://t.me/") or link.startswith("t.me/")):
                err = (
                    " <b>إضافة جروب</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "✘ اللينك مش صح!\n"
                    "لازم يبدأ بـ <code>https://t.me/</code>\n\nحاول تاني:"
                )
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("✘ إلغاء", callback_data="go_channels")]])
                await edit_panel_by_id(context, user_id, err, kb)
                return

            ch_name_key = next((n for n, i in DB["channels"].items() if i["id"] == ch_id), None)
            if ch_name_key:
                async with _data_lock:
                    DB["channels"][ch_name_key]["group"] = link
                    save_data()
            input_state.pop(user_id, None)

            success = (
                f"✔ <b>تم إضافة الجروب!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f" القناة: <b>{ch_name}</b>\n"
                f" الجروب: {link}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(" إدارة القنوات", callback_data="go_channels")],
                [InlineKeyboardButton("🏠 الرئيسية",      callback_data="go_home")],
            ])
            await edit_panel_by_id(context, user_id, success, kb)
        return

    # ── وضع النشر ──
    info = {"type": None, "file_id": None, "text": None, "caption": None}
    if   msg.text:     info.update({"type": "text",     "text": msg.text})
    elif msg.photo:    info.update({"type": "photo",    "file_id": msg.photo[-1].file_id, "caption": msg.caption or ""})
    elif msg.video:    info.update({"type": "video",    "file_id": msg.video.file_id,     "caption": msg.caption or ""})
    elif msg.document: info.update({"type": "document", "file_id": msg.document.file_id,  "caption": msg.caption or ""})
    elif msg.audio:    info.update({"type": "audio",    "file_id": msg.audio.file_id,     "caption": msg.caption or ""})
    elif msg.voice:    info.update({"type": "voice",    "file_id": msg.voice.file_id,     "caption": msg.caption or ""})
    elif msg.sticker:  info.update({"type": "sticker",  "file_id": msg.sticker.file_id})
    else:
        return

    pending[msg.message_id] = info
    pending_time[msg.message_id] = time.time()

    try:
        await msg.delete()
    except TelegramError as e:
        logger.warning(f"delete publish msg: {e}")

    await edit_panel_by_id(context, user_id, *panel_select_channel(msg.message_id))


# ──────────────────────────────────────────
#  تشغيل
# ──────────────────────────────────────────
def main():
    print(" جاري تشغيل البوت...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.User(ADMIN_ID) & ~filters.COMMAND, handle_admin_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✔ البوت شغال!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
