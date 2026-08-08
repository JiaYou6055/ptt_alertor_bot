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
        config.ALLOWED_USER_IDS = []

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        config.ALLOWED_USER_IDS = []

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
        update = MagicMock()
        update.message.chat_id = 999
        update.message.text = "立即檢查"
        update.message.reply_text = AsyncMock()

        context = MagicMock()

        with patch("bot.check_ptt_job", new=AsyncMock()), patch("bot.check_tracked_articles_job", new=AsyncMock()):
            asyncio.run(bot.text_command_handler(update, context))
        self.assertEqual(update.message.reply_text.call_count, 2)
        self.assertIn("正在立即抓取", update.message.reply_text.call_args_list[0][0][0])
        self.assertIn("立即抓取完成", update.message.reply_text.call_args_list[1][0][0])

if __name__ == "__main__":
    unittest.main()
