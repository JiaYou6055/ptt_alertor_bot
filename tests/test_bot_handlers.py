import unittest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from database import Database
import bot
import config

class TestBotHandlers(unittest.TestCase):
    def setUp(self):
        self.db_path = "test_bot_handlers.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        bot.db = Database(self.db_path)
        bot.db.init_db()
        # Isolate whitelist config so tests don't depend on the local .env
        # (ADMIN_USER_ID leaking in would silently block chat_id 999).
        self._orig_allowed = config.ALLOWED_USER_IDS
        self._orig_admin = config.ADMIN_USER_ID
        config.ALLOWED_USER_IDS = []
        config.ADMIN_USER_ID = None

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        config.ALLOWED_USER_IDS = self._orig_allowed
        config.ADMIN_USER_ID = self._orig_admin

    def test_text_command_add_keyword(self):
        update = MagicMock()
        update.message.chat_id = 999
        update.message.text = "新增 gossiping,movie 金城武,結衣"
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with patch("bot.check_board_exists", new=AsyncMock(return_value=True)):
            asyncio.run(bot.text_command_handler(update, context))
        update.message.reply_text.assert_called_once()
        self.assertIn("成功新增關鍵字訂閱", update.message.reply_text.call_args[0][0])

        subs = bot.db.get_user_subscriptions(999)
        self.assertEqual(len(subs), 4)

    def test_whitelist_blocking(self):
        config.ALLOWED_USER_IDS = [111, 222]

        update = MagicMock()
        update.message.chat_id = 999  # Unauthorized ID
        update.message.text = "清單"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.text_command_handler(update, context))
        self.assertIn("無權使用", update.message.reply_text.call_args[0][0])

    def test_dynamic_allow_and_deny(self):
        config.ALLOWED_USER_IDS = [111]  # Admin is 111

        # Admin allows user 888
        update = MagicMock()
        update.message.chat_id = 111
        update.message.text = "授權 888"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.text_command_handler(update, context))
        self.assertIn("成功新增授權", update.message.reply_text.call_args[0][0])
        self.assertTrue(bot.is_user_allowed(888))

        # Admin revokes user 888
        update.message.reply_text.reset_mock()
        update.message.text = "取消授權 888"
        asyncio.run(bot.text_command_handler(update, context))
        self.assertIn("已成功移除", update.message.reply_text.call_args[0][0])
        self.assertFalse(bot.is_user_allowed(888))

    def test_night_mode_config(self):
        self.assertTrue(config.NIGHT_MODE_ENABLED)
        self.assertEqual(config.NIGHT_START_HOUR, 1)
        self.assertEqual(config.NIGHT_END_HOUR, 7)
        self.assertEqual(config.NIGHT_CHECK_INTERVAL_SECONDS, 1800)

    def test_check_now_handler(self):
        # check_ptt_job returns >0 -> normal "pushed N articles" reply, no self-test
        update = MagicMock()
        update.message.chat_id = 999
        update.message.text = "立即檢查"
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with patch("bot.check_ptt_job", new=AsyncMock(return_value=3)), \
             patch("bot.check_tracked_articles_job", new=AsyncMock()), \
             patch("bot.resend_latest_matched", new=AsyncMock()) as mock_resend:
            asyncio.run(bot.text_command_handler(update, context))
        self.assertEqual(update.message.reply_text.call_count, 2)
        self.assertIn("正在立即抓取", update.message.reply_text.call_args_list[0][0][0])
        self.assertIn("已推播 3 篇", update.message.reply_text.call_args_list[1][0][0])
        mock_resend.assert_not_called()

    def test_check_now_handler_self_test_when_no_new(self):
        # check_ptt_job returns 0 -> fall back to resend_latest_matched self-test
        update = MagicMock()
        update.message.chat_id = 999
        update.message.text = "立即檢查"
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with patch("bot.check_ptt_job", new=AsyncMock(return_value=0)), \
             patch("bot.check_tracked_articles_job", new=AsyncMock()), \
             patch("bot.resend_latest_matched", new=AsyncMock(return_value=True)) as mock_resend:
            asyncio.run(bot.text_command_handler(update, context))
        mock_resend.assert_called_once()
        self.assertIn("重送最新一篇", update.message.reply_text.call_args_list[1][0][0])

    def _make_article(self, i):
        return {
            "article_id": f"M.{i}.A.ABC",
            "title": "測試文章標題" * 5,  # long-ish title to grow the digest
            "url": f"https://www.ptt.cc/bbs/stock/M.{i}.A.ABC.html",
            "author": f"user{i}",
            "board": "stock",
            "nrec": "50",
            "date": "8/08",
        }

    def test_build_notification_chunks_single(self):
        arts = [self._make_article(1)]
        chunks = bot.build_notification_chunks(arts)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["articles"], arts)

    def test_build_notification_chunks_splits_long_batch(self):
        # 100 articles must be split into multiple chunks, each under the limit,
        # and every article must appear exactly once across all chunks.
        arts = [self._make_article(i) for i in range(100)]
        chunks = bot.build_notification_chunks(arts)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text"]), bot.TELEGRAM_MAX_MSG_LEN)
        total = sum(len(c["articles"]) for c in chunks)
        self.assertEqual(total, 100)


    def test_error_handler_logging_and_admin_notify(self):
        config.ADMIN_USER_ID = 111
        bot.last_error_notify_time.clear()

        context = MagicMock()
        context.error = ValueError("Test system exception")
        context.bot.send_message = AsyncMock()

        asyncio.run(bot.error_handler(None, context))

        # Check error was logged in DB
        pending = bot.db.get_pending_errors()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["error_type"], "ValueError")
        self.assertIn("Test system exception", pending[0]["message"])

        # Check Admin received Telegram notification
        context.bot.send_message.assert_called_once()
        sent_msg = context.bot.send_message.call_args[1]["text"]
        self.assertIn("【系統異常通知】", sent_msg)
        self.assertIn("ValueError", sent_msg)

    def test_admin_error_commands(self):
        config.ADMIN_USER_ID = 111

        # 1. Insert a test error into DB
        res = bot.db.log_system_error("RuntimeError", "Sample failure", "Traceback details...")
        err_id = res["id"]

        # 2. Test list_errors_handler (/errors)
        update = MagicMock()
        update.effective_chat.id = 111
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.list_errors_handler(update, context))
        update.message.reply_text.assert_called_once()
        self.assertIn("【未解決系統異常清單】", update.message.reply_text.call_args[0][0])
        self.assertIn("RuntimeError", update.message.reply_text.call_args[0][0])

        # 3. Test error_detail_handler (/err_detail 1)
        update.message.reply_text.reset_mock()
        context.args = [str(err_id)]
        asyncio.run(bot.error_detail_handler(update, context))
        self.assertIn("【異常詳細紀錄 ID:", update.message.reply_text.call_args[0][0])
        self.assertIn("Traceback details...", update.message.reply_text.call_args[0][0])

        # 4. Test resolve_error_handler (/err_resolve 1)
        update.message.reply_text.reset_mock()
        context.args = [str(err_id)]
        asyncio.run(bot.resolve_error_handler(update, context))
        self.assertIn("已成功將異常 ID", update.message.reply_text.call_args[0][0])

        # Verify DB status is resolved
        pending_after = bot.db.get_pending_errors()
        self.assertEqual(len(pending_after), 0)


if __name__ == "__main__":
    unittest.main()

