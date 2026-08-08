import re
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
import httpx
from bs4 import BeautifulSoup
import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
COOKIES = {"over18": "1"}

# Consecutive failures required before the circuit breaker trips into cooldown.
# Below this threshold, failures are logged but crawling retries next cycle,
# so a single transient network blip does not freeze the bot for 15 minutes.
FAILURE_THRESHOLD = 3


class CircuitBreaker:
    """Circuit breaker for handling PTT network outages and downtime with exponential cooldown."""

    def __init__(self):
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def is_in_cooldown(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            remaining = int(self.cooldown_until - now)
            logger.info(
                f"⚠️ PTT 目前處於停機/連線失敗冷卻期中，尚需等待 {remaining} 秒後重試。"
            )
            return True
        return False

    def record_success(self) -> None:
        if self.consecutive_failures > 0:
            logger.info("🟢 PTT 連線已恢復正常！已重置連線失敗計數。")
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def record_failure(self, error_msg: str) -> None:
        self.consecutive_failures += 1
        # Only trip the breaker after several consecutive failures so a single
        # transient network blip (timeout / empty error) does not freeze crawling.
        # 1st-2nd failure: log only, no cooldown (retry next cycle).
        # 3rd failure: 15 mins (900s)
        # 4th failure: 30 mins (1800s)
        # 5th+ failure: 1 hour (3600s)
        if self.consecutive_failures < FAILURE_THRESHOLD:
            logger.warning(
                f"⚠️ PTT 連線失敗 ({error_msg})。連續失敗次數: {self.consecutive_failures}"
                f"/{FAILURE_THRESHOLD}，尚未達熔斷門檻，下一輪將重試。"
            )
            return

        over = self.consecutive_failures - FAILURE_THRESHOLD
        if over == 0:
            cooldown_sec = 15 * 60
        elif over == 1:
            cooldown_sec = 30 * 60
        else:
            cooldown_sec = 60 * 60

        self.cooldown_until = time.time() + cooldown_sec
        logger.warning(
            f"❌ PTT 連線失敗 ({error_msg})。連續失敗次數: {self.consecutive_failures}。"
            f"進入 {cooldown_sec // 60} 分鐘冷卻保護模式。"
        )


circuit_breaker = CircuitBreaker()


def parse_article_id(href: str) -> str:
    """Extract article ID from PTT URL href like '/bbs/Stock/M.170000.A.123.html'."""
    match = re.search(r"/(M\.\d+\.A\.[A-Za-z0-9_-]+)\.html$", href)
    if match:
        return match.group(1)
    return href.split("/")[-1].replace(".html", "")


def parse_nrec_to_int(nrec: str) -> int:
    """Convert PTT nrec string (e.g. '爆', '10', 'X1') to integer."""
    if not nrec:
        return 0
    if nrec == "爆":
        return 100
    if nrec == "XX":
        return -100
    if nrec.startswith("X") and len(nrec) == 2 and nrec[1].isdigit():
        return -int(nrec[1]) * 10
    try:
        return int(nrec)
    except ValueError:
        return 0


def parse_board_html(html_content: str, board: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Parse PTT board index HTML content, return list of article objects and prev_page_url."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []

    r_ents = soup.select("#main-container .r-ent")
    for rent in r_ents:
        title_el = rent.select_one(".title a")
        if not title_el:
            # Article deleted or unavailable
            continue

        href = title_el.get("href", "")
        title = title_el.get_text(strip=True)
        article_id = parse_article_id(href)
        url = f"{config.PTT_DOMAIN}{href}" if href.startswith("/") else href

        author_el = rent.select_one(".meta .author")
        author = author_el.get_text(strip=True) if author_el else ""

        date_el = rent.select_one(".meta .date")
        date = date_el.get_text(strip=True) if date_el else ""

        nrec_el = rent.select_one(".nrec")
        nrec_raw = nrec_el.get_text(strip=True) if nrec_el else ""
        nrec_val = parse_nrec_to_int(nrec_raw)

        articles.append(
            {
                "article_id": article_id,
                "title": title,
                "author": author,
                "url": url,
                "board": board,
                "date": date,
                "nrec": nrec_raw,
                "nrec_val": nrec_val,
            }
        )

    # Extract previous page URL (‹ 上頁)
    prev_url = None
    btn_group = soup.select(".btn-group-paging a")
    for btn in btn_group:
        if "上頁" in btn.get_text():
            prev_href = btn.get("href", "")
            if prev_href and "index" in prev_href:
                prev_url = f"{config.PTT_DOMAIN}{prev_href}" if prev_href.startswith("/") else prev_href
            break

    return articles, prev_url


async def fetch_board_articles(
    board: str, pages: int = 3, client: Optional[httpx.AsyncClient] = None
) -> List[Dict[str, Any]]:
    """Fetch multiple pages of articles from PTT board via HTTPS with circuit breaker outage protection."""
    if circuit_breaker.is_in_cooldown():
        return []

    current_url = f"{config.PTT_DOMAIN}/bbs/{board}/index.html"

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        should_close = True

    all_articles = []
    seen_ids = set()

    try:
        for _ in range(pages):
            if not current_url:
                break
            response = await client.get(current_url, headers=HEADERS, cookies=COOKIES)
            if response.status_code != 200:
                circuit_breaker.record_failure(f"HTTP {response.status_code}")
                return all_articles

            articles, prev_url = parse_board_html(response.text, board)
            for art in articles:
                if art["article_id"] not in seen_ids:
                    seen_ids.add(art["article_id"])
                    all_articles.append(art)

            current_url = prev_url

        # Success across pages
        circuit_breaker.record_success()
        return all_articles
    except Exception as e:
        circuit_breaker.record_failure(str(e))
        return all_articles
    finally:
        if should_close:
            await client.aclose()


async def fetch_article_details(
    article_url: str, client: Optional[httpx.AsyncClient] = None
) -> Dict[str, Any]:
    """Fetch a single article page with outage check."""
    if circuit_breaker.is_in_cooldown():
        return {"title": "", "comments": [], "total_comments": 0}

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        should_close = True

    try:
        response = await client.get(article_url, headers=HEADERS, cookies=COOKIES)
        if response.status_code != 200:
            circuit_breaker.record_failure(f"HTTP {response.status_code}")
            return {"title": "", "comments": [], "total_comments": 0}

        soup = BeautifulSoup(response.text, "html.parser")
        title_el = soup.select_one('meta[property="og:title"]')
        title = title_el["content"] if title_el and "content" in title_el.attrs else ""

        push_rows = soup.select("#main-container .push")
        comments = []
        for push in push_rows:
            tag = push.select_one(".push-tag")
            userid = push.select_one(".push-userid")
            content = push.select_one(".push-content")
            if tag and userid and content:
                comments.append(
                    f"{tag.get_text(strip=True)} {userid.get_text(strip=True)}{content.get_text(strip=True)}"
                )

        circuit_breaker.record_success()
        return {
            "title": title,
            "comments": comments,
            "total_comments": len(comments),
        }
    except Exception as e:
        circuit_breaker.record_failure(str(e))
        return {"title": "", "comments": [], "total_comments": 0}
    finally:
        if should_close:
            await client.aclose()


def match_subscription(article: Dict[str, Any], subscription: Dict[str, Any]) -> bool:
    """Check if an article matches a subscription rule based on sub_type."""
    sub_type = subscription.get("sub_type", "").lower()
    sub_board = subscription.get("board", "").strip()
    art_board = article.get("board", "").strip()

    # Board must match if sub_board is specified
    if sub_board and sub_board.lower() != art_board.lower():
        return False

    target = subscription.get("target", "").strip()

    if sub_type == "keyword":
        return target.lower() in article.get("title", "").lower()
    elif sub_type == "author":
        return target.lower() == article.get("author", "").lower()
    elif sub_type == "push":
        try:
            threshold = int(target)
            return article.get("nrec_val", 0) >= threshold
        except ValueError:
            return False
    elif sub_type == "boo":
        try:
            threshold = int(target)
            return article.get("nrec_val", 0) <= -threshold
        except ValueError:
            return False

    return False


async def check_board_exists(board: str, client: Optional[httpx.AsyncClient] = None) -> bool:
    """Verify if a PTT board exists by checking HTTP status of its index page."""
    if not board:
        return False
    url = f"{config.PTT_DOMAIN}/bbs/{board}/index.html"
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=5.0, follow_redirects=True)
        should_close = True
    try:
        response = await client.get(url, headers=HEADERS, cookies=COOKIES)
        return response.status_code == 200
    except Exception:
        return False
    finally:
        if should_close:
            await client.aclose()

