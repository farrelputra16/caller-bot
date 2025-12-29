import logging
import asyncio
import time
from datetime import datetime
from pymongo import MongoClient
import pytz 
import os
from threading import Thread
from flask import Flask
import aiohttp # <--- LIBRARY BARU UTK SPEED
import certifi

# --- 🛠️ FAKE SERVER 🛠️ ---
app_flask = Flask('')
@app_flask.route('/')
def home(): return "Solana Sniper V5 (Async Speed) Running"
def run_http(): app_flask.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
def keep_alive(): Thread(target=run_http).start()

# --- 🛠️ TIMEZONE FIX 🛠️ ---
import apscheduler.util
def fix_timezone_error(tz): return pytz.UTC
apscheduler.util.astimezone = fix_timezone_error

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, Defaults, 
    ConversationHandler, MessageHandler, CallbackQueryHandler, filters
)
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# --- CONFIGURATION ---
MONGO_URI = "mongodb+srv://farrel:farrel123@snipe-bot.mzzmjcw.mongodb.net/?appName=snipe-bot"
HELIUS_API_KEY = "6e59391b-7fc3-4fd1-81bb-725d257dc15c"
TELEGRAM_TOKEN = "8462035005:AAFVrV4J_6sDE76ad95c1fPQCu-Wt7HhMM0"

DB_NAME = "solana_sniper_bot"
DEFAULT_TIME_WINDOW = 300 

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE ---
try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client[DB_NAME]
    groups_col = db["wallet_groups"]
    processed_col = db["processed_txs"]
except Exception as e:
    logger.error(f"❌ Database Error: {e}")

active_signals = {}
GROUP_NAME, MIN_VOTE, ADD_WALLET_WIZARD, ADD_WALLET_SINGLE = range(4)

# --- GLOBAL ERROR HANDLER ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, BadRequest):
        if "Message is not modified" in str(context.error): return
    logger.error(f"⚠️ Error: {context.error}")

# --- UI HELPERS ---
async def safe_edit_message(query, text, reply_markup, parse_mode="Markdown"):
    try: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)
    except BadRequest: pass
    except Exception as e: logger.warning(f"Edit failed: {e}")

def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Create New Group", callback_data='create_group')],
        [InlineKeyboardButton("📂 Manage My Groups", callback_data='manage_groups')],
        [InlineKeyboardButton("🔄 Refresh Menu", callback_data='refresh_menu')]
    ])

def get_back_button(pattern='back_main'):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=pattern)]])

def is_valid_solana(text):
    text = text.strip()
    return len(text) >= 32 and len(text) <= 44 and " " not in text and text.isalnum()

def is_tx_processed(signature):
    return processed_col.find_one({"signature": signature}) is not None

def mark_tx_processed(signature, wallet):
    processed_col.insert_one({"signature": signature, "wallet": wallet, "createdAt": datetime.utcnow()})

# --- ASYNC API HELPERS (NEW SPEED ENGINE) ---

async def get_holder_stats_async(session, token_mint):
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        # Batch Request untuk mengurangi latency (2 request jadi 1 call)
        payload = [
            {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_mint]},
            {"jsonrpc": "2.0", "id": 2, "method": "getTokenSupply", "params": [token_mint]}
        ]
        
        async with session.post(url, json=payload, timeout=3) as resp:
            data = await resp.json()
            
            # Parsing Batch Response
            res_holders = next((item for item in data if item.get("id") == 1), {})
            res_supply = next((item for item in data if item.get("id") == 2), {})

            if 'result' in res_holders and 'result' in res_supply:
                holders = res_holders['result']['value']
                total_supply = float(res_supply['result']['value']['uiAmount'])
                if total_supply == 0: return "N/A"
                
                top10_sum = sum([float(h['uiAmount']) for h in holders[:10] if h['uiAmount']])
                percentage = (top10_sum / total_supply) * 100
                return f"{percentage:.2f}%"
    except: pass
    return "N/A"

async def get_token_info_async(session, token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with session.get(url, timeout=3) as resp:
            data = await resp.json()
            if data.get("pairs"):
                pair = data["pairs"][0]
                return {
                    "name": pair["baseToken"]["name"],
                    "symbol": pair["baseToken"]["symbol"],
                    "mcap": pair.get("fdv", 0),
                    "liquidity": pair.get("liquidity", {}).get("usd", 0),
                    "volume": pair.get("volume", {}).get("h24", 0),
                    "price": pair["priceUsd"],
                    "url": pair["url"]
                }
    except: pass
    return None

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    text = f"⚡ **Solana Sniper V5 (High Speed)**\nHello {user}, monitoring engine is active."
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message(update.callback_query, text, get_main_menu())
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu(), parse_mode="Markdown")

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await safe_edit_message(update.callback_query, "👇 **Main Menu:**", get_main_menu())

# --- WIZARD: CREATE GROUP ---
async def create_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await safe_edit_message(update.callback_query, "🆕 **Step 1/3: Group Name**", get_back_button())
    return GROUP_NAME

async def receive_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['temp_group_name'] = update.message.text.strip()
    await update.message.reply_text(f"✅ Name: **{update.message.text.strip()}**\n\n🆕 **Step 2/3: Sensitivity**\nMin wallets? (e.g. *2*)", reply_markup=get_back_button(), parse_mode="Markdown")
    return MIN_VOTE

async def receive_min_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        vote = int(update.message.text)
        if vote < 1: raise ValueError
        context.user_data['temp_min_vote'] = vote
        context.user_data['temp_wallets'] = []
        await update.message.reply_text(f"🎯 Target: **{vote} Wallets**\n\n🆕 **Step 3/3: Add Wallets**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish", callback_data='save_new_group')]]), parse_mode="Markdown")
        return ADD_WALLET_WIZARD
    except:
        await update.message.reply_text("❌ Invalid number.")
        return MIN_VOTE

async def receive_wallet_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_valid_solana(text) and text not in context.user_data['temp_wallets']:
        context.user_data['temp_wallets'].append(text)
        await update.message.reply_text(f"✅ **Wallet #{len(context.user_data['temp_wallets'])} Added!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish", callback_data='save_new_group')]]), parse_mode="Markdown")
    return ADD_WALLET_WIZARD

async def save_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    wallets = context.user_data.get('temp_wallets', [])
    if not wallets:
        await update.callback_query.message.reply_text("⚠️ Add at least 1 wallet.")
        return ADD_WALLET_WIZARD
    groups_col.insert_one({"chat_id": update.effective_chat.id, "group_name": context.user_data['temp_group_name'], "min_confluence": context.user_data['temp_min_vote'], "wallets": wallets, "created_at": datetime.utcnow()})
    await safe_edit_message(update.callback_query, f"🎉 **Group Created!**", None)
    await update.callback_query.message.reply_text("👇 **Main Menu:**", reply_markup=get_main_menu(), parse_mode="Markdown")
    return ConversationHandler.END

# --- DASHBOARD ---
async def show_groups_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    groups = list(groups_col.find({"chat_id": update.effective_chat.id}))
    if not groups:
        await safe_edit_message(update.callback_query, "📭 No groups found.", get_back_button())
        return
    kb = [[InlineKeyboardButton(f"📂 {g['group_name']} ({len(g['wallets'])})", callback_data=f"manage_{g['_id']}")] for g in groups]
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data='back_main')])
    await safe_edit_message(update.callback_query, "📋 **Select Group:**", InlineKeyboardMarkup(kb))

async def manage_single_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    from bson.objectid import ObjectId
    try:
        group_id = update.callback_query.data.split("_")[1]
        group = groups_col.find_one({"_id": ObjectId(group_id)})
        if not group: return
        context.user_data['editing_group_id'] = group_id
        kb = [[InlineKeyboardButton("➕ Add Wallet", callback_data=f"addw_{group_id}"), InlineKeyboardButton("➖ Remove Wallet", callback_data=f"rmw_menu_{group_id}")], [InlineKeyboardButton("🗑 Delete Group", callback_data=f"delg_confirm_{group_id}")], [InlineKeyboardButton("🔙 Back", callback_data='manage_groups')]]
        await safe_edit_message(update.callback_query, f"⚙️ **{group['group_name']}**\nTrigger: {group['min_confluence']} | Wallets: {len(group['wallets'])}", InlineKeyboardMarkup(kb))
    except: await safe_edit_message(update.callback_query, "⚠️ Error.", get_back_button())

async def start_add_single_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_edit_message(update.callback_query, "✍️ **Paste New Wallet:**", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data='cancel_add')]]))
    return ADD_WALLET_SINGLE

async def receive_single_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_valid_solana(text):
        from bson.objectid import ObjectId
        groups_col.update_one({"_id": ObjectId(context.user_data.get('editing_group_id'))}, {"$addToSet": {"wallets": text}})
        await update.message.reply_text(f"✅ Added `{text[:6]}...`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Group", callback_data=f"manage_{context.user_data.get('editing_group_id')}")]]), parse_mode="Markdown")
        return ConversationHandler.END
    return ADD_WALLET_SINGLE

async def cancel_add_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await manage_single_group(update, context)
    return ConversationHandler.END

async def remove_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bson.objectid import ObjectId
    group_id = update.callback_query.data.split("_")[2]
    group = groups_col.find_one({"_id": ObjectId(group_id)})
    if not group or not group.get('wallets'):
        await update.callback_query.answer("No wallets.", show_alert=True)
        return
    kb = [[InlineKeyboardButton(f"❌ {w[:6]}...{w[-4:]}", callback_data=f"rmx_{group_id}_{w[:15]}")] for w in group['wallets']]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data=f"manage_{group_id}")])
    await safe_edit_message(update.callback_query, "🗑 **Tap to remove:**", InlineKeyboardMarkup(kb))

async def exec_remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.callback_query.data.split("_")
    from bson.objectid import ObjectId
    group = groups_col.find_one({"_id": ObjectId(parts[1])})
    wallet = next((w for w in group['wallets'] if w.startswith(parts[2])), None)
    if wallet:
        groups_col.update_one({"_id": ObjectId(parts[1])}, {"$pull": {"wallets": wallet}})
        await update.callback_query.answer("Removed!", show_alert=True)
        await remove_wallet_menu(update, context)

async def confirm_delete_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.callback_query.data.split("_")[2]
    kb = [[InlineKeyboardButton("🔥 YES", callback_data=f"delg_exec_{gid}")], [InlineKeyboardButton("🔙 CANCEL", callback_data=f"manage_{gid}")]]
    await safe_edit_message(update.callback_query, "⚠️ **Delete?**", InlineKeyboardMarkup(kb))

async def exec_delete_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bson.objectid import ObjectId
    groups_col.delete_one({"_id": ObjectId(update.callback_query.data.split("_")[2])})
    await safe_edit_message(update.callback_query, "✅ Deleted.", get_back_button('manage_groups'))

async def cancel_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Canceled.", reply_markup=get_main_menu())
    return ConversationHandler.END

# --- 🚀 ASYNC MONITOR ENGINE (SPEED V5) ---

async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    groups = list(groups_col.find())
    if not groups: return

    # Menggunakan aiohttp session untuk koneksi super cepat (Persistent Connection)
    # Ini mencegah "Handshake" berulang-ulang yang bikin lambat
    connector = aiohttp.TCPConnector(limit=50) # Izinkan 50 koneksi sekaligus
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_single_group(session, context, group) for group in groups]
        await asyncio.gather(*tasks)

async def check_single_group(session, context, group):
    if 'chat_id' not in group or not group.get('wallets'): return
    wallets, chat_id, group_name, min_req = group['wallets'], group['chat_id'], group['group_name'], group['min_confluence']
    
    for wallet in wallets:
        try:
            url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&type=SWAP"
            async with session.get(url, timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        tx, sig = data[0], data[0]['signature']
                        if not is_tx_processed(sig):
                            bought_mint = None
                            ignore = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
                            for t in tx.get('tokenTransfers', []):
                                if t['toUserAccount'] == wallet and t['mint'] not in ignore:
                                    bought_mint = t['mint']
                                    break
                            if bought_mint:
                                mark_tx_processed(sig, wallet)
                                # Gunakan session yang sama untuk alert logic
                                await trigger_alert(session, context, chat_id, group_name, wallet, bought_mint, min_req)
        except: pass

async def trigger_alert(session, context, chat_id, group_name, wallet, token, min_req):
    ts = time.time()
    sid = f"{token}_{chat_id}"
    if sid in active_signals and ts - active_signals[sid]['start_time'] > DEFAULT_TIME_WINDOW: del active_signals[sid]
    if sid not in active_signals: active_signals[sid] = {"wallets": set(), "start_time": ts, "alerted": False}
    
    active_signals[sid]['wallets'].add(wallet)
    count = len(active_signals[sid]['wallets'])
    
    if count >= min_req and not active_signals[sid]['alerted']:
        active_signals[sid]['alerted'] = True
        
        # Async Fetch (Non-blocking)
        info = await get_token_info_async(session, token) or {"name": "UNK", "symbol": "???", "mcap": 0, "price": "0", "url": "#", "liquidity": 0, "volume": 0}
        holders_pct = await get_holder_stats_async(session, token)
        
        mcap = f"${info['mcap']:,.0f}" if info['mcap'] else "-"
        padre_link = f"https://padre.gg/token/{token}"
        axiom_link = f"https://axiom.trade/token/{token}"
        photon_link = f"https://photon-sol.tinyastro.io/en/lp/{token}"
        
        msg = (
            f"🚨 <b>{group_name} ALERT!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>{count} Wallets Apeing!</b>\n\n"
            f"💎 <b>{info['name']} ({info['symbol']})</b>\n"
            f"🎫 <code>{token}</code>\n\n"
            f"📊 <b>Stats:</b>\n"
            f"💵 Price: ${info['price']}\n"
            f"🧢 MCap: {mcap}\n"
            f"🐳 <b>Top 10: {holders_pct}</b>\n\n"
            f"👇 <b>Quick Links:</b>\n"
            f"🛡️ <a href='{padre_link}'>Padre</a> | ⚡ <a href='{axiom_link}'>Axiom</a>\n"
            f"🌟 <a href='{photon_link}'>Photon</a> | 🦅 <a href='{info['url']}'>Dex</a>"
        )
        try: await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", disable_web_page_preview=True)
        except: pass

# --- MAIN ---
if __name__ == "__main__":
    keep_alive()
    defaults = Defaults(tzinfo=pytz.UTC)
    req = HTTPXRequest(connection_pool_size=100, read_timeout=20, write_timeout=20, connect_timeout=20)
    app = Application.builder().token(TELEGRAM_TOKEN).defaults(defaults).request(req).build()

    # Handlers
    conv_create = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_group_start, pattern='^create_group$')],
        states={GROUP_NAME: [MessageHandler(filters.TEXT, receive_group_name)], MIN_VOTE: [MessageHandler(filters.TEXT, receive_min_vote)], ADD_WALLET_WIZARD: [MessageHandler(filters.TEXT, receive_wallet_wizard)]},
        fallbacks=[CallbackQueryHandler(save_new_group, pattern='^save_new_group$'), CommandHandler('cancel', cancel_global)]
    )
    conv_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_single_wallet, pattern='^addw_')],
        states={ADD_WALLET_SINGLE: [MessageHandler(filters.TEXT, receive_single_wallet)]},
        fallbacks=[CallbackQueryHandler(cancel_add_single, pattern='^cancel_add$'), CommandHandler('cancel', cancel_global)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_create)
    app.add_handler(conv_add)
    app.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_main$'))
    app.add_handler(CallbackQueryHandler(show_groups_list, pattern='^manage_groups$'))
    app.add_handler(CallbackQueryHandler(start, pattern='^refresh_menu$'))
    app.add_handler(CallbackQueryHandler(manage_single_group, pattern='^manage_'))
    app.add_handler(CallbackQueryHandler(remove_wallet_menu, pattern='^rmw_menu_'))
    app.add_handler(CallbackQueryHandler(exec_remove_wallet, pattern='^rmx_'))
    app.add_handler(CallbackQueryHandler(confirm_delete_group, pattern='^delg_confirm_'))
    app.add_handler(CallbackQueryHandler(exec_delete_group, pattern='^delg_exec_'))
    
    app.add_error_handler(error_handler)

    # PERCEPAT INTERVAL POLLING KE 2 DETIK
    app.job_queue.run_repeating(monitor_task, interval=2, first=1)
    
    print("🚀 Bot V5 (Speed Demon - AsyncIO) Started...")
    app.run_polling()