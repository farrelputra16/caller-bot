import logging
import requests
import certifi
import asyncio
from datetime import datetime
from pymongo import MongoClient
import pytz 
import os
from threading import Thread
from flask import Flask

# --- 🛠️ BAGIAN 1: SERVER PALSU (AGAR BISA GRATIS DI RENDER) 🛠️ ---
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot is running!"

def run_http():
    # Render memberikan PORT lewat environment variable
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_http)
    t.start()
# ---------------------------------------------------------------

# --- 🛠️ BAGIAN 2: FIX TIMEZONE MAC/LINUX 🛠️ ---
import apscheduler.util
def fix_timezone_error(tz):
    return pytz.UTC
apscheduler.util.astimezone = fix_timezone_error
# -------------------------------------------------------------

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, Defaults, 
    ConversationHandler, MessageHandler, CallbackQueryHandler, filters
)
from telegram.request import HTTPXRequest

# --- KONFIGURASI ---
# ⚠️ PASTIKAN PASSWORD MONGODB BENAR DI SINI
MONGO_URI = "mongodb+srv://farrel:<db_password>@snipe-bot.mzzmjcw.mongodb.net/?appName=snipe-bot"

HELIUS_API_KEY = "6e59391b-7fc3-4fd1-81bb-725d257dc15c"
TELEGRAM_TOKEN = "8462035005:AAFVrV4J_6sDE76ad95c1fPQCu-Wt7HhMM0"

DB_NAME = "solana_sniper_bot"
DEFAULT_TIME_WINDOW = 300 

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- DATABASE ---
try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client[DB_NAME]
    groups_col = db["wallet_groups"]
    processed_col = db["processed_txs"]
except Exception as e:
    print(f"❌ Database Error: {e}")

active_signals = {}
GROUP_NAME, MIN_VOTE, ADD_WALLET, CONFIRM_EXIT = range(4)

# --- UI HELPERS ---
def get_main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Create New Group", callback_data='create_group')],
        [InlineKeyboardButton("📋 My Groups", callback_data='list_groups')],
        [InlineKeyboardButton("🗑 Delete Group", callback_data='delete_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_token_info(token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        res = requests.get(url, timeout=5).json()
        if res.get("pairs"):
            pair = res["pairs"][0]
            return {
                "name": pair["baseToken"]["name"],
                "symbol": pair["baseToken"]["symbol"],
                "mcap": pair.get("fdv", 0),
                "price": pair["priceUsd"],
                "url": pair["url"]
            }
    except:
        pass
    return None

def is_tx_processed(signature):
    return processed_col.find_one({"signature": signature}) is not None

def mark_tx_processed(signature, wallet):
    processed_col.insert_one({"signature": signature, "wallet": wallet, "createdAt": datetime.utcnow()})

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 **Hello {user}!**\nTargeting & Sniping Bot Ready.",
        reply_markup=get_main_menu(), parse_mode="Markdown"
    )

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("👇 **Main Menu:**", reply_markup=get_main_menu(), parse_mode="Markdown")

# --- CONVERSATION HANDLERS (CREATE) ---
async def create_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🆕 **Step 1:** Type Group Name.", parse_mode="Markdown")
    return GROUP_NAME

async def receive_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['temp_group_name'] = update.message.text
    await update.message.reply_text("🆕 **Step 2:** Min Wallets to Trigger Alert? (e.g. 2)", parse_mode="Markdown")
    return MIN_VOTE

async def receive_min_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        vote = int(update.message.text)
        context.user_data['temp_min_vote'] = vote
        context.user_data['temp_wallets'] = []
        await update.message.reply_text("🆕 **Step 3:** Paste Wallet Address.", parse_mode="Markdown")
        return ADD_WALLET
    except:
        await update.message.reply_text("❌ Number only.")
        return MIN_VOTE

async def receive_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = update.message.text.strip()
    context.user_data['temp_wallets'].append(wallet)
    count = len(context.user_data['temp_wallets'])
    keyboard = [[InlineKeyboardButton("➕ Add More", callback_data='add_more'), InlineKeyboardButton("✅ Finish", callback_data='save_group')]]
    await update.message.reply_text(f"✅ Wallet #{count} Added!", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM_EXIT

async def loop_add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("👇 Paste next wallet:")
    return ADD_WALLET

async def finish_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    groups_col.insert_one({
        "chat_id": chat_id,
        "group_name": context.user_data['temp_group_name'],
        "min_confluence": context.user_data['temp_min_vote'],
        "wallets": context.user_data['temp_wallets'],
        "created_at": datetime.utcnow()
    })
    await update.callback_query.edit_message_text("🎉 **Group Saved!**", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Canceled.", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- LIST & DELETE ---
async def list_groups_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = list(groups_col.find({"chat_id": update.effective_chat.id}))
    msg = "📋 **Groups:**\n" + "\n".join([f"- {g['group_name']} ({len(g['wallets'])} wallets)" for g in groups]) if groups else "📭 Empty."
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_main')]]), parse_mode="Markdown")

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = list(groups_col.find({"chat_id": update.effective_chat.id}))
    if not groups: return await list_groups_btn(update, context)
    kb = [[InlineKeyboardButton(f"❌ {g['group_name']}", callback_data=f"del_{g['group_name']}")] for g in groups] + [[InlineKeyboardButton("🔙 Back", callback_data='back_main')]]
    await update.callback_query.edit_message_text("🗑 **Delete which one?**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups_col.delete_one({"chat_id": update.effective_chat.id, "group_name": update.callback_query.data[4:]})
    await update.callback_query.edit_message_text("✅ Deleted.", reply_markup=get_main_menu())

# --- MONITOR TASK ---
async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    for group in list(groups_col.find()):
        if 'chat_id' not in group or not group.get('wallets'): continue
        for wallet in group['wallets']:
            try:
                resp = requests.get(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&type=SWAP", timeout=5)
                if resp.status_code == 200 and resp.json():
                    tx = resp.json()[0]
                    if is_tx_processed(tx['signature']): continue
                    
                    bought = next((t['mint'] for t in tx.get('tokenTransfers', []) if t['toUserAccount'] == wallet and t['mint'] not in ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]), None)
                    if bought:
                        mark_tx_processed(tx['signature'], wallet)
                        await process_snipe(context, group['chat_id'], group['group_name'], wallet, bought, group['min_confluence'])
            except: pass

async def process_snipe(context, chat_id, group_name, wallet, token, min_req):
    import time
    sid = f"{token}_{chat_id}"
    if sid in active_signals and time.time() - active_signals[sid]['start_time'] > DEFAULT_TIME_WINDOW: del active_signals[sid]
    if sid not in active_signals: active_signals[sid] = {"wallets": set(), "start_time": time.time(), "alerted": False}
    
    active_signals[sid]['wallets'].add(wallet)
    count = len(active_signals[sid]['wallets'])
    
    if count >= min_req and not active_signals[sid]['alerted']:
        active_signals[sid]['alerted'] = True
        info = get_token_info(token) or {"name": "Unknown", "symbol": "???", "price": "0", "mcap": 0, "url": "#"}
        msg = f"🚨 <b>{group_name} ALERT!</b>\n⚡ <b>{count} Wallets!</b>\n💎 {info['name']} ({info['symbol']})\n<code>{token}</code>\n💵 ${info['price']} | 🧢 ${info['mcap']:,.0f}\n<a href='{info['url']}'>DexScreener</a>"
        try: await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", disable_web_page_preview=True)
        except: pass

# --- MAIN ---
if __name__ == "__main__":
    # 1. JALANKAN SERVER PALSU (AGAR RENDER TIDAK MEMATIKAN BOT)
    keep_alive()

    defaults = Defaults(tzinfo=pytz.UTC)
    req = HTTPXRequest(connection_pool_size=20, read_timeout=30, write_timeout=30, connect_timeout=30)
    app = Application.builder().token(TELEGRAM_TOKEN).defaults(defaults).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_group_start, pattern='^create_group$')],
        states={GROUP_NAME: [MessageHandler(filters.TEXT, receive_group_name)], MIN_VOTE: [MessageHandler(filters.TEXT, receive_min_vote)], ADD_WALLET: [MessageHandler(filters.TEXT, receive_wallet)], CONFIRM_EXIT: [CallbackQueryHandler(loop_add_wallet, pattern='^add_more$'), CallbackQueryHandler(finish_group, pattern='^save_group$')]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(list_groups_btn, pattern='^list_groups$'))
    app.add_handler(CallbackQueryHandler(delete_menu, pattern='^delete_menu$'))
    app.add_handler(CallbackQueryHandler(confirm_delete, pattern='^del_'))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_main$'))

    app.job_queue.run_repeating(monitor_task, interval=12, first=5)
    app.run_polling()