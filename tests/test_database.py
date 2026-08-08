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

if __name__ == "__main__":
    unittest.main()
