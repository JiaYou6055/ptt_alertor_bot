import unittest
import time
from crawler import (
    parse_board_html,
    parse_article_id,
    parse_nrec_to_int,
    match_subscription,
    circuit_breaker,
    is_article_too_old,
)

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>看板 Stock 文章列表 - 批踢踢實業坊</title></head>
<body>
<div id="main-container">
    <div class="action-bar">
        <div class="btn-group btn-group-paging">
            <a class="btn wide" href="/bbs/Stock/index100.html">‹ 上頁</a>
        </div>
    </div>
    <div class="r-ent">
        <div class="nrec"><span class="hl f1">爆</span></div>
        <div class="title">
            <a href="/bbs/Stock/M.1700000001.A.100.html">[標題] 台積電 法人說明會重點整理</a>
        </div>
        <div class="meta">
            <div class="author">kevin</div>
            <div class="date"> 8/08</div>
        </div>
    </div>
</div>
</body>
</html>
"""

class TestCrawler(unittest.TestCase):
    def setUp(self):
        circuit_breaker.record_success()

    def test_circuit_breaker_cooldown(self):
        self.assertFalse(circuit_breaker.is_in_cooldown())

        # Failures below FAILURE_THRESHOLD (3) only count, no cooldown yet,
        # so a single transient blip does not freeze crawling.
        circuit_breaker.record_failure("HTTP 503")
        self.assertFalse(circuit_breaker.is_in_cooldown())
        self.assertEqual(circuit_breaker.consecutive_failures, 1)

        circuit_breaker.record_failure("HTTP 503")
        self.assertFalse(circuit_breaker.is_in_cooldown())
        self.assertEqual(circuit_breaker.consecutive_failures, 2)

        # 3rd consecutive failure trips the breaker -> 15 min cooldown
        circuit_breaker.record_failure("HTTP 503")
        self.assertTrue(circuit_breaker.is_in_cooldown())
        self.assertEqual(circuit_breaker.consecutive_failures, 3)

        # Success resets circuit breaker
        circuit_breaker.record_success()
        self.assertFalse(circuit_breaker.is_in_cooldown())
        self.assertEqual(circuit_breaker.consecutive_failures, 0)

    def test_parse_nrec_to_int(self):
        self.assertEqual(parse_nrec_to_int("爆"), 100)
        self.assertEqual(parse_nrec_to_int("10"), 10)
        self.assertEqual(parse_nrec_to_int("X2"), -20)

    def test_is_article_too_old(self):
        recent_id = f"M.{int(time.time()) - 3600}.A.100"  # 1 hour ago
        old_id = f"M.{int(time.time()) - 8 * 86400}.A.100"  # 8 days ago
        within_boundary_id = f"M.{int(time.time()) - 7 * 86400 + 60}.A.100"  # just under 7 days ago
        self.assertFalse(is_article_too_old(recent_id))
        self.assertTrue(is_article_too_old(old_id))
        self.assertFalse(is_article_too_old(within_boundary_id))
        # Unparsable id must not be dropped from matching
        self.assertFalse(is_article_too_old("not-a-valid-id"))

    def test_parse_board_html_with_prev_url(self):
        articles, prev_url = parse_board_html(SAMPLE_HTML, "Stock")
        self.assertEqual(len(articles), 1)
        self.assertEqual(prev_url, "https://www.ptt.cc/bbs/Stock/index100.html")

if __name__ == "__main__":
    unittest.main()
