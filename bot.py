import json
import asyncio, json, logging, re, uuid, ipaddress
from datetime import datetime
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from aiogram.filters import StateFilter
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from urllib.parse import urlparse
import time
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from config import *

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
DB = DATABASE_PATH
dp = Dispatcher()

class Buy(StatesGroup):
    dest_ip = State()

class TestFlow(StatesGroup):
    dest_ip = State()

class PaymentFlow(StatesGroup):
    title = State()
    card = State()
    owner = State()

class Topup(StatesGroup):
    amount = State()
    receipt = State()

class ServiceFlow(StatesGroup):
    change_ip = State()
    change_port = State()

class AdminFlow(StatesGroup):
    server_name = State()
    server_url = State()
    server_username = State()
    server_password = State()
    server_delivery_ip = State()
    server_create_price = State()
    server_gb_price = State()
    set_warning = State()
    set_card = State()
    set_owner = State()
    set_create_price = State()
    set_gb_price = State()
    credit_user = State()
    credit_amount = State()
    debit_user = State()
    debit_amount = State()
    edit_button_label = State()

def is_admin(uid): return uid == ADMIN_ID
def money(n): return f"{int(n):,} تومان"


async def edit_or_send(q: CallbackQuery, text: str, reply_markup=None, **kwargs):
    """Keep callback navigation inside one Telegram message whenever possible."""
    try:
        return await q.message.edit_text(
            text, reply_markup=reply_markup, **kwargs
        )
    except Exception as exc:
        # Telegram rejects no-op edits and some message types cannot be edited.
        if "message is not modified" in str(exc).lower():
            return q.message
        return await q.message.answer(text, reply_markup=reply_markup, **kwargs)


async def edit_caption_or_send(q: CallbackQuery, caption: str, reply_markup=None):
    try:
        return await q.message.edit_caption(caption=caption, reply_markup=reply_markup)
    except Exception:
        return await edit_or_send(q, caption, reply_markup=reply_markup)



def db():
    return aiosqlite.connect(DB)

async def init_db():
    async with db() as c:
        await c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, warned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,url TEXT,username TEXT,password TEXT,delivery_ip TEXT,create_price INTEGER DEFAULT 0,gb_price INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,server_id INTEGER,inbound_id INTEGER,port INTEGER,dest_ip TEXT,name TEXT,enabled INTEGER DEFAULT 1,last_bytes INTEGER DEFAULT 0,total_bytes INTEGER DEFAULT 0,created_at TEXT);
        CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount INTEGER,receipt_id TEXT,status TEXT DEFAULT 'pending',created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount INTEGER,reason TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS test_history(user_id INTEGER,server_id INTEGER,first_test_at TEXT,PRIMARY KEY(user_id,server_id));
        CREATE TABLE IF NOT EXISTS test_services(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,server_id INTEGER,inbound_id INTEGER,port INTEGER,dest_ip TEXT,last_bytes INTEGER DEFAULT 0,total_bytes INTEGER DEFAULT 0,status TEXT DEFAULT 'active',created_at TEXT,finished_at TEXT);
        CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,card_number TEXT NOT NULL,owner TEXT,active INTEGER NOT NULL DEFAULT 1);

        """)
        cur = await c.execute("PRAGMA table_info(servers)")
        columns = [row[1] for row in await cur.fetchall()]
        if "template_inbound_id" not in columns:
            await c.execute("ALTER TABLE servers ADD COLUMN template_inbound_id INTEGER")
        if "test_enabled" not in columns:
            await c.execute("ALTER TABLE servers ADD COLUMN test_enabled INTEGER NOT NULL DEFAULT 1")
        cur = await c.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in await cur.fetchall()]
        if "blocked" not in user_columns:
            await c.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        cur = await c.execute("SELECT COUNT(*) FROM payment_methods")
        if (await cur.fetchone())[0] == 0:
            cur = await c.execute("SELECT value FROM settings WHERE key='card'")
            old_card = await cur.fetchone()
            cur = await c.execute("SELECT value FROM settings WHERE key='owner'")
            old_owner = await cur.fetchone()
            if old_card and old_card[0].strip():
                await c.execute("INSERT INTO payment_methods(title,card_number,owner) VALUES(?,?,?)",
                                ("کارت قبلی", old_card[0], old_owner[0] if old_owner else ""))
        await c.commit()

async def setting(key, default):
    async with db() as c:
        cur=await c.execute("SELECT value FROM settings WHERE key=?",(key,))
        row=await cur.fetchone()
        return row[0] if row else str(default)

async def set_setting(key, value):
    async with db() as c:
        await c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
        await c.commit()

async def ensure_user(uid):
    async with db() as c:
        cur = await c.execute("INSERT OR IGNORE INTO users(id) VALUES(?)",(uid,))
        await c.commit()
        return cur.rowcount == 1

USER_BUTTONS = {"buy": "🔴 خرید تانل", "test": "🔵 تست رایگان", "topup": "💰 افزایش موجودی", "balance": "💳 کیف پول", "usage": "📊 گزارش مصرف", "services": "📡 سرویس‌های من"}

async def user_reply_kb():
    labels = {k: await setting(f"user_btn_{k}", v) for k, v in USER_BUTTONS.items()}
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=labels["buy"]), KeyboardButton(text=labels["test"])],
        [KeyboardButton(text=labels["services"])],
        [KeyboardButton(text=labels["topup"]), KeyboardButton(text=labels["balance"])],
        [KeyboardButton(text=labels["usage"])],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder="یک گزینه از منو انتخاب کنید…")

class BlockedUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_admin(user.id):
            async with db() as c:
                row = await (await c.execute("SELECT blocked FROM users WHERE id=?", (user.id,))).fetchone()
            if row and row[0]:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔️ دسترسی شما مسدود شده است.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔️ دسترسی شما به ربات مسدود شده است.")
                return
        return await handler(event, data)

dp.message.middleware(BlockedUserMiddleware())
dp.callback_query.middleware(BlockedUserMiddleware())

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ خرید تانل",callback_data="buy")],
        [InlineKeyboardButton(text="➕ شارژ حساب",callback_data="topup"),InlineKeyboardButton(text="💳 کیف پول",callback_data="balance")],
        [InlineKeyboardButton(text="📈 گزارش مصرف",callback_data="usage"),InlineKeyboardButton(text="📡 سرویس‌های من",callback_data="services")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن سرور",callback_data="a_addserver"),InlineKeyboardButton(text="📡 لیست سرورها",callback_data="a_servers")],
        [InlineKeyboardButton(text="💳 روش‌های پرداخت",callback_data="a_payments")],
        [InlineKeyboardButton(text="⚠️ هشدار موجودی",callback_data="a_warning")],
        [InlineKeyboardButton(text="🧾 رسیدهای در انتظار",callback_data="a_topups")],
        [InlineKeyboardButton(text="👥 کاربران ربات",callback_data="a_users")],
        [InlineKeyboardButton(text="🎨 ویرایش دکمه‌های کاربر",callback_data="a_userlabels")],
        [InlineKeyboardButton(text="➕ شارژ دستی",callback_data="a_credit"),InlineKeyboardButton(text="➖ کسر موجودی",callback_data="a_debit")],
        [InlineKeyboardButton(text="🏠 بازگشت به فروشگاه",callback_data="back_shop")]
    ])

def flow_cancel_kb(is_admin_flow=False):
    target = "cancel_admin_flow" if is_admin_flow else "cancel_user_flow"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ لغو عملیات", callback_data=target)]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به پنل مدیریت", callback_data="admin_home")]
    ])

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    # /start always exits an unfinished flow, so the user is never trapped in a prompt.
    await state.clear()
    is_new = await ensure_user(m.from_user.id)
    if is_new:
        u = m.from_user
        try:
            await m.bot.send_message(ADMIN_ID,
                "🆕 <b>کاربر جدید وارد ربات شد</b>\\n"
                f"🆔 شناسه: <code>{u.id}</code>\\n"
                f"👤 نام: {u.full_name}\\n"
                f"🔗 نام کاربری: @{u.username}" if u.username else
                "🆕 <b>کاربر جدید وارد ربات شد</b>\\n"
                f"🆔 شناسه: <code>{u.id}</code>\\n👤 نام: {u.full_name}",
                parse_mode="HTML")
        except Exception:
            logging.exception("new user notification failed")
    await m.answer("✨ <b>به سامانه خدمات تانل خوش آمدید</b>\n\nاز منوی پایین، بخش موردنظر را انتخاب کنید.", reply_markup=await user_reply_kb(), parse_mode="HTML")

@dp.message(Command("cancel"))
async def cancel_command(m: Message, state: FSMContext):
    await state.clear()
    if is_admin(m.from_user.id):
        return await m.answer("🛠 عملیات لغو شد.", reply_markup=admin_kb())
    await m.answer("🏪 عملیات لغو شد. به منوی اصلی برگشتید.", reply_markup=await user_reply_kb())

@dp.message(Command("admin"))
async def admin(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return await m.answer("⛔️ دسترسی ندارید.")
    await state.clear()
    await m.answer("🛠 پنل مدیریت", reply_markup=admin_kb())
@dp.callback_query(F.data=="back_shop")
async def back_shop(q: CallbackQuery):
    await edit_or_send(q, 
        "🏪 به فروشگاه خوش آمدید.",
        reply_markup=main_kb()
    )
    await q.answer()
@dp.callback_query(F.data == "admin_home")
async def admin_home(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id):
        return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    await state.clear()
    await edit_or_send(q, "🛠 پنل مدیریت", reply_markup=admin_kb())
    await q.answer()

@dp.callback_query(F.data == "cancel_user_flow")
async def cancel_user_flow(q: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_send(q, "🏪 عملیات لغو شد. به منوی اصلی برگشتید.", reply_markup=main_kb())
    await q.answer("عملیات لغو شد.")

@dp.callback_query(F.data == "cancel_admin_flow")
async def cancel_admin_flow(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id):
        return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    await state.clear()
    await edit_or_send(q, "🛠 عملیات لغو شد. پنل مدیریت", reply_markup=admin_kb())
    await q.answer("عملیات لغو شد.")

@dp.callback_query(F.data=="balance")
async def balance(q: CallbackQuery):
    await ensure_user(q.from_user.id)
    async with db() as c:
        row=await (await c.execute("SELECT balance FROM users WHERE id=?",(q.from_user.id,))).fetchone()
    await edit_or_send(q, "💳 <b>موجودی حساب شما</b>\n\n💰 "+money(row[0]), reply_markup=main_kb(), parse_mode="HTML")
    await q.answer()


async def choose_free_port(api, server_id, low=20000, high=60000, attempts=150):
    inbounds = await api.list_inbounds()
    used = set()
    for item in inbounds:
        try: used.add(int(item.get("port")))
        except (TypeError, ValueError): pass
    async with db() as c:
        rows = await (await c.execute("SELECT port FROM services WHERE server_id=? UNION SELECT port FROM test_services WHERE server_id=?",
                                      (server_id, server_id))).fetchall()
    used.update(int(r[0]) for r in rows if r[0] is not None)
    import secrets
    for _ in range(attempts):
        candidate = secrets.randbelow(high-low+1)+low
        if candidate not in used:
            return candidate
    raise RuntimeError("No free port found in configured range")

@dp.callback_query(F.data=="buy")
async def buy(q: CallbackQuery):
    async with db() as c:
        rows=await (await c.execute("SELECT id,name,create_price,gb_price FROM servers WHERE active=1")).fetchall()
    if not rows:
        await edit_or_send(q, "فعلاً هیچ پلن فعالی وجود ندارد.")
    else:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{r[1]} | ساخت {money(r[2])} | هر گیگ {money(r[3])}",callback_data=f"choose:{r[0]}")] for r in rows])
        await edit_or_send(q, "پلن موردنظر را انتخاب کنید:",reply_markup=kb)
    await q.answer()

@dp.callback_query(F.data.startswith("choose:"))
async def choose(q: CallbackQuery,state:FSMContext):
    sid=int(q.data.split(":")[1])
    async with db() as c:
        row=await (await c.execute("SELECT id,name,create_price FROM servers WHERE id=? AND active=1",(sid,))).fetchone()
    if not row: return await q.answer("پلن در دسترس نیست.",show_alert=True)
    await state.update_data(server_id=sid,create_price=row[2])
    await state.set_state(Buy.dest_ip)
    await edit_or_send(q, "🌐 IP مقصد را وارد کنید؛ پورت به‌صورت خودکار و یکتا انتخاب می‌شود:", reply_markup=flow_cancel_kb())
    await q.answer()

@dp.message(Buy.dest_ip)
async def get_ip(m:Message,state:FSMContext):
    try:
        ip = str(ipaddress.ip_address(m.text.strip()))
    except Exception:
        return await m.answer("❌ IP معتبر وارد کنید.", reply_markup=flow_cancel_kb())

    d = await state.get_data()
    await ensure_user(m.from_user.id)

    async with db() as c:
        server = await (await c.execute(
            "SELECT name,url,username,password,delivery_ip,create_price,gb_price,template_inbound_id FROM servers WHERE id=? AND active=1",
            (d["server_id"],)
        )).fetchone()

        user = await (await c.execute(
            "SELECT balance FROM users WHERE id=?",
            (m.from_user.id,)
        )).fetchone()

    if not server:
        await state.clear()
        return await m.answer("پلن دیگر فعال نیست.")

    if not server[7]:
        await state.clear()
        return await m.answer("برای این سرور هنوز اینباند نمونه انتخاب نشده است. با مدیر تماس بگیرید.")

    if user[0] < server[5]:
        await state.clear()
        return await m.answer(f"موجودی کافی نیست. هزینه ساخت: {money(server[5])}")

    name = f"user_{m.from_user.id}_{int(time.time())}"

    try:
        async with Sanaei(server[1], server[2], server[3]) as api:
            port = await choose_free_port(api, d["server_id"])
            name = f"user_{m.from_user.id}_{port}"
            result = await api.create_tunnel(port, ip, name, server[7])

            inbound_id = int(result)

            async with db() as c:
                await c.execute("BEGIN IMMEDIATE")

                cur = await c.execute(
                    "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                    (server[5], m.from_user.id, server[5])
                )

                if cur.rowcount != 1:
                    await c.rollback()
                    await api.set_enable(inbound_id, False)
                    return await m.answer(
                        "موجودی تغییر کرده و کافی نیست؛ تانل برای جلوگیری از بدهی غیرفعال شد."
                    )

                await c.execute(
                    "INSERT INTO services(user_id,server_id,inbound_id,port,dest_ip,name,last_bytes,created_at) VALUES(?,?,?,?,?,?,0,?)",
                    (
                        m.from_user.id,
                        d["server_id"],
                        inbound_id,
                        port,
                        ip,
                        name,
                        datetime.utcnow().isoformat()
                    )
                )

                await c.execute(
                    "INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                    (
                        m.from_user.id,
                        -server[5],
                        "هزینه ساخت تانل",
                        datetime.utcnow().isoformat()
                    )
                )

                await c.commit()

        await m.answer(
            f"✅ تانل ساخته شد\n"
            f"پلن: {server[0]}\n"
            f"IP تحویلی: {server[4]}\n"
            f"پورت: {port}\n"
            f"IP مقصد: {ip}\n"
            f"پورت مقصد: {port}\n"
            f"هزینه ساخت: {money(server[5])}",
            reply_markup=await user_reply_kb()
        )

    except Exception:
        logging.exception("create tunnel failed")
        await m.answer(
            "❌ ساخت تانل ناموفق بود. از مدیر بخواهید لاگ سرور را بررسی کند."
        )

    finally:
        await state.clear()

class Sanaei:
    def __init__(self,base,user,password):
        self.base=base.rstrip("/")
        self.user=user; self.password=password
        self.session=None
    async def __aenter__(self):
        timeout=aiohttp.ClientTimeout(total=25)
        connector=aiohttp.TCPConnector(ssl=VERIFY_PANEL_SSL)
        self.session=aiohttp.ClientSession(timeout=timeout,connector=connector)
        async with self.session.post(self.base+"/login",json={"username":self.user,"password":self.password}) as r:
            data=await r.json(content_type=None)
            if r.status>=400 or data.get("success") is False: raise RuntimeError("Sanaei login failed: "+str(data))
        return self
    async def __aexit__(self,*args): await self.session.close()
    async def request(self,method,path,**kw):
        async with self.session.request(method,self.base+"/panel/api/inbounds"+path,**kw) as r:
            data=await r.json(content_type=None)
            if r.status>=400 or data.get("success") is False: raise RuntimeError(str(data))
            return data
    async def create_tunnel(self,port,ip,name,template_id,total=0):
        inbounds = await self.list_inbounds()

        template = next(
            (x for x in inbounds if int(x.get("id", -1)) == int(template_id)),
            None
        )

        if not template:
            raise RuntimeError("Template inbound not found")

        raw_settings = template.get("settings")
        settings = json.loads(raw_settings) if isinstance(raw_settings, str) else dict(raw_settings or {})

        settings["address"] = ip
        settings["port"] = port

        payload = {
            "enable": True,
            "remark": name,
            "listen": template.get("listen", ""),
            "port": port,
            "protocol": template["protocol"],
            "expiryTime": 0,
            "total": int(total),
            "settings": json.dumps(settings, separators=(",", ":")),
            "streamSettings": template.get("streamSettings", ""),
            "sniffing": template.get("sniffing", "")
        }

        data = await self.request("POST", "/add", json=payload)
        obj = data.get("obj")

        if isinstance(obj, dict) and obj.get("id") is not None:
            return obj["id"]

        listing = await self.request("GET", "/list")
        for item in listing.get("obj") or []:
            if item.get("remark") == name:
                return item["id"]

        raise RuntimeError("Inbound created but ID not returned/found")
    async def set_enable(self,iid,enabled):
        return await self.request("POST",f"/setEnable/{iid}",json={"enable":enabled})
    async def list_inbounds(self):
        return (await self.request("GET","/list")).get("obj") or []
    async def update_tunnel(self, iid, new_port=None, new_ip=None):
        inbound = next(
            (x for x in await self.list_inbounds()
             if int(x.get("id", -1)) == int(iid)),
            None
        )
        if not inbound:
            raise RuntimeError("Inbound not found")

        raw = inbound.get("settings")
        settings = json.loads(raw) if isinstance(raw, str) else dict(raw or {})

        if new_ip is not None:
            settings["address"] = new_ip
        if new_port is not None:
            settings["port"] = int(new_port)

        payload = {
            "enable": inbound.get("enable", True),
            "remark": inbound.get("remark", ""),
            "listen": inbound.get("listen", ""),
            "port": int(new_port) if new_port is not None else int(inbound["port"]),
            "protocol": inbound["protocol"],
            "expiryTime": inbound.get("expiryTime", 0),
            "total": inbound.get("total", 0),
            "settings": json.dumps(settings, separators=(",", ":")),
            "streamSettings": inbound.get("streamSettings", ""),
            "sniffing": inbound.get("sniffing", "")
        }

        return await self.request("POST", f"/update/{iid}", json=payload)

    async def delete_tunnel(self, iid):
        return await self.request("POST", f"/del/{iid}")

async def topup_start(q,state):
    await state.set_state(Topup.amount)
    await edit_or_send(q, "💳 مبلغ شارژ را به تومان وارد کنید:\n\nحداقل مبلغ: ۱٬۰۰۰ تومان", reply_markup=flow_cancel_kb())
    await q.answer()

@dp.callback_query(F.data=="topup")
async def topup(q:CallbackQuery,state:FSMContext): await topup_start(q,state)

@dp.message(Topup.amount)
async def topup_amount(m:Message,state:FSMContext):
    if not m.text or not m.text.isdigit() or int(m.text)<1000: return await m.answer("❌ مبلغ معتبر (حداقل ۱۰۰۰ تومان) وارد کنید.", reply_markup=flow_cancel_kb())
    await state.update_data(amount=int(m.text))
    async with db() as c:
        methods = await (await c.execute("SELECT title,card_number,owner FROM payment_methods WHERE active=1 ORDER BY id")).fetchall()
    if not methods:
        return await m.answer("⚠️ در حال حاضر روش پرداختی ثبت نشده است. با پشتیبانی تماس بگیرید.", reply_markup=flow_cancel_kb())
    payment_text = "\n\n".join(
        f"💳 <b>{x[0]}</b>\nشماره کارت: <code>{x[1]}</code>" + (f"\nبه نام: {x[2]}" if x[2] else "")
        for x in methods)
    await state.set_state(Topup.receipt)
    await m.answer(f"{payment_text}\n\n💰 مبلغ: {money(int(m.text))}\nپس از واریز، تصویر رسید را ارسال کنید.", reply_markup=flow_cancel_kb(), parse_mode="HTML")

@dp.message(Topup.receipt)
async def topup_receipt(m:Message,state:FSMContext):
    if not m.photo: return await m.answer("❌ لطفاً تصویر رسید را به‌صورت عکس ارسال کنید.", reply_markup=flow_cancel_kb())
    d=await state.get_data()
    async with db() as c:
        cur=await c.execute("INSERT INTO topups(user_id,amount,receipt_id,created_at) VALUES(?,?,?,?)",(m.from_user.id,d["amount"],m.photo[-1].file_id,datetime.utcnow().isoformat()))
        tid=cur.lastrowid; await c.commit()
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ تأیید",callback_data=f"approve:{tid}"),InlineKeyboardButton(text="❌ رد",callback_data=f"reject:{tid}")]])
    await m.bot.send_photo(ADMIN_ID,m.photo[-1].file_id,caption=f"درخواست شارژ #{tid}\nکاربر: {m.from_user.id}\nمبلغ: {money(d['amount'])}",reply_markup=kb)
    await m.answer("✅ رسید برای مدیر ارسال شد. پس از بررسی، نتیجه اعلام می‌شود.", reply_markup=await user_reply_kb())
    await state.clear()

@dp.callback_query(F.data.startswith(("approve:","reject:")))
async def review_topup(q:CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.",show_alert=True)
    action,tid=q.data.split(":"); tid=int(tid)
    async with db() as c:
        row=await (await c.execute("SELECT user_id,amount,status FROM topups WHERE id=?",(tid,))).fetchone()
        if not row or row[2]!="pending": return await q.answer("این درخواست قبلاً بررسی شده.",show_alert=True)
        status="approved" if action=="approve" else "rejected"
        await c.execute("UPDATE topups SET status=? WHERE id=?",(status,tid))
        if status=="approved":
            await c.execute("UPDATE users SET balance=balance+?,warned=0 WHERE id=?",(row[1],row[0]))
            await c.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",(row[0],row[1],"شارژ کارت به کارت",datetime.utcnow().isoformat()))
        await c.commit()
    await q.message.edit_caption((q.message.caption or "")+f"\n\nنتیجه: {status}")
    await q.bot.send_message(row[0],"✅ شارژ حساب شما تأیید شد." if action=="approve" else "❌ رسید شارژ شما رد شد.")
    await q.answer("انجام شد.")

@dp.callback_query(F.data=="usage")
async def usage(q:CallbackQuery):
    async with db() as c:
        rows=await (await c.execute("SELECT s.port,s.dest_ip,s.total_bytes,s.enabled,v.name FROM services s JOIN servers v ON v.id=s.server_id WHERE s.user_id=?",(q.from_user.id,))).fetchall()
    if not rows: msg="هنوز سرویسی ندارید."
    else:
        msg="📊 مصرف سرویس‌های شما:\n"
        for r in rows: msg+=f"\n{r[4]} | پورت {r[0]} | {r[1]}\nمصرف: {r[2]/1024**3:.3f} GB | {'فعال' if r[3] else 'غیرفعال'}\n"
    await edit_or_send(q, msg, reply_markup=main_kb()); await q.answer()

@dp.callback_query(F.data=="services")
async def services(q:CallbackQuery):
    async with db() as c:
        rows = await (await c.execute(
            """SELECT id,name,port,dest_ip,enabled,total_bytes
            FROM services WHERE user_id=? ORDER BY id DESC""",
            (q.from_user.id,)
        )).fetchall()

    if not rows:
        await edit_or_send(q, "📭 هنوز سرویسی ندارید.", reply_markup=main_kb())
        await q.answer()
        return

    buttons = []
    for r in rows:
        status = "🟢" if r[4] else "🔴"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {r[1]} | پورت {r[2]}",
                callback_data=f"svc:{r[0]}"
            )
        ])

    await edit_or_send(q, 
        "📡 سرویس‌های شما\n\nبرای مدیریت هر سرویس، روی آن بزنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await q.answer()

@dp.callback_query(F.data.startswith("svc:"))
async def service_menu(q: CallbackQuery):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            """SELECT id,name,port,dest_ip,enabled,total_bytes
            FROM services WHERE id=? AND user_id=?""",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        await q.answer("سرویس پیدا نشد.", show_alert=True)
        return

    status = "🟢 فعال" if row[4] else "🔴 غیرفعال"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"svcdel:{sid}")],
        [InlineKeyboardButton(text="🌐 تغییر IP مقصد", callback_data=f"svcipt:{sid}")],
        [InlineKeyboardButton(text="🔌 تغییر پورت مقصد", callback_data=f"svcport:{sid}")],
        [InlineKeyboardButton(text="📊 مشاهده حجم مصرفی", callback_data=f"svcusage:{sid}")],
        [InlineKeyboardButton(text="↩️ بازگشت", callback_data="services")]
    ])

    await edit_or_send(q, 
        f"📡 مدیریت سرویس: {row[1]}\n\n"
        f"وضعیت: {status}\n"
        f"پورت: {row[2]}\n"
        f"IP مقصد: {row[3]}\n"
        f"مصرف کل: {row[5] / 1024**3:.3f} GB",
        reply_markup=kb
    )
    await q.answer()

@dp.callback_query(F.data.startswith("svcusage:"))
async def service_usage(q: CallbackQuery):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            """SELECT name,port,dest_ip,total_bytes,enabled
            FROM services WHERE id=? AND user_id=?""",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        await q.answer("سرویس پیدا نشد.", show_alert=True)
        return

    await edit_or_send(q, 
        f"📊 گزارش مصرف سرویس\n\n"
        f"📡 نام: {row[0]}\n"
        f"🔌 پورت: {row[1]}\n"
        f"🌐 IP مقصد: {row[2]}\n"
        f"📥 مصرف ثبت‌شده: {row[3] / 1024**3:.4f} GB\n"
        f"⚙️ وضعیت: {'فعال 🟢' if row[4] else 'غیرفعال 🔴'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ بازگشت به مدیریت سرویس", callback_data=f"svc:{sid}")],
            [InlineKeyboardButton(text="📡 فهرست سرویس‌ها", callback_data="services")]
        ])
    )
    await q.answer()

@dp.callback_query(F.data.startswith("svcipt:"))
async def service_change_ip(q: CallbackQuery, state: FSMContext):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            "SELECT id FROM services WHERE id=? AND user_id=?",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        await q.answer("سرویس پیدا نشد.", show_alert=True)
        return

    await state.update_data(service_id=sid)
    await state.set_state(ServiceFlow.change_ip)
    await edit_or_send(q, "🌐 IP مقصد جدید را وارد کنید:")
    await q.answer()


@dp.callback_query(F.data.startswith("svcport:"))
async def service_change_port(q: CallbackQuery, state: FSMContext):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            "SELECT id FROM services WHERE id=? AND user_id=?",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        await q.answer("سرویس پیدا نشد.", show_alert=True)
        return

    await state.update_data(service_id=sid)
    await state.set_state(ServiceFlow.change_port)
    await edit_or_send(q, "🔌 پورت مقصد جدید را وارد کنید (۱ تا ۶۵۵۳۵):")
    await q.answer()

@dp.message(ServiceFlow.change_ip)
async def save_service_ip(m: Message, state: FSMContext):
    ip = m.text.strip()

    import ipaddress
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return await m.answer("❌ آدرس IP معتبر نیست. دوباره وارد کنید:")

    data = await state.get_data()
    sid = data["service_id"]

    async with db() as c:
        row = await (await c.execute(
            """SELECT s.server_id,s.inbound_id
            FROM services s WHERE s.id=? AND s.user_id=?""",
            (sid, m.from_user.id)
        )).fetchone()
        if not row:
            await state.clear()
            return await m.answer("سرویس پیدا نشد.")

        server = await (await c.execute(
            "SELECT url,username,password FROM servers WHERE id=?",
            (row[0],)
        )).fetchone()

    try:
        async with Sanaei(server[0], server[1], server[2]) as api:
            await api.update_tunnel(row[1], new_ip=ip)

        async with db() as c:
            await c.execute(
                "UPDATE services SET dest_ip=? WHERE id=? AND user_id=?",
                (ip, sid, m.from_user.id)
            )
            await c.commit()

        await m.answer("✅ IP مقصد با موفقیت تغییر کرد.")
    except Exception:
        logging.exception("change service IP failed")
        await m.answer("❌ تغییر IP انجام نشد. لاگ ربات را بررسی کنید.")
    finally:
        await state.clear()


@dp.message(ServiceFlow.change_port)
async def save_service_port(m: Message, state: FSMContext):
    try:
        port = int(m.text.strip())
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        return await m.answer("❌ پورت باید عددی بین ۱ تا ۶۵۵۳۵ باشد.")

    data = await state.get_data()
    sid = data["service_id"]

    async with db() as c:
        row = await (await c.execute(
            """SELECT s.server_id,s.inbound_id
            FROM services s WHERE s.id=? AND s.user_id=?""",
            (sid, m.from_user.id)
        )).fetchone()
        if not row:
            await state.clear()
            return await m.answer("سرویس پیدا نشد.")

        server = await (await c.execute(
            "SELECT url,username,password FROM servers WHERE id=?",
            (row[0],)
        )).fetchone()

    try:
        async with Sanaei(server[0], server[1], server[2]) as api:
            await api.update_tunnel(row[1], new_port=port)

        async with db() as c:
            await c.execute(
                "UPDATE services SET port=? WHERE id=? AND user_id=?",
                (port, sid, m.from_user.id)
            )
            await c.commit()

        await m.answer("✅ پورت مقصد با موفقیت تغییر کرد.")
    except Exception:
        logging.exception("change service port failed")
        await m.answer("❌ تغییر پورت انجام نشد. لاگ ربات را بررسی کنید.")
    finally:
        await state.clear()
@dp.callback_query(F.data.startswith("svcdel:"))
async def service_delete_confirm(q: CallbackQuery):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            "SELECT name FROM services WHERE id=? AND user_id=?",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        return await q.answer("سرویس پیدا نشد.", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="❌ بله، حذف شود",
                callback_data=f"svcdeleteyes:{sid}"
            ),
            InlineKeyboardButton(
                text="🔙 انصراف",
                callback_data=f"svc:{sid}"
            )
        ]
    ])

    await edit_or_send(q, 
        f"⚠️ آیا مطمئن هستید سرویس «{row[0]}» حذف شود؟\n"
        "با حذف، اتصال این سرویس از پنل نیز پاک خواهد شد.",
        reply_markup=kb
    )
    await q.answer()
@dp.callback_query(F.data.startswith("svcdeleteyes:"))
async def service_delete_yes(q: CallbackQuery):
    sid = int(q.data.split(":")[1])

    async with db() as c:
        row = await (await c.execute(
            """SELECT s.server_id, s.inbound_id, v.url, v.username,
                      v.password, v.template_inbound_id
               FROM services s
               JOIN servers v ON v.id = s.server_id
               WHERE s.id=? AND s.user_id=?""",
            (sid, q.from_user.id)
        )).fetchone()

    if not row:
        await q.answer("سرویس پیدا نشد.", show_alert=True)
        return

    server_id, inbound_id, url, username, password, template_id = row

    if int(inbound_id) == int(template_id):
        await q.answer("حذف سرویس قالب مجاز نیست.", show_alert=True)
        return

    try:
        async with Sanaei(url, username, password) as api:
            await api.delete_tunnel(inbound_id)

            remaining = await api.list_inbounds()
            still_exists = any(
                int(x.get("id", -1)) == int(inbound_id)
                for x in remaining
            )

            if still_exists:
                raise RuntimeError("Inbound still exists after delete")

        async with db() as c:
            await c.execute(
                "DELETE FROM services WHERE id=? AND user_id=?",
                (sid, q.from_user.id)
            )
            await c.commit()

        await edit_or_send(q, 
            "✅ سرویس با موفقیت از پنل و فهرست شما حذف شد."
        )

    except Exception:
        logging.exception("delete service failed")
        await edit_or_send(q, 
            "❌ حذف سرویس انجام نشد. لطفاً لاگ ربات را بررسی کنید."
        )

    await q.answer()

@dp.callback_query(F.data.startswith("a_"))
async def admin_actions(q:CallbackQuery,state:FSMContext):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.",show_alert=True)
    act=q.data[2:]
    if act == "users":
        await show_users_page(q, 0)
        return
    if act == "userlabels":
        rows=[]
        for key, default in USER_BUTTONS.items():
            rows.append([InlineKeyboardButton(text=f"✏️ {await setting(f'user_btn_{key}', default)}", callback_data=f"editlabel:{key}")])
        rows.append([InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin_home")])
        await edit_or_send(q, "🎨 <b>ویرایش عنوان دکمه‌های کاربر</b>\nدکمه موردنظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
        await q.answer()
        return
    prompts={"addserver":"نام سرور/پلن را وارد کنید:","warning":"حد هشدار موجودی به تومان:","credit":"آیدی عددی کاربر را وارد کنید:","debit":"آیدی عددی کاربر را وارد کنید:"}
    if act=="payments":
        async with db() as c:
            methods = await (await c.execute("SELECT id,title,card_number,owner,active FROM payment_methods ORDER BY id")).fetchall()
        buttons = [[InlineKeyboardButton(text=f"{'🟢' if r[4] else '🔴'} {r[1]} · {r[2]}", callback_data=f"paytoggle:{r[0]}"),
                    InlineKeyboardButton(text="🗑", callback_data=f"paydelete:{r[0]}")] for r in methods]
        buttons.append([InlineKeyboardButton(text="➕ افزودن روش پرداخت", callback_data="payadd")])
        buttons.append([InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="admin_home")])
        await edit_or_send(q, "💳 مدیریت روش‌های پرداخت\\nبرای فعال/غیرفعال کردن روی عنوان روش بزنید.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await q.answer(); return
    if act=="servers":
        async with db() as c:
            rows=await (await c.execute(
                "SELECT id,name,url,create_price,gb_price,active,template_inbound_id,test_enabled FROM servers"
            )).fetchall()
        if not rows:
            await edit_or_send(q, "📭 سروری ثبت نشده است.", reply_markup=admin_back_kb())
            await q.answer()
            return
        for r in rows:
            template_text = f"🧩 اینباند نمونه: {r[6]}" if r[6] else "🧩 اینباند نمونه: انتخاب نشده"
            kb=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔌 بررسی اتصال به پنل",callback_data=f"testserver:{r[0]}")],
                [InlineKeyboardButton(text=template_text,callback_data=f"templates:{r[0]}")],
                [InlineKeyboardButton(text=("🧪 خاموش کردن تست" if r[7] else "🧪 روشن کردن تست"),callback_data=f"togtest:{r[0]}")],
                [
                    InlineKeyboardButton(text="حذف",callback_data=f"delserver:{r[0]}"),
                    InlineKeyboardButton(text="فعال/غیرفعال",callback_data=f"togserver:{r[0]}")
                ],
                [InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="admin_home")]
            ])
            await q.message.answer(
                f"#{r[0]} {r[1]}\n{r[2]}\nساخت: {money(r[3])} | هر گیگ: {money(r[4])}\nفعال: {bool(r[5])} | تست رایگان: {bool(r[7])}",
                reply_markup=kb
            )
        await q.answer(); return
    if act=="topups":
        async with db() as c: rows=await (await c.execute("SELECT id,user_id,amount FROM topups WHERE status='pending'")).fetchall()
        text = ("🧾 <b>رسیدهای در انتظار بررسی</b>\n\n" +
                "\n".join([f"▫️ #{r[0]} | کاربر: <code>{r[1]}</code> | مبلغ: {money(r[2])}" for r in rows])
                if rows else "📭 <b>درخواستی در انتظار نیست.</b>\n\nدر حال حاضر رسید تأییدنشده‌ای وجود ندارد.")
        await edit_or_send(q, text, reply_markup=admin_back_kb(), parse_mode="HTML")
        await q.answer(); return
    if act not in prompts: return await q.answer()
    await state.update_data(admin_action=act)
    states={"addserver":AdminFlow.server_name,"warning":AdminFlow.set_warning,"credit":AdminFlow.credit_user,"debit":AdminFlow.debit_user}
    await state.set_state(states[act]); await edit_or_send(q, "🛠 " + prompts[act], reply_markup=flow_cancel_kb(is_admin_flow=True)); await q.answer()

@dp.callback_query(F.data == "a_users")
async def admin_users(q: CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    await show_users_page(q, 0)

async def show_users_page(q, page):
    per_page = 8
    async with db() as c:
        total = (await (await c.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        rows = await (await c.execute("SELECT id,balance,blocked FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (per_page, page*per_page))).fetchall()
    buttons = [[InlineKeyboardButton(text=f"{'⛔️' if b else '🟢'} {uid} · {money(balance)}", callback_data=f"userdetail:{uid}:{page}")] for uid,balance,b in rows]
    nav=[]
    if page: nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"userpage:{page-1}"))
    if (page+1)*per_page < total: nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"userpage:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin_home")])
    await edit_or_send(q, f"👥 <b>کاربران ربات</b>\nتعداد کل: <b>{total}</b>\n\nبرای مشاهده جزئیات، کاربر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await q.answer()

@dp.callback_query(F.data.startswith("userpage:"))
async def user_page(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    await show_users_page(q, int(q.data.split(":")[1]))

async def render_user_detail(q, uid, page):
    async with db() as c:
        row=await (await c.execute("SELECT balance,blocked FROM users WHERE id=?",(uid,))).fetchone()
        count=(await (await c.execute("SELECT COUNT(*) FROM services WHERE user_id=?",(uid,))).fetchone())[0]
    if not row: return await q.answer("کاربر پیدا نشد.", show_alert=True)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 رفع مسدودی" if row[1] else "⛔️ مسدود کردن کاربر",callback_data=f"userblock:{uid}:{page}")],
        [InlineKeyboardButton(text="💳 مشاهده موجودی",callback_data=f"userbalance:{uid}:{page}")],
        [InlineKeyboardButton(text="🧾 مشاهده سفارش‌ها",callback_data=f"userorders:{uid}:{page}")],
        [InlineKeyboardButton(text="➕ افزایش موجودی",callback_data=f"usercredit:{uid}:{page}")],
        [InlineKeyboardButton(text="➖ کاهش موجودی",callback_data=f"userdebit:{uid}:{page}")],
        [InlineKeyboardButton(text="↩️ بازگشت به کاربران",callback_data=f"userpage:{page}")],
        [InlineKeyboardButton(text="🏠 پنل مدیریت",callback_data="admin_home")]])
    await edit_or_send(q,f"👤 <b>مدیریت کاربر</b>\nشناسه: <code>{uid}</code>\nوضعیت: {'⛔️ مسدود' if row[1] else '🟢 فعال'}\nموجودی: <b>{money(row[0])}</b>\nتعداد سرویس‌ها: <b>{count}</b>",reply_markup=kb,parse_mode="HTML")

@dp.callback_query(F.data.startswith("userdetail:"))
async def user_detail(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    _,uid,page=q.data.split(":"); await render_user_detail(q,int(uid),int(page)); await q.answer()

@dp.callback_query(F.data.startswith("userblock:"))
async def user_block(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    _,uid,page=q.data.split(":")
    async with db() as c:
        await c.execute("UPDATE users SET blocked=CASE blocked WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(int(uid),)); await c.commit()
    await q.answer("وضعیت دسترسی تغییر کرد."); await render_user_detail(q,int(uid),int(page))

@dp.callback_query(F.data.startswith("userbalance:"))
async def user_balance(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    _,uid,page=q.data.split(":")
    async with db() as c: row=await (await c.execute("SELECT balance FROM users WHERE id=?",(int(uid),))).fetchone()
    await edit_or_send(q,f"💳 موجودی کاربر <code>{uid}</code>: <b>{money(row[0])}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ بازگشت",callback_data=f"userdetail:{uid}:{page}")]]),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith("userorders:"))
async def user_orders(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    _,uid,page=q.data.split(":")
    async with db() as c:
        sv=await (await c.execute("SELECT id,name,port,dest_ip,enabled FROM services WHERE user_id=? ORDER BY id DESC LIMIT 10",(int(uid),))).fetchall()
        tp=await (await c.execute("SELECT id,amount,status FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 10",(int(uid),))).fetchall()
    lines=["🧾 <b>سفارش‌ها و تراکنش‌های کاربر</b>",f"شناسه: <code>{uid}</code>","","📡 <b>سرویس‌ها</b>"]
    lines += [f"• #{r[0]} {r[1]} | پورت {r[2]} | {'🟢' if r[4] else '🔴'}" for r in sv] or ["• موردی ثبت نشده"]
    lines += ["","💳 <b>درخواست‌های شارژ</b>"]
    lines += [f"• #{r[0]} | {money(r[1])} | {r[2]}" for r in tp] or ["• موردی ثبت نشده"]
    await edit_or_send(q,"\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ بازگشت",callback_data=f"userdetail:{uid}:{page}")]]),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith(("usercredit:","userdebit:")))
async def user_adjust(q,state):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    action,uid,page=q.data.split(":")
    await state.update_data(target=int(uid),user_page=int(page),selected_user=True)
    await state.set_state(AdminFlow.credit_amount if action=="usercredit" else AdminFlow.debit_amount)
    await edit_or_send(q,f"💰 مبلغ {'افزایش' if action=='usercredit' else 'کاهش'} موجودی کاربر <code>{uid}</code> را وارد کنید:",reply_markup=flow_cancel_kb(is_admin_flow=True),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data == "a_userlabels")
async def user_labels_menu(q):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    rows=[]
    for key,default in USER_BUTTONS.items(): rows.append([InlineKeyboardButton(text=f"✏️ {await setting(f'user_btn_{key}',default)}",callback_data=f"editlabel:{key}")])
    rows.append([InlineKeyboardButton(text="↩️ پنل مدیریت",callback_data="admin_home")])
    await edit_or_send(q,"🎨 <b>ویرایش عنوان دکمه‌های کاربر</b>\nدکمه موردنظر را انتخاب کنید:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),parse_mode="HTML"); await q.answer()

@dp.callback_query(F.data.startswith("editlabel:"))
async def edit_label_start(q,state):
    if not is_admin(q.from_user.id): return await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
    key=q.data.split(":")[1]
    if key not in USER_BUTTONS: return await q.answer("گزینه نامعتبر است.",show_alert=True)
    await state.update_data(edit_label_key=key); await state.set_state(AdminFlow.edit_button_label)
    await edit_or_send(q,"✏️ عنوان جدید را ارسال کنید (حداکثر ۵۰ نویسه):",reply_markup=flow_cancel_kb(is_admin_flow=True)); await q.answer()

@dp.message(AdminFlow.edit_button_label)
async def edit_label_save(m,state):
    label=(m.text or "").strip()
    if not label or len(label)>50: return await m.answer("❌ عنوان باید بین ۱ تا ۵۰ نویسه باشد.",reply_markup=flow_cancel_kb(is_admin_flow=True))
    data=await state.get_data(); await set_setting(f"user_btn_{data['edit_label_key']}",label); await state.clear()
    await m.answer("✅ عنوان دکمه ذخیره شد. با /start منوی جدید را دریافت می‌کنید.",reply_markup=admin_kb())

@dp.message(StateFilter(None))
async def user_reply_menu_router(m: Message, state):
    if not m.text: return
    for key,default in USER_BUTTONS.items():
        if m.text.strip()!=await setting(f"user_btn_{key}",default): continue
        if key=="buy":
            async with db() as c: rows=await (await c.execute("SELECT id,name,create_price,gb_price FROM servers WHERE active=1")).fetchall()
            if not rows: return await m.answer("📭 در حال حاضر پلن فعالی وجود ندارد.",reply_markup=await user_reply_kb())
            kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🌍 {r[1]} · ساخت {money(r[2])} · هر گیگ {money(r[3])}",callback_data=f"choose:{r[0]}")] for r in rows]+[[InlineKeyboardButton(text="✖️ انصراف",callback_data="cancel_user_flow")]])
            return await m.answer("🛒 <b>انتخاب پلن</b>\nیکی از پلن‌ها را انتخاب کنید:",reply_markup=kb,parse_mode="HTML")
        if key=="test":
            async with db() as c:
                rows = await (await c.execute("""
                    SELECT s.id,s.name FROM servers s WHERE s.active=1 AND s.test_enabled=1
                    AND NOT EXISTS(SELECT 1 FROM test_history h WHERE h.user_id=? AND h.server_id=s.id)
                    ORDER BY s.id
                """, (m.from_user.id,))).fetchall()
            if not rows:
                return await m.answer("🧪 فعلاً سرور تست قابل استفاده‌ای ندارید یا قبلاً تست این سرورها را دریافت کرده‌اید.", reply_markup=await user_reply_kb())
            kb = [[InlineKeyboardButton(text=f"🌐 {r[1]}", callback_data=f"testchoose:{r[0]}")] for r in rows]
            kb.append([InlineKeyboardButton(text="✖️ لغو", callback_data="cancel_user_flow")])
            return await m.answer("🧪 سرور تست رایگان را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        if key=="topup":
            await state.set_state(Topup.amount); return await m.answer("💳 مبلغ شارژ را به تومان وارد کنید:",reply_markup=flow_cancel_kb())
        if key=="balance":
            await ensure_user(m.from_user.id)
            async with db() as c: row=await (await c.execute("SELECT balance FROM users WHERE id=?",(m.from_user.id,))).fetchone()
            return await m.answer(f"💳 <b>موجودی شما</b>\n\n💰 {money(row[0])}",reply_markup=await user_reply_kb(),parse_mode="HTML")
        if key=="usage":
            async with db() as c: rows=await (await c.execute("SELECT s.port,s.dest_ip,s.total_bytes,s.enabled,v.name FROM services s JOIN servers v ON v.id=s.server_id WHERE s.user_id=?",(m.from_user.id,))).fetchall()
            lines=["📊 <b>گزارش مصرف شما</b>"]+[f"• {r[4]} | پورت {r[0]} | {r[1]}\n  مصرف: {r[2]/1024**3:.3f} GB | {'🟢 فعال' if r[3] else '🔴 غیرفعال'}" for r in rows]
            return await m.answer("\n\n".join(lines),reply_markup=await user_reply_kb(),parse_mode="HTML")
        if key=="services":
            async with db() as c: rows=await (await c.execute("SELECT id,name,port,enabled FROM services WHERE user_id=? ORDER BY id DESC",(m.from_user.id,))).fetchall()
            if not rows: return await m.answer("📭 هنوز سرویسی ندارید.",reply_markup=await user_reply_kb())
            kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{'🟢' if r[3] else '🔴'} {r[1]} · پورت {r[2]}",callback_data=f"svc:{r[0]}")] for r in rows])
            return await m.answer("📡 سرویس‌های شما · برای مدیریت انتخاب کنید:",reply_markup=kb)


@dp.callback_query(F.data == "a_payments")
async def payments_menu(q: CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.", show_alert=True)
    async with db() as c:
        rows = await (await c.execute("SELECT id,title,card_number,active FROM payment_methods ORDER BY id")).fetchall()
    kb = [[InlineKeyboardButton(text=f"{'🟢' if r[3] else '🔴'} {r[1]} · {r[2]}", callback_data=f"paytoggle:{r[0]}"),
           InlineKeyboardButton(text="🗑", callback_data=f"paydelete:{r[0]}")] for r in rows]
    kb += [[InlineKeyboardButton(text="➕ افزودن روش پرداخت", callback_data="payadd")],
           [InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="admin_home")]]
    await edit_or_send(q, "💳 روش‌های پرداخت (روی ردیف برای تغییر وضعیت بزنید):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await q.answer()

@dp.callback_query(F.data == "payadd")
async def pay_add(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.", show_alert=True)
    await state.set_state(PaymentFlow.title)
    await edit_or_send(q, "عنوان روش پرداخت را وارد کنید:", reply_markup=flow_cancel_kb(True))
    await q.answer()

@dp.message(PaymentFlow.title)
async def pay_title(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not title: return await m.answer("عنوان معتبر وارد کنید.", reply_markup=flow_cancel_kb(True))
    await state.update_data(pay_title=title)
    await state.set_state(PaymentFlow.card)
    await m.answer("شماره کارت یا شناسه پرداخت را وارد کنید:", reply_markup=flow_cancel_kb(True))

@dp.message(PaymentFlow.card)
async def pay_card(m: Message, state: FSMContext):
    card = (m.text or "").strip()
    if len(card) < 4: return await m.answer("شماره/شناسه معتبر وارد کنید.", reply_markup=flow_cancel_kb(True))
    await state.update_data(pay_card=card)
    await state.set_state(PaymentFlow.owner)
    await m.answer("نام صاحب کارت را وارد کنید (در صورت عدم نیاز، - بفرستید):", reply_markup=flow_cancel_kb(True))

@dp.message(PaymentFlow.owner)
async def pay_owner(m: Message, state: FSMContext):
    d = await state.get_data()
    owner = (m.text or "").strip()
    if owner == "-": owner = ""
    async with db() as c:
        await c.execute("INSERT INTO payment_methods(title,card_number,owner) VALUES(?,?,?)",
                        (d["pay_title"], d["pay_card"], owner))
        await c.commit()
    await state.clear()
    await m.answer("✅ روش پرداخت اضافه شد.", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("paytoggle:"))
async def pay_toggle(q: CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.", show_alert=True)
    pid = int(q.data.split(":")[1])
    async with db() as c:
        await c.execute("UPDATE payment_methods SET active=1-active WHERE id=?", (pid,))
        await c.commit()
    await q.answer("وضعیت تغییر کرد.")
    await payments_menu(q)

@dp.callback_query(F.data.startswith("paydelete:"))
async def pay_delete(q: CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.", show_alert=True)
    pid = int(q.data.split(":")[1])
    async with db() as c:
        await c.execute("DELETE FROM payment_methods WHERE id=?", (pid,))
        await c.commit()
    await q.answer("روش حذف شد.")
    await payments_menu(q)

@dp.callback_query(F.data.startswith("togtest:"))
async def toggle_server_test(q: CallbackQuery):
    if not is_admin(q.from_user.id): return await q.answer("دسترسی ندارید.", show_alert=True)
    sid = int(q.data.split(":")[1])
    async with db() as c:
        await c.execute("UPDATE servers SET test_enabled=1-test_enabled WHERE id=?", (sid,))
        await c.commit()
    await q.answer("وضعیت تست رایگان تغییر کرد.")
    # Refresh the server list from the admin menu handler.
    await edit_or_send(q, "وضعیت تست سرور تغییر کرد. برای مشاهده فهرست، دوباره «لیست سرورها» را بزنید.")

@dp.callback_query(F.data == "test")
async def test_entry(q: CallbackQuery, state: FSMContext):
    async with db() as c:
        rows = await (await c.execute("""
            SELECT s.id,s.name FROM servers s
            WHERE s.active=1 AND s.test_enabled=1
            AND NOT EXISTS(SELECT 1 FROM test_history h WHERE h.user_id=? AND h.server_id=s.id)
            ORDER BY s.id
        """, (q.from_user.id,))).fetchall()
    if not rows:
        await edit_or_send(q, "🧪 در حال حاضر سرور تست قابل استفاده‌ای ندارید؛ یا قبلاً از تست این سرورها استفاده کرده‌اید.", reply_markup=main_kb())
    else:
        kb = [[InlineKeyboardButton(text=f"🌐 {r[1]}", callback_data=f"testchoose:{r[0]}")] for r in rows]
        kb.append([InlineKeyboardButton(text="✖️ لغو", callback_data="cancel_user_flow")])
        await edit_or_send(q, "🧪 سرور تست رایگان را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await q.answer()

@dp.callback_query(F.data.startswith("testchoose:"))
async def test_choose(q: CallbackQuery, state: FSMContext):
    sid = int(q.data.split(":")[1])
    async with db() as c:
        row = await (await c.execute("SELECT id,name FROM servers WHERE id=? AND active=1 AND test_enabled=1", (sid,))).fetchone()
    if not row: return await q.answer("این سرور تست در دسترس نیست.", show_alert=True)
    await state.update_data(test_server_id=sid)
    await state.set_state(TestFlow.dest_ip)
    await edit_or_send(q, "🌍 IP مقصد را وارد کنید؛ پورت به‌صورت خودکار انتخاب می‌شود. حجم تست: ۲ گیگابایت.", reply_markup=flow_cancel_kb())
    await q.answer()

@dp.message(TestFlow.dest_ip)
async def create_free_test(m: Message, state: FSMContext):
    try: ip = str(ipaddress.ip_address((m.text or "").strip()))
    except ValueError: return await m.answer("❌ IP معتبر وارد کنید.", reply_markup=flow_cancel_kb())
    d = await state.get_data()
    async with db() as c:
        server = await (await c.execute("SELECT name,url,username,password,delivery_ip,template_inbound_id FROM servers WHERE id=? AND active=1 AND test_enabled=1", (d["test_server_id"],))).fetchone()
        used = await (await c.execute("SELECT 1 FROM test_history WHERE user_id=? AND server_id=?", (m.from_user.id,d["test_server_id"]))).fetchone()
    if not server or not server[5]:
        await state.clear()
        return await m.answer("سرور تست یا اینباند نمونه در دسترس نیست.")
    if used:
        await state.clear()
        return await m.answer("شما قبلاً از تست این سرور استفاده کرده‌اید.")
    port = None
    try:
        async with Sanaei(server[1],server[2],server[3]) as api:
            port = await choose_free_port(api,d["test_server_id"])
            inbound_id = await api.create_tunnel(port,ip,f"test_{m.from_user.id}_{port}",server[5], total=2*1024**3)
        async with db() as c:
            await c.execute("INSERT INTO test_history(user_id,server_id,first_test_at) VALUES(?,?,?)",
                            (m.from_user.id,d["test_server_id"],datetime.utcnow().isoformat()))
            await c.execute("INSERT INTO test_services(user_id,server_id,inbound_id,port,dest_ip,created_at) VALUES(?,?,?,?,?,?)",
                            (m.from_user.id,d["test_server_id"],int(inbound_id),port,ip,datetime.utcnow().isoformat()))
            await c.commit()
        await state.clear()
        await m.answer(f"✅ تست رایگان ساخته شد.\\nسرور: {server[0]}\\nIP مقصد: {ip}\\nپورت: <code>{port}</code>\\nحجم مجاز: ۲ گیگابایت", parse_mode="HTML", reply_markup=await user_reply_kb())
        try: await m.bot.send_message(ADMIN_ID, f"🧪 تست رایگان ساخته شد\\nکاربر: <code>{m.from_user.id}</code>\\nسرور: {server[0]}\\nپورت: {port}", parse_mode="HTML")
        except Exception: pass
    except Exception as exc:
        logging.exception("free test creation failed")
        await state.clear()
        await m.answer(f"❌ ساخت تست ناموفق بود: {str(exc)[:250]}")

@dp.callback_query(F.data.startswith("templates:"))
async def choose_template(q: CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("دسترسی ندارید.", show_alert=True)

    sid = int(q.data.split(":")[1])

    async with db() as c:
        server = await (await c.execute(
            "SELECT name,url,username,password FROM servers WHERE id=?",
            (sid,)
        )).fetchone()

    if not server:
        return await q.answer("سرور پیدا نشد.", show_alert=True)

    await q.answer("در حال دریافت اینباندها...")

    try:
        async with Sanaei(server[1], server[2], server[3]) as api:
            inbounds = await api.list_inbounds()

        if not inbounds:
            return await edit_or_send(q, "هیچ اینباندی در این پنل پیدا نشد.")

        buttons = []
        for item in inbounds:
            iid = item.get("id")
            remark = item.get("remark") or "بدون نام"
            protocol = item.get("protocol") or "نامشخص"
            port = item.get("port") or "نامشخص"

            buttons.append([
                InlineKeyboardButton(
                    text=f"{remark} | {protocol} | پورت {port} | ID {iid}",
                    callback_data=f"settemplate:{sid}:{iid}"
                )
            ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await edit_or_send(q, 
            f"🧩 انتخاب اینباند نمونه برای سرور {server[0]}:",
            reply_markup=kb
        )

    except Exception:
        logging.exception("Template list failed for server %s", sid)
        await edit_or_send(q, "خطا در دریافت اینباندهای پنل.")


@dp.callback_query(F.data.startswith("settemplate:"))
async def set_template(q: CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("دسترسی ندارید.", show_alert=True)

    _, sid, iid = q.data.split(":")
    sid = int(sid)
    iid = int(iid)

    async with db() as c:
        server = await (await c.execute(
            "SELECT url,username,password FROM servers WHERE id=?",
            (sid,)
        )).fetchone()

    if not server:
        return await q.answer("سرور پیدا نشد.", show_alert=True)

    try:
        async with Sanaei(server[0], server[1], server[2]) as api:
            inbounds = await api.list_inbounds()

        selected = next(
            (x for x in inbounds if int(x.get("id", -1)) == iid),
            None
        )

        if not selected:
            return await q.answer("این اینباند دیگر وجود ندارد.", show_alert=True)

        async with db() as c:
            await c.execute(
                "UPDATE servers SET template_inbound_id=? WHERE id=?",
                (iid, sid)
            )
            await c.commit()

        await q.answer("اینباند نمونه ذخیره شد.")
        await edit_or_send(q, 
            f"✅ اینباند نمونه ذخیره شد.\n"
            f"نام: {selected.get('remark')}\n"
            f"پروتکل: {selected.get('protocol')}\n"
            f"پورت: {selected.get('port')}"
        )

    except Exception:
        logging.exception("Template selection failed for server %s", sid)
        await q.answer("خطا در ذخیره اینباند نمونه.", show_alert=True)
@dp.callback_query(F.data.startswith("testserver:"))
async def test_server_connection(q:CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("دسترسی ندارید.",show_alert=True)

    sid=int(q.data.split(":")[1])
    await q.answer("در حال بررسی اتصال...")
    async with db() as c:
        server=await (await c.execute(
            "SELECT name,url,username,password FROM servers WHERE id=?",(sid,)
        )).fetchone()

    if not server:
        return await edit_or_send(q, "سرور پیدا نشد.")

    try:
        started = time.perf_counter()
        async with Sanaei(server[1], server[2], server[3]) as api:
            inbounds = await api.list_inbounds()
        elapsed_ms = (time.perf_counter() - started) * 1000
        host = urlparse(server[1]).hostname or "نامشخص"
        enabled = sum(1 for item in inbounds if item.get("enable", True))
        report = (
            "╭━━━ 🖥️ <b>گزارش سلامت سرور</b> ━━━╮\n"
            f"│ 🟢 <b>تست اتصال:</b> موفق\n"
            f"│ 🌐 <b>سرور:</b> {server[0]}\n"
            f"│ 🔗 <b>هاست پنل:</b> <code>{host}</code>\n"
            f"│ ⚡ <b>زمان پاسخ API:</b> {elapsed_ms:.0f} ms\n"
            f"│ 📡 <b>کل Inboundها:</b> {len(inbounds)}\n"
            f"│ 🟢 <b>فعال:</b> {enabled} · 🔴 <b>غیرفعال:</b> {len(inbounds)-enabled}\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "ℹ️ CPU و RAM سیستم‌عامل از API پنل x-ui قابل دریافت نیست؛ برای نمایش دقیق آن‌ها باید دسترسی SSH یا Agent مانیتورینگ اضافه شود."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تست مجدد", callback_data=f"testserver:{sid}")],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل مدیریت", callback_data="admin_home")]
        ])
        await edit_or_send(q, report, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logging.exception("Sanaei connection test failed for server %s", sid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تلاش مجدد", callback_data=f"testserver:{sid}")],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل مدیریت", callback_data="admin_home")]
        ])
        await edit_or_send(q, f"╭━━━ 🖥️ <b>گزارش سلامت سرور</b> ━━━╮\n🔴 <b>تست اتصال:</b> ناموفق\n🌐 <b>سرور:</b> {server[0]}\n\nآدرس پنل، اطلاعات ورود و دسترسی شبکه را بررسی کنید.\n╰━━━━━━━━━━━━━━━━━━━━╯", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith(("delserver:","togserver:")))
async def server_toggle(q:CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("دسترسی ندارید.", show_alert=True)

    act, sid = q.data.split(":")
    sid = int(sid)

    async with db() as c:
        server = await (await c.execute(
            "SELECT name FROM servers WHERE id=?",
            (sid,)
        )).fetchone()

        if not server:
            return await q.answer("سرور پیدا نشد.", show_alert=True)

        if act == "delserver":
            services = await (await c.execute(
                "SELECT COUNT(*) FROM services WHERE server_id=? AND enabled=1",
                (sid,)
            )).fetchone()

            if services[0] > 0:
                return await q.answer(
                    "این سرور سرویس فعال مشتری دارد و قابل حذف نیست.",
                    show_alert=True
                )

            await c.execute(
                "DELETE FROM servers WHERE id=?",
                (sid,)
            )
            await c.commit()

            await q.answer("سرور حذف شد.")
            await edit_or_send(q, f"🗑 سرور «{server[0]}» حذف شد.")
            return

        await c.execute(
            "UPDATE servers SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (sid,)
        )
        await c.commit()

    await q.answer("وضعیت سرور به‌روزرسانی شد.")
    await edit_or_send(q, "وضعیت سرور تغییر کرد.")

@dp.message(AdminFlow.server_name)
async def sname(m:Message,state:FSMContext):
    await state.update_data(name=m.text); await state.set_state(AdminFlow.server_url); await m.answer("آدرس کامل پنل را وارد کنید، مثال https://host:port/basepath", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_url)
async def surl(m:Message,state:FSMContext):
    if not m.text.startswith("https://") and not m.text.startswith("http://"): return await m.answer("آدرس باید با http:// یا https:// شروع شود.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await state.update_data(url=m.text.rstrip("/")); await state.set_state(AdminFlow.server_username); await m.answer("نام کاربری پنل:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_username)
async def su(m:Message,state:FSMContext):
    await state.update_data(username=m.text); await state.set_state(AdminFlow.server_password); await m.answer("رمز عبور پنل:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_password)
async def sp(m:Message,state:FSMContext):
    await state.update_data(password=m.text); await state.set_state(AdminFlow.server_delivery_ip); await m.answer("IP تحویلی به مشتری:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_delivery_ip)
async def sd(m:Message,state:FSMContext):
    try: ip=str(ipaddress.ip_address(m.text.strip()))
    except: return await m.answer("IP معتبر وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await state.update_data(delivery_ip=ip); await state.set_state(AdminFlow.server_create_price); await m.answer("هزینه ساخت تانل به تومان:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_create_price)
async def scp(m:Message,state:FSMContext):
    if not m.text.isdigit(): return await m.answer("فقط عدد وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await state.update_data(create_price=int(m.text)); await state.set_state(AdminFlow.server_gb_price); await m.answer("قیمت هر گیگ مصرف به تومان:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.server_gb_price)
async def sgp(m:Message,state:FSMContext):
    if not m.text.isdigit(): return await m.answer("فقط عدد وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    d=await state.get_data()
    async with db() as c:
        await c.execute("INSERT INTO servers(name,url,username,password,delivery_ip,create_price,gb_price) VALUES(?,?,?,?,?,?,?)",(d["name"],d["url"],d["username"],d["password"],d["delivery_ip"],d["create_price"],int(m.text)))
        await c.commit()
    await state.clear(); await m.answer("✅ سرور اضافه شد.",reply_markup=admin_kb())

async def numeric_setting(m,state,key):
    if not m.text or not m.text.isdigit(): return await m.answer("فقط عدد وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await set_setting(key,m.text); await state.clear(); await m.answer("✅ ذخیره شد.",reply_markup=admin_kb())
@dp.message(AdminFlow.set_card)
async def setcard(m,state): await set_setting("card",m.text.strip()); await state.clear(); await m.answer("✅ شماره کارت ذخیره شد.", reply_markup=admin_kb())
@dp.message(AdminFlow.set_owner)
async def setowner(m,state): await set_setting("owner",m.text.strip()); await state.clear(); await m.answer("✅ نام صاحب کارت ذخیره شد.", reply_markup=admin_kb())
@dp.message(AdminFlow.set_warning)
async def setwarn(m,state): await numeric_setting(m,state,"warning")
@dp.message(AdminFlow.set_create_price)
async def setcp(m,state): await numeric_setting(m,state,"create_price")
@dp.message(AdminFlow.set_gb_price)
async def setgp(m,state): await numeric_setting(m,state,"gb_price")

@dp.message(AdminFlow.credit_user)
async def credituser(m,state):
    if not m.text.isdigit(): return await m.answer("آیدی عددی وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await state.update_data(target=int(m.text)); await state.set_state(AdminFlow.credit_amount); await m.answer("مبلغ شارژ:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.credit_amount)
async def creditamount(m,state):
    if not m.text.isdigit(): return await m.answer("مبلغ عددی وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    d=await state.get_data(); uid=d["target"]; amount=int(m.text); await ensure_user(uid)
    async with db() as c:
        await c.execute("UPDATE users SET balance=balance+?,warned=0 WHERE id=?",(amount,uid))
        await c.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",(uid,amount,"شارژ دستی",datetime.utcnow().isoformat())); await c.commit()
    await state.clear(); await m.answer("✅ شارژ انجام شد.", reply_markup=admin_kb())
    try: await m.bot.send_message(uid,f"حساب شما به مبلغ {money(amount)} شارژ شد.")
    except: pass
@dp.message(AdminFlow.debit_user)
async def debituser(m,state):
    if not m.text.isdigit(): return await m.answer("آیدی عددی وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    await state.update_data(target=int(m.text)); await state.set_state(AdminFlow.debit_amount); await m.answer("مبلغ کسر:", reply_markup=flow_cancel_kb(is_admin_flow=True))
@dp.message(AdminFlow.debit_amount)
async def debitamount(m,state):
    if not m.text.isdigit(): return await m.answer("مبلغ عددی وارد کنید.", reply_markup=flow_cancel_kb(is_admin_flow=True))
    d=await state.get_data(); uid=d["target"]; amount=int(m.text)
    async with db() as c:
        await c.execute("UPDATE users SET balance=MAX(0,balance-?) WHERE id=?",(amount,uid))
        await c.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",(uid,-amount,"کسر دستی",datetime.utcnow().isoformat())); await c.commit()
    await state.clear(); await m.answer("✅ کسر موجودی انجام شد.", reply_markup=admin_kb())

async def usage_worker(bot):
    while True:
        try:
            async with db() as c:
                rows=await (await c.execute("""SELECT s.id,s.user_id,s.server_id,s.inbound_id,s.last_bytes,s.enabled,v.url,v.username,v.password,v.gb_price
                FROM services s JOIN servers v ON v.id=s.server_id WHERE s.enabled=1""")).fetchall()
            by_server={}
            for r in rows: by_server.setdefault(r[2],[]).append(r)
            for sid,services in by_server.items():
                first=services[0]
                try:
                    async with Sanaei(first[6],first[7],first[8]) as api:
                        inbounds=await api.list_inbounds()
                    for s in services:
                        inbound=next((x for x in inbounds if int(x.get("id",-1))==int(s[3])),None)
                        if inbound is None: continue
                        # Prefer clientStats; otherwise inbound counters. For one inbound per customer.
                        stats=inbound.get("clientStats") or []
                        if stats:
                            current=sum(int(x.get("up",0))+int(x.get("down",0)) for x in stats)
                        else:
                            current=int(inbound.get("up",0))+int(inbound.get("down",0))
                        delta=max(0,current-int(s[4]))
                        if delta<=0: continue
                        charge=int((delta/(1024**3))*int(s[9]))
                        async with db() as c:
                            await c.execute("BEGIN IMMEDIATE")
                            user=await (await c.execute("SELECT balance FROM users WHERE id=?",(s[1],))).fetchone()
                            if not user: await c.rollback(); continue
                            if charge <= 0:
                                await c.execute("UPDATE services SET last_bytes=?,total_bytes=total_bytes+? WHERE id=?",
                                                (current,delta,s[0]))
                                await c.commit()
                            elif user[0] < charge:
                                    await c.execute("UPDATE users SET balance=0 WHERE id=?",(s[1],))
                                    await c.execute("UPDATE services SET last_bytes=?,total_bytes=total_bytes+? WHERE id=?",(current,delta,s[0]))
                                    await c.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",(s[1],-user[0],"مصرف تانل (توقف به علت اتمام اعتبار)",datetime.utcnow().isoformat()))
                                    await c.commit()
                                    async with Sanaei(first[6],first[7],first[8]) as api: await api.set_enable(s[3],False)
                                    async with db() as c2: await c2.execute("UPDATE services SET enabled=0 WHERE id=?",(s[0],)); await c2.commit()
                                    try: await bot.send_message(s[1],"⛔️ موجودی شما تمام شد و تانل غیرفعال شد. پس از شارژ، برای فعال‌سازی با پشتیبانی تماس بگیرید.")
                                    except: pass
                            else:
                                await c.execute("UPDATE users SET balance=balance-? WHERE id=?",(charge,s[1]))
                                await c.execute("UPDATE services SET last_bytes=?,total_bytes=total_bytes+? WHERE id=?",(current,delta,s[0]))
                                await c.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",(s[1],-charge,"هزینه مصرف",datetime.utcnow().isoformat()))
                                await c.commit()
                                try: await bot.send_message(s[1],f"📊 مصرف جدید ثبت شد.\nحجم: {delta/1024**2:.2f} MB\nهزینه: {money(charge)}\nموجودی: {money(user[0]-charge)}")
                                except: pass
                        # Low balance warning (threshold configurable globally)
                        threshold=int(await setting("warning",LOW_BALANCE_WARNING))
                        async with db() as c:
                            u=await (await c.execute("SELECT balance,warned FROM users WHERE id=?",(s[1],))).fetchone()
                            if u and u[0]<=threshold and not u[1]:
                                await c.execute("UPDATE users SET warned=1 WHERE id=?",(s[1],)); await c.commit()
                                try: await bot.send_message(s[1],f"⚠️ موجودی شما کم است: {money(u[0])}\nلطفاً برای جلوگیری از قطع تانل حساب خود را شارژ کنید.")
                                except: pass
                except Exception:
                    logging.exception("usage poll failed for server %s",sid)
        except Exception: logging.exception("usage worker error")
        await asyncio.sleep(max(1,USAGE_CHECK_MINUTES)*60)


async def test_usage_worker(bot):
    limit = 2 * 1024**3
    while True:
        try:
            async with db() as c:
                rows = await (await c.execute("""
                    SELECT t.id,t.user_id,t.server_id,t.inbound_id,t.last_bytes,t.total_bytes,
                           v.url,v.username,v.password,v.name
                    FROM test_services t JOIN servers v ON v.id=t.server_id
                    WHERE t.status='active'
                """)).fetchall()
            grouped = {}
            for row in rows: grouped.setdefault(row[2], []).append(row)
            for sid, items in grouped.items():
                first = items[0]
                try:
                    async with Sanaei(first[6],first[7],first[8]) as api:
                        inbounds = await api.list_inbounds()
                    for item in items:
                        inbound = next((x for x in inbounds if int(x.get("id",-1)) == int(item[3])),None)
                        if not inbound: continue
                        stats = inbound.get("clientStats") or []
                        current = sum(int(x.get("up",0))+int(x.get("down",0)) for x in stats) if stats else int(inbound.get("up",0))+int(inbound.get("down",0))
                        delta = max(0,current-int(item[4]))
                        total = int(item[5]) + delta
                        async with db() as c:
                            await c.execute("UPDATE test_services SET last_bytes=?,total_bytes=? WHERE id=?",(current,total,item[0]))
                            await c.commit()
                        if total >= limit:
                            async with Sanaei(first[6],first[7],first[8]) as api:
                                await api.delete_tunnel(item[3])
                            async with db() as c:
                                await c.execute("UPDATE test_services SET status='expired',finished_at=? WHERE id=?",(datetime.utcnow().isoformat(),item[0]))
                                await c.commit()
                            msg = f"⏳ حجم تست رایگان شما در سرور {item[9]} به ۲ گیگابایت رسید و سرویس حذف شد."
                            try: await bot.send_message(item[1],msg)
                            except Exception: pass
                            try: await bot.send_message(ADMIN_ID,f"🧪 تست کاربر {item[1]} در سرور {item[9]} به پایان رسید و حذف شد.")
                            except Exception: pass
                except Exception:
                    logging.exception("test usage poll failed for server %s",sid)
        except Exception:
            logging.exception("test usage worker error")
        await asyncio.sleep(max(1,USAGE_CHECK_MINUTES)*60)

async def main():
    if BOT_TOKEN.startswith("PASTE_") or ADMIN_ID==123456789:
        raise SystemExit("ابتدا BOT_TOKEN و ADMIN_ID را در config.py تنظیم کنید.")
    await init_db()
    bot=Bot(BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start", description="🧍🏻 پنل کاربری"),
        BotCommand(command="admin", description="🧑🏻‍💻 پنل مدیریت"),
        BotCommand(command="cancel", description="🚫 لغو عملیات"),
    ])
    asyncio.create_task(usage_worker(bot))
    asyncio.create_task(test_usage_worker(bot))
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
