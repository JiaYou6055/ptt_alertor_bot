import logging
import re
import time
import traceback
from datetime import time as dt_time
from typing import List, Dict, Any, Optional
import httpx
from telegram import Update, BotCommand, User
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from database import Database
from crawler import (
    fetch_board_articles,
    fetch_article_details,
    match_subscription,
    parse_article_id,
    parse_nrec_to_int,
    check_board_exists,
    is_article_too_old,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database(config.DB_PATH)

# Last execution timestamps for dynamic night interval control
last_ptt_check_time: float = 0.0
last_article_check_time: float = 0.0
last_error_notify_time: Dict[int, float] = {}

# Telegram hard limit per message is 4096 chars; keep a safety buffer so a
# large batch of matched articles is split into multiple messages instead of
# being rejected wholesale with "Message is too long".
TELEGRAM_MAX_MSG_LEN = 3800

# Telegram legacy Markdown (parse_mode="Markdown") special chars that must be
# escaped when displaying raw, untrusted text (e.g. a PTT title) as plain
# text rather than as link text, otherwise an unmatched _ * ` [ can break
# entity parsing and cause the whole message to fail to send.
_MARKDOWN_V1_SPECIAL_CHARS = re.compile(r"([_*`\[])")


def escape_markdown_v1(text: str) -> str:
    """Escape Telegram legacy Markdown special chars so raw PTT titles render as literal text."""
    return _MARKDOWN_V1_SPECIAL_CHARS.sub(r"\\\1", text)


def format_nrec_display(nrec_raw: str) -> str:
    """Convert a raw PTT nrec code into a human push/boo label.

    Examples: '66' -> '推66', 'X2' -> '噓20', '爆' -> '推爆', 'XX' -> '噓爆', '' -> '0'.
    """
    if not nrec_raw:
        return "0"
    if nrec_raw == "爆":
        return "推爆"
    if nrec_raw == "XX":
        return "噓爆"
    if nrec_raw.startswith("X") and len(nrec_raw) == 2 and nrec_raw[1].isdigit():
        return f"噓{int(nrec_raw[1]) * 10}"
    val = parse_nrec_to_int(nrec_raw)
    if val < 0:
        return f"噓{abs(val)}"
    if val == 0:
        return "0"
    return f"推{val}"


def is_admin(chat_id: int) -> bool:
    """Check if chat_id belongs to a Super Admin (specified in .env)."""
    if config.ADMIN_USER_ID and chat_id == config.ADMIN_USER_ID:
        return True
    if config.ALLOWED_USER_IDS and chat_id in config.ALLOWED_USER_IDS:
        return True
    if not config.ADMIN_USER_ID and not config.ALLOWED_USER_IDS and not db.get_db_allowed_users():
        return True
    return False


def is_user_allowed(chat_id: int) -> bool:
    """Check if a chat_id is permitted to access the bot (admin or whitelisted)."""
    if is_admin(chat_id):
        return True
    db_allowed = db.get_db_allowed_users()
    if not config.ALLOWED_USER_IDS and not config.ADMIN_USER_ID and not db_allowed:
        return True
    return chat_id in db_allowed


def update_user_info_from_telegram(user: Optional[User]) -> str:
    """Extract display name from Telegram User object and update DB if allowed."""
    if not user or not hasattr(user, "id"):
        return ""
    try:
        chat_id = int(user.id)
    except (ValueError, TypeError):
        return ""

    name_parts = []
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    username = getattr(user, "username", "") or ""

    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        name_parts.append(full_name)
    if username:
        name_parts.append(f"(@{username})")

    display_name = " ".join(name_parts)
    if display_name and is_user_allowed(chat_id):
        db.update_user_name(chat_id, display_name)
    return display_name


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log uncaught errors to DB and send notification to Super Admin if configured."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    err_type = type(context.error).__name__ if context.error else "UnknownError"
    err_msg_str = str(context.error) if context.error else "No error message"
    tb_lines = (
        traceback.format_exception(None, context.error, context.error.__traceback__)
        if context.error
        else []
    )
    tb_str = "".join(tb_lines)

    log_res = db.log_system_error(err_type, err_msg_str, tb_str)
    err_id = log_res["id"]
    count = log_res["occurrence_count"]
    is_new = log_res["is_new"]

    if config.ADMIN_USER_ID:
        now = time.time()
        last_sent = last_error_notify_time.get(err_id, 0.0)
        # Notify Admin if it's a new error OR > 10 min (600s) since last notification
        if is_new or (now - last_sent > 600):
            last_error_notify_time[err_id] = now
            try:
                safe_err = escape_markdown_v1(err_msg_str[:200])
                safe_type = escape_markdown_v1(err_type)
                msg = (
                    f"⚠️ *【系統異常通知】*\n"
                    f"📌 *ID:* `{err_id}` | *類型:* `{safe_type}` | *累計次數:* {count}\n"
                    f"💬 *訊息:* `{safe_err}`\n\n"
                    f"💡 可使用 `/err_detail {err_id}` 查看完整資訊，或 `/err_resolve {err_id}` 標記已處理。"
                )
                await context.bot.send_message(
                    chat_id=config.ADMIN_USER_ID, text=msg, parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send error notification to Admin: {e}")



async def setup_bot_commands(application) -> None:
    """Register bot command hints in Telegram UI menu and send startup notification."""
    commands = [
        BotCommand("help", "顯示指令對照表與完整說明"),
        BotCommand("check", "立即執行 PTT 爬蟲抓取 (不用等 5 分鐘)"),
        BotCommand("list", "查看我的 PTT 訂閱清單"),
        BotCommand("top", "查看熱門前 5 名追蹤排行榜"),
        BotCommand("myid", "查詢自己的 Telegram Chat ID"),
        BotCommand("night", "查看夜間靜音模式狀態"),
        BotCommand("del", "刪除指定 ID 的訂閱項目 (例如 /del 1)"),
        BotCommand("allow", "【管理員】授權 Chat ID (如 /allow 123 小明)"),
        BotCommand("deny", "【管理員】取消某 Chat ID 的授權"),
        BotCommand("whitelist", "【管理員】查看授權白名單列表與成員名稱"),
        BotCommand("errors", "【管理員】查看未解決的系統異常清單"),
        BotCommand("err_detail", "【管理員】查看指定異常 ID 詳細資訊與 Traceback"),
        BotCommand("err_resolve", "【管理員】將指定異常 ID 標記為已處理"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Successfully registered Telegram Bot Commands Menu.")
    except Exception as e:
        logger.error(f"Failed to set bot commands menu: {e}")

    # Send startup online notification to Super Admin
    if config.ADMIN_USER_ID:
        try:
            night_status = "🌙 夜間模式時段中" if config.is_night_mode() else "☀️ 日間正常模式中"
            current_interval_min = config.get_current_check_interval() // 60
            startup_msg = (
                "🟢 *【PTT Alert 機器人上線通知】*\n\n"
                "🚀 服務已順利啟動運行！\n"
                f"⏱️ 輪詢間隔：`{current_interval_min} 分鐘`\n"
                f"📊 當前模式：`{night_status}`\n\n"
                "您可以傳送 `指令` 或 `立即檢查` 開始使用。"
            )
            await application.bot.send_message(
                chat_id=config.ADMIN_USER_ID, text=startup_msg, parse_mode="Markdown"
            )
            logger.info(f"Sent startup notification to Admin ({config.ADMIN_USER_ID}).")
        except Exception as e:
            logger.error(f"Failed to send startup notification: {e}")


async def myid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /myid so users can check their Telegram chat ID and name."""
    if update.message:
        user = update.message.from_user
        display_name = update_user_info_from_telegram(user)
        chat_id = update.message.chat_id
        admin_badge = " (超級管理員)" if is_admin(chat_id) else ""
        name_info = f"\n👤 帳號名稱：`{display_name}`" if display_name else ""
        await update.message.reply_text(
            f"🆔 您的 Telegram Chat ID 為：`{chat_id}`{admin_badge}{name_info}", parse_mode="Markdown"
        )


async def resend_latest_matched(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Re-push the single latest board article matching the user's subscriptions.

    Used by /check as a delivery-channel self-test: when a normal crawl found no
    NEW article to push (everything already pushed), this still sends one real
    matching article so the user can confirm notifications are working. It bypasses
    the dedup table on purpose and does NOT mark anything as pushed.
    Returns True if a test message was sent.
    """
    all_subs = db.get_all_subscriptions()
    my_subs = [s for s in all_subs if s.get("chat_id") == chat_id]
    if not my_subs:
        return False

    boards = {s.get("board", "").lower() for s in my_subs if s.get("board")}
    latest: Optional[Dict[str, Any]] = None
    latest_ts = -1
    latest_show_author = False

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for board in boards:
            articles = await fetch_board_articles(board, client=client)
            board_subs = [s for s in my_subs if s.get("board", "").lower() == board]
            for article in articles:
                matched_subs = [sub for sub in board_subs if match_subscription(article, sub)]
                if not matched_subs:
                    continue
                # Article id looks like "M.<unix_ts>.A.xxx"; use it to find newest.
                try:
                    ts = int(article["article_id"].split(".")[1])
                except (IndexError, ValueError):
                    ts = 0
                if ts > latest_ts:
                    latest_ts = ts
                    latest = article
                    latest_show_author = any(sub.get("sub_type", "").lower() == "author" for sub in matched_subs)

    if not latest:
        return False

    nrec_str = f"[{format_nrec_display(latest['nrec'])}]"
    msg = (
        "🧪 *【推播管道測試】* 目前無新文章，重送最新一篇符合條件的文章供您確認推播正常：\n\n"
        f"🚨 *[{latest['board']}]* {nrec_str} {escape_markdown_v1(latest['title'])}\n"
        f"{latest['url']}"
    )
    if latest_show_author:
        msg += f"\n👤 `{latest['author']}`"
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send channel-test message to {chat_id}: {e}")
        return False


async def check_now_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger an immediate crawl cycle without waiting for timer."""
    if not update.message:
        return

    chat_id = update.message.chat_id
    if not is_user_allowed(chat_id):
        await update.message.reply_text("⛔ 權限不足。")
        return

    global last_ptt_check_time, last_article_check_time
    last_ptt_check_time = 0.0
    last_article_check_time = 0.0

    await update.message.reply_text("🔄 正在立即抓取最新 PTT 文章與追蹤推文，請稍候...")

    # Force execution of jobs
    pushed = await check_ptt_job(context)
    await check_tracked_articles_job(context)

    if pushed > 0:
        await update.message.reply_text(
            f"✅ 立即抓取完成！已推播 {pushed} 篇符合條件的新文章至您的對話視窗。"
        )
    else:
        # No new article to push — send one real matching article as a
        # delivery-channel self-test so the user can confirm push works.
        sent = await resend_latest_matched(context, chat_id)
        if sent:
            await update.message.reply_text(
                "✅ 立即抓取完成！目前無新文章，已重送最新一篇符合條件的文章供您確認推播管道正常。"
            )
        else:
            await update.message.reply_text(
                "✅ 立即抓取完成！目前訂閱看板中沒有任何符合門檻的文章可供推播確認。"
            )


async def night_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current Night Mode status and configuration."""
    if not update.message:
        return

    chat_id = update.message.chat_id
    if not is_user_allowed(chat_id):
        await update.message.reply_text("⛔ 權限不足。")
        return

    is_night = config.is_night_mode()
    status_str = "🌙 *夜間靜音模式運作中*" if is_night else "☀️ *日間正常模式運作中*"

    interval_sec = config.get_current_check_interval()
    interval_desc = f"{interval_sec // 60} 分鐘"

    text = (
        f"{status_str}\n\n"
        f"⏰ *夜間時段：* `{config.NIGHT_START_HOUR:02d}:00` ~ `{config.NIGHT_END_HOUR:02d}:00`\n"
        f"🔇 *通知型態：* 靜音無聲推播 (`disable_notification`)\n"
        f"⏱️ *當前輪詢頻率：* 每 `{interval_desc}` 抓取一次\n"
        f"💡 *(可於 .env 設定 NIGHT_START_HOUR, NIGHT_END_HOUR 與 NIGHT_CHECK_INTERVAL_SECONDS)*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def allow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dynamically authorize a chat_id with optional name/note (Super Admin only)."""
    if not update.message:
        return

    admin_chat_id = update.message.chat_id
    if not is_admin(admin_chat_id):
        await update.message.reply_text("⛔ 抱歉，只有超級管理員才可以管理白名單授權。")
        return

    target_id_str = None
    note_name = ""
    if context.args and len(context.args) > 0:
        target_id_str = context.args[0].strip()
        if len(context.args) > 1:
            note_name = " ".join(context.args[1:]).strip()

    if not target_id_str:
        await update.message.reply_text(
            "❌ 請提供要授權的 Chat ID！\n例如：`/allow 123456789` 或 `/allow 123456789 小明`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(target_id_str)
    except ValueError:
        await update.message.reply_text("❌ Chat ID 必須為數字！")
        return

    db.add_allowed_user(target_id, name=note_name, added_by=admin_chat_id)
    name_desc = f" ({note_name})" if note_name else ""
    await update.message.reply_text(
        f"✅ 成功新增授權！Chat ID `{target_id}`{name_desc} 現在可以使用機器人。",
        parse_mode="Markdown",
    )


async def deny_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dynamically revoke authorization for a chat_id (Super Admin only)."""
    if not update.message:
        return

    admin_chat_id = update.message.chat_id
    if not is_admin(admin_chat_id):
        await update.message.reply_text("⛔ 抱歉，只有超級管理員才可以取消白名單授權。")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("❌ 請提供要取消授權的 Chat ID！例如：`/deny 123456789`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("❌ Chat ID 必須為數字！")
        return

    ok = db.remove_allowed_user(target_id)
    if ok:
        await update.message.reply_text(f"🗑️ 已成功移除 Chat ID `{target_id}` 的存取權限。", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ 白名單中找不到 Chat ID `{target_id}`（可能設定在 .env 靜態白名單中）。", parse_mode="Markdown")


async def whitelist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current authorized whitelist with member names (Super Admin only)."""
    if not update.message:
        return

    chat_id = update.message.chat_id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ 抱歉，只有超級管理員才可以查看白名單清單。")
        return

    env_users = config.ALLOWED_USER_IDS
    db_users = db.get_db_allowed_users_with_names()

    lines = ["🛡️ *目前白名單授權成員清單：*\n"]
    if config.ADMIN_USER_ID:
        lines.append(f"👑 *超級管理員 (Owner)：* `{config.ADMIN_USER_ID}`\n")

    if env_users:
        lines.append("📌 *.env 靜態授權：*")
        for u in env_users:
            lines.append(f"• `{u}`")
        lines.append("")

    if db_users:
        lines.append("📌 *動態授權成員 (資料庫)：*")
        for u in db_users:
            name_str = f" - {u['name']}" if u.get("name") else " - (尚未登錄名稱)"
            lines.append(f"• `{u['chat_id']}`{name_str}")
        lines.append("")

    if not env_users and not db_users and not config.ADMIN_USER_ID:
        lines.append("ℹ️ 未設定任何白名單限制（目前開放所有人使用）。")
    else:
        lines.append("超級管理員可使用 `/allow <ID> [備註/姓名]` 管理白名單！")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def list_errors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """【管理員】查看未解決的系統異常記錄清單 (/errors)"""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if config.ADMIN_USER_ID and chat_id != config.ADMIN_USER_ID:
        await update.message.reply_text("⛔ 您沒有權限使用此管理員指令。")
        return

    errors = db.get_pending_errors(limit=10)
    if not errors:
        await update.message.reply_text("✅ 目前沒有待處理的系統異常記錄！", parse_mode="Markdown")
        return

    lines = ["🛠️ *【未解決系統異常清單】* (最多前 10 筆)\n"]
    for err in errors:
        eid = err["id"]
        etype = escape_markdown_v1(err["error_type"])
        emsg = escape_markdown_v1(err["message"][:60])
        cnt = err["occurrence_count"]
        last = err["last_seen"] or err["first_seen"] or "未知"
        lines.append(f"• *ID `{eid}`* | `{etype}` (共 {cnt} 次)\n  `{emsg}`\n  ⏱️ 最後發生: {last}\n")

    lines.append("\n💡 提示：使用 `/err_detail <ID>` 查看詳細 Traceback，`/err_resolve <ID>` 標記已修復。")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def error_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """【管理員】查看特定異常詳細資訊 (/err_detail <ID>)"""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if config.ADMIN_USER_ID and chat_id != config.ADMIN_USER_ID:
        await update.message.reply_text("⛔ 您沒有權限使用此管理員指令。")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("⚠️ 請提供正確的異常 ID，範例：`/err_detail 1`", parse_mode="Markdown")
        return

    err_id = int(args[0])
    err = db.get_error_by_id(err_id)
    if not err:
        await update.message.reply_text(f"❌ 找不到 ID 為 `{err_id}` 的異常記錄。", parse_mode="Markdown")
        return

    etype = escape_markdown_v1(err["error_type"])
    emsg = escape_markdown_v1(err["message"])
    tb = err["traceback"][:1500] if err["traceback"] else "無 Traceback 紀錄"
    status = "🔴 待處理 (pending)" if err["status"] == "pending" else "🟢 已解決 (resolved)"

    msg = (
        f"🔍 *【異常詳細紀錄 ID: {err['id']}】*\n\n"
        f"📌 *狀態:* {status}\n"
        f"🏷️ *類型:* `{etype}`\n"
        f"🔢 *累計次數:* {err['occurrence_count']}\n"
        f"🕒 *首次發生:* {err['first_seen']}\n"
        f"⏱️ *最後發生:* {err['last_seen']}\n\n"
        f"💬 *錯誤訊息:* `{emsg}`\n\n"
        f"📜 *Traceback (前 1500 字元):*\n```\n{tb}\n```"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def resolve_error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """【管理員】標記異常為已解決 (/err_resolve <ID>)"""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if config.ADMIN_USER_ID and chat_id != config.ADMIN_USER_ID:
        await update.message.reply_text("⛔ 您沒有權限使用此管理員指令。")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("⚠️ 請提供正確的異常 ID，範例：`/err_resolve 1`", parse_mode="Markdown")
        return

    err_id = int(args[0])
    ok = db.resolve_error(err_id)
    if ok:
        await update.message.reply_text(f"✅ 已成功將異常 ID `{err_id}` 標記為已解決！", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ 標記失敗，找不到 ID 為 `{err_id}` 的異常記錄。", parse_mode="Markdown")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send command usage guide in Ptt-Alertor style."""
    if not update.message:
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    update_user_info_from_telegram(user)

    if not is_user_allowed(chat_id):
        await update.message.reply_text(
            f"⛔ 抱歉，此機器人設有白名單限制，您目前無權使用。\n您的 Telegram ID 為：`{chat_id}`",
            parse_mode="Markdown",
        )
        return

    text = (
        "🤖 *Ptt Alertor 指令對照表*\n\n"
        "您可以使用 *自然語言指令* 或 *斜線指令* 進行操作：\n\n"
        "🔑 *關鍵字相關*\n"
        "• `新增 看板 關鍵字` (例如：`新增 gossiping,movie 金城武,結衣`)\n"
        "• `刪除 看板 關鍵字` (例如：`刪除 gossiping 金城武`)\n\n"
        "👤 *作者相關*\n"
        "• `新增作者 看板 作者` (例如：`新增作者 gossiping ffaarr,obov`)\n"
        "• `刪除作者 看板 作者` (例如：`刪除作者 gossiping ffaarr`)\n\n"
        "🔥 *推噓文數門檻*\n"
        "• `新增推文數 看板 總數` (例如：`新增推文數 beauty,joke 10`)\n"
        "• `新增噓文數 看板 總數` (例如：`新增噓文數 gossiping 20`)\n\n"
        "💬 *特定文章推文追蹤*\n"
        "• `新增推文 PTT文章網址`\n"
        "  (例如：`新增推文 https://www.ptt.cc/bbs/EZsoft/M.1708247900.A.27C.html`)\n"
        "• `刪除推文 PTT文章網址`\n\n"
        "📊 *一般查詢與操作*\n"
        "• `立即檢查` (或 `/check`) - 立即觸發抓取 (不用等 5 分鐘)\n"
        "• `清單` (或 `/list`) - 查看我的訂閱項目\n"
        "• `排行` (或 `/top`) - 熱門前五名追蹤排行榜\n"
        "• `/night` - 查看夜間模式狀態\n"
        "• `/myid` - 查看自己的 Telegram Chat ID"
    )

    if is_admin(chat_id):
        text += (
            "\n\n👑 *管理員權限指令*\n"
            "• `/allow <Chat_ID> [備註/姓名]` - 動態授權使用者 (如 `授權 123 小明`)\n"
            "• `/deny <Chat_ID>` - 動態移除授權使用者\n"
            "• `/whitelist` - 查看授權白名單成員列表"
        )

    await update.message.reply_text(text, parse_mode="Markdown")


async def list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's active subscriptions."""
    if not update.message:
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    update_user_info_from_telegram(user)

    if not is_user_allowed(chat_id):
        await update.message.reply_text(
            f"⛔ 抱歉，此機器人設有白名單限制，您目前無權使用。\n您的 Telegram ID 為：`{chat_id}`",
            parse_mode="Markdown",
        )
        return

    subs = db.get_user_subscriptions(chat_id)

    if not subs:
        await update.message.reply_text(
            "ℹ️ 您目前沒有任何訂閱項目。\n輸入 `指令` 查看如何新增！",
            parse_mode="Markdown",
        )
        return

    keywords = []
    authors = []
    pushes = []
    boos = []
    articles = []

    for s in subs:
        st = s.get("sub_type")
        sid = s.get("id")
        board = s.get("board", "")
        target = s.get("target", "")

        if st == "keyword":
            keywords.append(f"• [ID: {sid}] 看板: `{board}` | 關鍵字: `{target}`")
        elif st == "author":
            authors.append(f"• [ID: {sid}] 看板: `{board}` | 作者: `{target}`")
        elif st == "push":
            pushes.append(f"• [ID: {sid}] 看板: `{board}` | 推文數 ≥ `{target}`")
        elif st == "boo":
            boos.append(f"• [ID: {sid}] 看板: `{board}` | 噓文數 ≥ `{target}`")
        elif st == "article":
            articles.append(f"• [ID: {sid}] 追蹤文章: {target}")

    lines = ["📋 *您的 PTT 訂閱清單：*\n"]
    if keywords:
        lines.append("🔑 *關鍵字：*")
        lines.extend(keywords)
        lines.append("")
    if authors:
        lines.append("👤 *作者：*")
        lines.extend(authors)
        lines.append("")
    if pushes or boos:
        lines.append("🔥 *推噓文門檻：*")
        lines.extend(pushes)
        lines.extend(boos)
        lines.append("")
    if articles:
        lines.append("💬 *追蹤文章：*")
        lines.extend(articles)
        lines.append("")

    lines.append("如需刪除，可傳送 `刪除 看板 關鍵字` 或傳送 `/del <ID>`！")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def top_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top 5 keywords and authors across all subscriptions."""
    if not update.message:
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    update_user_info_from_telegram(user)

    if not is_user_allowed(chat_id):
        await update.message.reply_text(
            f"⛔ 抱歉，此機器人設有白名單限制，您目前無權使用。\n您的 Telegram ID 為：`{chat_id}`",
            parse_mode="Markdown",
        )
        return

    top_data = db.get_top_subscriptions(limit=5)
    lines = ["🏆 *Ptt Alertor 追蹤排行榜 Top 5*\n"]

    lines.append("🔥 *熱門關鍵字：*")
    if top_data["keywords"]:
        for idx, (kw, cnt) in enumerate(top_data["keywords"], 1):
            lines.append(f"{idx}. `{kw}` ({cnt} 人訂閱)")
    else:
        lines.append("暫無資料")

    lines.append("\n👤 *熱門作者：*")
    if top_data["authors"]:
        for idx, (au, cnt) in enumerate(top_data["authors"], 1):
            lines.append(f"{idx}. `{au}` ({cnt} 人訂閱)")
    else:
        lines.append("暫無資料")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def del_by_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /del <id> command."""
    if not update.message or not context.args:
        if update.message:
            await update.message.reply_text("❌ 請提供訂閱 ID，例如：`/del 1`", parse_mode="Markdown")
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    update_user_info_from_telegram(user)

    if not is_user_allowed(chat_id):
        await update.message.reply_text(
            f"⛔ 抱歉，此機器人設有白名單限制，您目前無權使用。\n您的 Telegram ID 為：`{chat_id}`",
            parse_mode="Markdown",
        )
        return

    sub_id_str = context.args[0].replace("art_", "")
    try:
        sub_id = int(sub_id_str)
    except ValueError:
        await update.message.reply_text("❌ ID 必須為數字！")
        return

    success = db.delete_subscription_by_id(sub_id, chat_id)
    if success:
        await update.message.reply_text(f"✅ 已成功刪除訂閱 ID: `{sub_id}`")
    else:
        await update.message.reply_text(f"❌ 找不到訂閱 ID `{sub_id}`。")


async def validate_boards_exist(update: Update, board_str: str) -> bool:
    """Helper to check if all boards specified in board_str exist on PTT."""
    boards = [b.strip() for b in board_str.split(",") if b.strip()]
    for b in boards:
        exists = await check_board_exists(b)
        if not exists:
            await update.message.reply_text(
                f"❌ 找不到 PTT 看板「`{b}`」！請檢查名稱拼寫是否正確（例如：gossiping, stock, movie）。",
                parse_mode="Markdown",
            )
            return False
    return True


async def text_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse natural language Ptt-Alertor commands sent by user."""
    if not update.message or not update.message.text:
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    update_user_info_from_telegram(user)

    if not is_user_allowed(chat_id):
        await update.message.reply_text(
            f"⛔ 抱歉，此機器人設有白名單限制，您目前無權使用。\n您的 Telegram ID 為：`{chat_id}`",
            parse_mode="Markdown",
        )
        return

    # 1. 指令 / 說明
    if text in ["指令", "說明", "help"]:
        await help_handler(update, context)
        return

    # 2. 清單 / list
    if text in ["清單", "list"]:
        await list_handler(update, context)
        return

    # 3. 排行 / top
    if text in ["排行", "top"]:
        await top_handler(update, context)
        return

    # 4. 立即檢查 / 立即抓取 / 更新 / check
    if text in ["立即檢查", "立即抓取", "更新", "check"]:
        await check_now_handler(update, context)
        return

    # 5. 白名單 / whitelist
    if text in ["白名單", "whitelist"]:
        await whitelist_handler(update, context)
        return

    # 6. 夜間模式 / night
    if text in ["夜間模式", "night"]:
        await night_status_handler(update, context)
        return

    # 7. 授權 123456 [備註/姓名] (Admin only)
    match_allow = re.match(r"^(?:授權|allow)\s+(\d+)(?:\s+(.+))?$", text, re.IGNORECASE)
    if match_allow:
        args = [match_allow.group(1)]
        if match_allow.group(2):
            args.append(match_allow.group(2))
        context.args = args
        await allow_handler(update, context)
        return

    # 8. 取消授權 123456 (Admin only)
    match_deny = re.match(r"^(?:取消授權|deny)\s+(\d+)$", text, re.IGNORECASE)
    if match_deny:
        context.args = [match_deny.group(1)]
        await deny_handler(update, context)
        return

    # 9. 新增關鍵字: "新增 gossiping,movie 金城武,結衣"
    match_add_kw = re.match(r"^新增\s+([^\s]+)\s+(.+)$", text)
    if match_add_kw:
        board_part, target_part = match_add_kw.group(1), match_add_kw.group(2)
        if not await validate_boards_exist(update, board_part):
            return
        sub_ids = db.add_subscription(chat_id, "keyword", board_part, target_part)
        await update.message.reply_text(
            f"✅ *成功新增關鍵字訂閱！*\n📌 看板：`{board_part}`\n🔍 關鍵字：`{target_part}`",
            parse_mode="Markdown",
        )
        return

    # 10. 刪除關鍵字: "刪除 gossiping 金城武"
    match_del_kw = re.match(r"^刪除\s+([^\s]+)\s+(.+)$", text)
    if match_del_kw:
        board_part, target_part = match_del_kw.group(1), match_del_kw.group(2)
        cnt = db.delete_subscription(chat_id, "keyword", board_part, target_part)
        await update.message.reply_text(f"🗑️ 已刪除 {cnt} 筆關鍵字訂閱 (`{board_part}` - `{target_part}`)。")
        return

    # 11. 新增作者: "新增作者 gossiping ffaarr,obov"
    match_add_author = re.match(r"^新增作者\s+([^\s]+)\s+(.+)$", text)
    if match_add_author:
        board_part, target_part = match_add_author.group(1), match_add_author.group(2)
        if not await validate_boards_exist(update, board_part):
            return
        db.add_subscription(chat_id, "author", board_part, target_part)
        await update.message.reply_text(
            f"✅ *成功新增作者訂閱！*\n📌 看板：`{board_part}`\n👤 作者：`{target_part}`",
            parse_mode="Markdown",
        )
        return

    # 12. 刪除作者: "刪除作者 gossiping ffaarr"
    match_del_author = re.match(r"^刪除作者\s+([^\s]+)\s+(.+)$", text)
    if match_del_author:
        board_part, target_part = match_del_author.group(1), match_del_author.group(2)
        cnt = db.delete_subscription(chat_id, "author", board_part, target_part)
        await update.message.reply_text(f"🗑️ 已刪除 {cnt} 筆作者訂閱 (`{board_part}` - `{target_part}`)。")
        return

    # 13. 新增推文數: "新增推文數 beauty,joke 10"
    match_add_push = re.match(r"^新增推文數\s+([^\s]+)\s+(\d+)$", text)
    if match_add_push:
        board_part, num_part = match_add_push.group(1), match_add_push.group(2)
        if not await validate_boards_exist(update, board_part):
            return
        db.add_subscription(chat_id, "push", board_part, num_part)
        await update.message.reply_text(
            f"✅ *成功新增推文數訂閱！*\n📌 看板：`{board_part}`\n🔥 門檻：推文數 ≥ `{num_part}`",
            parse_mode="Markdown",
        )
        return

    # 14. 新增噓文數: "新增噓文數 gossiping 20"
    match_add_boo = re.match(r"^新增噓文數\s+([^\s]+)\s+(\d+)$", text)
    if match_add_boo:
        board_part, num_part = match_add_boo.group(1), match_add_boo.group(2)
        if not await validate_boards_exist(update, board_part):
            return
        db.add_subscription(chat_id, "boo", board_part, num_part)
        await update.message.reply_text(
            f"✅ *成功新增噓文數訂閱！*\n📌 看板：`{board_part}`\n👎 門檻：噓文數 ≥ `{num_part}`",
            parse_mode="Markdown",
        )
        return

    # 15. 新增推文 (追蹤文章): "新增推文 https://www.ptt.cc/bbs/EZsoft/M.1708247900.A.27C.html"
    match_add_art = re.match(r"^新增推文\s+(https?://[^\s]+)$", text)
    if match_add_art:
        url = match_add_art.group(1)
        m = re.search(r"/bbs/([^/]+)/(M\.\d+\.A\.[A-Za-z0-9_-]+)\.html", url)
        if not m:
            await update.message.reply_text("❌ 文章網址格式不正確！必須為 ptt.cc 文章網址。")
            return
        board, article_id = m.group(1), m.group(2)
        if not await validate_boards_exist(update, board):
            return
        details = await fetch_article_details(url)
        ok = db.add_article_tracking(chat_id, url, board, article_id, details["total_comments"])
        if ok:
            await update.message.reply_text(
                f"✅ *已開始追蹤文章推文！*\n📌 標題：{details['title'] or article_id}\n💬 目前推文數：{details['total_comments']}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("ℹ️ 您已經在追蹤此文章了！")
        return

    # 16. 刪除推文 (停止追蹤文章)
    match_del_art = re.match(r"^刪除推文\s+(https?://[^\s]+)$", text)
    if match_del_art:
        url = match_del_art.group(1)
        cnt = db.delete_article_tracking(chat_id, url)
        await update.message.reply_text(f"🗑️ 已停止追蹤該文章推文 (已移除 {cnt} 筆)！")
        return


def format_article_line(article: Dict[str, Any]) -> str:
    """Format a single matched article as one compact digest line + URL line."""
    nrec_str = f"[{format_nrec_display(article['nrec'])}]"
    author_part = f" - `{article['author']}`" if article.get("_show_author") else ""
    return (
        f"• *[{article['board']}]* {nrec_str} "
        f"{escape_markdown_v1(article['title'])}{author_part}\n"
        f"  {article['url']}"
    )


def build_notification_chunks(matched_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split matched articles into message chunks under Telegram's length limit.

    Returns a list of {"text": str, "articles": [...]} so each chunk can be
    sent independently and only its own articles marked pushed on success.
    """
    if len(matched_list) == 1:
        article = matched_list[0]
        nrec_str = f"[{format_nrec_display(article['nrec'])}]"
        show_author = article.get("_show_author", False)
        if config.COMPACT_NOTIFICATION:
            text = (
                f"🚨 *[{article['board']}]* {nrec_str} {escape_markdown_v1(article['title'])}\n"
                f"{article['url']}"
            )
            if show_author:
                text += f"\n👤 `{article['author']}`"
        else:
            author_field = f"👤 *作者：* `{article['author']}`\n" if show_author else ""
            text = (
                f"🚨 *【PTT Alert】看板：#{article['board']}*\n\n"
                f"📌 *標題：* {escape_markdown_v1(article['title'])}\n"
                f"{author_field}"
                f"🔥 *人氣：* {format_nrec_display(article['nrec'])}\n"
                f"📅 *日期：* {article['date']}\n"
                f"🔗 {article['url']}"
            )
        return [{"text": text, "articles": matched_list}]

    header = f"🚨 *【PTT 訂閱通知】共 {len(matched_list)} 篇新文章：*\n"
    chunks: List[Dict[str, Any]] = []
    cur_lines = [header]
    cur_articles: List[Dict[str, Any]] = []
    cur_len = len(header)

    for article in matched_list:
        line = format_article_line(article)
        # +1 for the joining newline. Flush the current chunk if adding this
        # line would exceed the safe limit (and the chunk already has content).
        if cur_articles and cur_len + len(line) + 1 > TELEGRAM_MAX_MSG_LEN:
            chunks.append({"text": "\n".join(cur_lines), "articles": cur_articles})
            cur_lines = [header]
            cur_articles = []
            cur_len = len(header)
        cur_lines.append(line)
        cur_articles.append(article)
        cur_len += len(line) + 1

    if cur_articles:
        chunks.append({"text": "\n".join(cur_lines), "articles": cur_articles})
    return chunks


async def check_ptt_job(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Periodic job to fetch PTT articles and match against keyword/author/push/boo subscriptions.

    Returns the number of articles successfully pushed in this cycle (0 if none),
    so callers like /check can decide whether to run a delivery-channel self-test.
    """
    global last_ptt_check_time
    now = time.time()
    required_interval = config.get_current_check_interval()

    # Dynamic check interval control (300s day / 1800s night)
    if now - last_ptt_check_time < required_interval - 5:
        return 0
    last_ptt_check_time = now

    boards = db.get_subscribed_boards()
    if not boards:
        return 0

    all_subs = db.get_all_subscriptions()
    if not all_subs:
        return 0

    is_night = config.is_night_mode()

    # Map chat_id -> List of matched articles for batching
    user_notifications: Dict[int, List[Dict[str, Any]]] = {}

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for board in boards:
            articles = await fetch_board_articles(board, client=client)
            board_subs = [s for s in all_subs if s.get("board", "").lower() == board.lower()]

            for article in articles:
                article_id = article["article_id"]
                if db.is_article_pushed(article_id):
                    continue
                if is_article_too_old(article_id):
                    continue

                matched_chat_author: Dict[int, bool] = {}
                for sub in board_subs:
                    if match_subscription(article, sub):
                        cid = sub["chat_id"]
                        is_author_match = sub.get("sub_type", "").lower() == "author"
                        matched_chat_author[cid] = matched_chat_author.get(cid, False) or is_author_match

                if matched_chat_author:
                    for cid, show_author in matched_chat_author.items():
                        if is_user_allowed(cid):
                            if cid not in user_notifications:
                                user_notifications[cid] = []
                            user_notifications[cid].append({**article, "_show_author": show_author})
                    # NOTE: do NOT mark as pushed here. Marking happens only
                    # after the message is successfully delivered (see below),
                    # so a failed send does not permanently suppress the alert.

    # Dispatch per user, splitting large batches into multiple messages so a
    # long digest is never rejected by Telegram's 4096-char limit.
    pushed_count = 0
    for cid, matched_list in user_notifications.items():
        if not matched_list:
            continue

        for chunk in build_notification_chunks(matched_list):
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text=chunk["text"],
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    disable_notification=is_night,
                )
                # Mark as pushed only after this chunk is delivered, so a failed
                # send is retried next cycle instead of being lost forever.
                for article in chunk["articles"]:
                    db.mark_article_pushed(article["article_id"], article["board"])
                pushed_count += len(chunk["articles"])
            except Exception as e:
                logger.error(f"Failed to send alert to {cid}: {e}")

    return pushed_count


async def check_tracked_articles_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job to check tracked PTT articles for new comments."""
    global last_article_check_time
    now = time.time()
    required_interval = config.get_current_check_interval()

    if now - last_article_check_time < required_interval - 5:
        return
    last_article_check_time = now

    tracked = db.get_tracked_articles()
    if not tracked:
        return

    is_night = config.is_night_mode()

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for t in tracked:
            tid = t["id"]
            chat_id = t["chat_id"]
            url = t["article_url"]
            last_cnt = t["last_comment_count"]

            if not is_user_allowed(chat_id):
                continue

            details = await fetch_article_details(url, client=client)
            total = details["total_comments"]

            if total > last_cnt:
                new_comments = details["comments"][last_cnt:]
                comments_text = "\n".join(new_comments[-3:])
                if config.COMPACT_NOTIFICATION:
                    msg = (
                        f"💬 *[{t['board']}]* 🆕 新增 {total - last_cnt} 則推文：\n"
                        f"[{details['title'] or t['article_id']}]({url})\n"
                        f"```{comments_text}```"
                    )
                else:
                    msg = (
                        f"💬 *【PTT 文章新推文通知】*\n\n"
                        f"📌 *標題：* {details['title'] or t['article_id']}\n"
                        f"🆕 新增 {total - last_cnt} 則推文：\n"
                        f"```{comments_text}```\n"
                        f"🔗 [查看完整文章]({url})"
                    )
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=False,
                        disable_notification=is_night,
                    )
                    db.update_article_comment_count(tid, total)
                except Exception as e:
                    logger.error(f"Failed to send comment notification for {url}: {e}")


async def cleanup_pushed_articles_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job to purge pushed_articles older than the retention window.

    Old entries reference articles that have long scrolled off the board index
    and can never be matched again, so removing them keeps the dedup table small
    without any risk of re-notifying a stale article.
    """
    try:
        deleted = db.clean_old_pushed_articles(days=7)
        logger.info(f"🧹 已清理 {deleted} 筆 7 天前的 pushed_articles 去重記錄。")
    except Exception as e:
        logger.error(f"Failed to clean old pushed_articles: {e}")


def main() -> None:
    """Initialize DB and launch Telegram bot."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in environment or config.py!")
        print("\n❌ 錯誤：未設定 TELEGRAM_BOT_TOKEN！請先在 .env 檔案中填寫 Token。\n")
        return

    db.init_db()
    logger.info("Database initialized successfully.")

    application = ApplicationBuilder().post_init(setup_bot_commands).token(config.TELEGRAM_BOT_TOKEN).build()

    # Register uncaught exception error handler
    application.add_error_handler(error_handler)

    # Slash command handlers
    application.add_handler(CommandHandler("start", help_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("myid", myid_handler))
    application.add_handler(CommandHandler("check", check_now_handler))
    application.add_handler(CommandHandler("night", night_status_handler))
    application.add_handler(CommandHandler("list", list_handler))
    application.add_handler(CommandHandler("top", top_handler))
    application.add_handler(CommandHandler("del", del_by_id_handler))
    application.add_handler(CommandHandler("allow", allow_handler))
    application.add_handler(CommandHandler("deny", deny_handler))
    application.add_handler(CommandHandler("whitelist", whitelist_handler))
    application.add_handler(CommandHandler("errors", list_errors_handler))
    application.add_handler(CommandHandler("err_detail", error_detail_handler))
    application.add_handler(CommandHandler("err_resolve", resolve_error_handler))

    # Text message parser for natural Chinese Ptt-Alertor commands
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_command_handler)
    )

    # Register repeating jobs
    job_queue = application.job_queue
    if job_queue:
        # Check every 60s, internally controlled by get_current_check_interval()
        job_queue.run_repeating(
            check_ptt_job, interval=60, first=5
        )
        job_queue.run_repeating(
            check_tracked_articles_job, interval=60, first=10
        )
        # Daily cleanup of the dedup table (04:00 local time, during quiet hours)
        job_queue.run_daily(
            cleanup_pushed_articles_job, time=dt_time(hour=4, minute=0)
        )
        logger.info("Registered PTT board & article comment check jobs.")

    logger.info("PTT Alert Bot starting polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
