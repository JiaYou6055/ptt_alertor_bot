import os
import unittest
from database import Database

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.test_db_path = "test_ptt_alert.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        self.db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_add_and_get_subscription(self):
        sub_ids = self.db.add_subscription(chat_id=123456, sub_type="keyword", board="Stock,movie", target="台積電,結衣")
        self.assertEqual(len(sub_ids), 4)

        subs = self.db.get_user_subscriptions(chat_id=123456)
        self.assertEqual(len(subs), 4)

    def test_delete_subscription(self):
        self.db.add_subscription(chat_id=123456, sub_type="author", board="gossiping", target="obov")
        del_cnt = self.db.delete_subscription(chat_id=123456, sub_type="author", board="gossiping", target="obov")
        self.assertEqual(del_cnt, 1)

        subs = self.db.get_user_subscriptions(chat_id=123456)
        self.assertEqual(len(subs), 0)

    def test_top_subscriptions(self):
        self.db.add_subscription(chat_id=1, sub_type="keyword", board="Stock", target="台積電")
        self.db.add_subscription(chat_id=2, sub_type="keyword", board="Stock", target="台積電")
        self.db.add_subscription(chat_id=3, sub_type="keyword", board="Stock", target="聯發科")

        top = self.db.get_top_subscriptions(limit=5)
        self.assertEqual(len(top["keywords"]), 2)
        self.assertEqual(top["keywords"][0][0], "台積電")
        self.assertEqual(top["keywords"][0][1], 2)

    def test_article_tracking(self):
        url = "https://www.ptt.cc/bbs/EZsoft/M.1708247900.A.27C.html"
        ok = self.db.add_article_tracking(123, url, "EZsoft", "M.1708247900.A.27C", 5)
        self.assertTrue(ok)

        tracked = self.db.get_tracked_articles()
        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0]["last_comment_count"], 5)

    def test_system_error_logging_and_resolving(self):
        # 1. Log a new error
        res1 = self.db.log_system_error("ConnectTimeout", "HTTPX connection timed out", "Traceback line 1...")
        self.assertTrue(res1["is_new"])
        self.assertEqual(res1["occurrence_count"], 1)
        err_id = res1["id"]

        # 2. Log duplicate pending error (should update occurrence_count)
        res2 = self.db.log_system_error("ConnectTimeout", "HTTPX connection timed out", "Traceback line 2...")
        self.assertFalse(res2["is_new"])
        self.assertEqual(res2["id"], err_id)
        self.assertEqual(res2["occurrence_count"], 2)

        # 3. Get pending errors
        pending = self.db.get_pending_errors()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], err_id)
        self.assertEqual(pending[0]["occurrence_count"], 2)

        # 4. Get error by ID
        detail = self.db.get_error_by_id(err_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["error_type"], "ConnectTimeout")

        # 5. Resolve error
        ok = self.db.resolve_error(err_id)
        self.assertTrue(ok)

        # 6. Verify pending errors is now empty
        pending_after = self.db.get_pending_errors()
        self.assertEqual(len(pending_after), 0)

if __name__ == "__main__":
    unittest.main()

