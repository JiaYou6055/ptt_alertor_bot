import sqlite3
from typing import List, Dict, Any, Optional, Tuple, Set

class Database:
    def __init__(self, db_path: str = "ptt_alert.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Main subscriptions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    sub_type TEXT NOT NULL,
                    board TEXT,
                    target TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Pushed articles cache
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pushed_articles (
                    article_id TEXT PRIMARY KEY,
                    board TEXT NOT NULL,
                    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Tracked articles table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    article_url TEXT NOT NULL,
                    board TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    last_comment_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, article_id)
                );
            """)
            # Dynamic whitelist table for users with name support
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS allowed_users (
                    chat_id INTEGER PRIMARY KEY,
                    name TEXT DEFAULT '',
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # System errors table for error tracking & deduplication
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    traceback TEXT NOT NULL,
                    occurrence_count INTEGER DEFAULT 1,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending'
                );
            """)
            # Migration check: add name column if missing from older DB
            cursor.execute("PRAGMA table_info(allowed_users);")
            columns = [row["name"] for row in cursor.fetchall()]
            if "name" not in columns:
                cursor.execute("ALTER TABLE allowed_users ADD COLUMN name TEXT DEFAULT '';")

            conn.commit()

    def add_allowed_user(self, chat_id: int, name: str = "", added_by: int = 0) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO allowed_users (chat_id, name, added_by) 
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET 
                    name = CASE WHEN ? != '' THEN ? ELSE name END,
                    added_by = ?
                """,
                (chat_id, name, added_by, name, name, added_by),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_user_name(self, chat_id: int, name: str) -> None:
        if not name:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE allowed_users SET name = ? WHERE chat_id = ?",
                (name, chat_id),
            )
            conn.commit()

    def remove_allowed_user(self, chat_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM allowed_users WHERE chat_id = ?", (chat_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_db_allowed_users(self) -> Set[int]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id FROM allowed_users")
            return {row["chat_id"] for row in cursor.fetchall()}

    def get_db_allowed_users_with_names(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, name, created_at FROM allowed_users ORDER BY created_at ASC")
            return [dict(row) for row in cursor.fetchall()]

    def add_subscription(self, chat_id: int, sub_type: str, board: str, target: str) -> List[int]:
        """Support comma-separated boards and targets, returning list of new sub IDs."""
        boards = [b.strip() for b in board.split(",") if b.strip()] if board else [""]
        targets = [t.strip() for t in target.split(",") if t.strip()]

        created_ids = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for b in boards:
                for t in targets:
                    cursor.execute(
                        """
                        INSERT INTO subscriptions (chat_id, sub_type, board, target)
                        VALUES (?, ?, ?, ?)
                        """,
                        (chat_id, sub_type.lower(), b, t),
                    )
                    created_ids.append(cursor.lastrowid)
            conn.commit()
        return created_ids

    def delete_subscription(self, chat_id: int, sub_type: str, board: str, target: str) -> int:
        """Delete specific subscription matching type, board, and target."""
        boards = [b.strip() for b in board.split(",") if b.strip()] if board else [""]
        targets = [t.strip() for t in target.split(",") if t.strip()]

        deleted_count = 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for b in boards:
                for t in targets:
                    cursor.execute(
                        """
                        DELETE FROM subscriptions 
                        WHERE chat_id = ? AND sub_type = ? AND LOWER(board) = LOWER(?) AND LOWER(target) = LOWER(?)
                        """,
                        (chat_id, sub_type.lower(), b, t),
                    )
                    deleted_count += cursor.rowcount
            conn.commit()
        return deleted_count

    def delete_subscription_by_id(self, sub_id: int, chat_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
                (sub_id, chat_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def add_article_tracking(self, chat_id: int, article_url: str, board: str, article_id: str, last_comment_count: int = 0) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO tracked_articles (chat_id, article_url, board, article_id, last_comment_count)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, article_url, board, article_id, last_comment_count),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def delete_article_tracking(self, chat_id: int, article_url_or_id: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM tracked_articles 
                WHERE chat_id = ? AND (article_url = ? OR article_id = ?)
                """,
                (chat_id, article_url_or_id, article_url_or_id),
            )
            conn.commit()
            return cursor.rowcount

    def update_article_comment_count(self, tracking_id: int, new_count: int) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tracked_articles SET last_comment_count = ? WHERE id = ?",
                (new_count, tracking_id),
            )
            conn.commit()

    def get_tracked_articles(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tracked_articles")
            return [dict(row) for row in cursor.fetchall()]

    def get_user_subscriptions(self, chat_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id DESC",
                (chat_id,),
            )
            subs = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                "SELECT * FROM tracked_articles WHERE chat_id = ? ORDER BY id DESC",
                (chat_id,),
            )
            tracked = [dict(row) for row in cursor.fetchall()]
            for t in tracked:
                subs.append({
                    "id": f"art_{t['id']}",
                    "chat_id": t["chat_id"],
                    "sub_type": "article",
                    "board": t["board"],
                    "target": t["article_url"],
                    "created_at": t["created_at"]
                })
            return subs

    def get_all_subscriptions(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM subscriptions")
            return [dict(row) for row in cursor.fetchall()]

    def get_subscribed_boards(self) -> List[str]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT board FROM subscriptions WHERE board IS NOT NULL AND board != ''")
            return [row["board"] for row in cursor.fetchall()]

    def get_top_subscriptions(self, limit: int = 5) -> Dict[str, List[Tuple[str, int]]]:
        """Return top keywords and authors across all users."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT target, COUNT(*) as cnt 
                FROM subscriptions 
                WHERE sub_type = 'keyword' 
                GROUP BY LOWER(target) 
                ORDER BY cnt DESC LIMIT ?
                """,
                (limit,),
            )
            top_keywords = [(row["target"], row["cnt"]) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT target, COUNT(*) as cnt 
                FROM subscriptions 
                WHERE sub_type = 'author' 
                GROUP BY LOWER(target) 
                ORDER BY cnt DESC LIMIT ?
                """,
                (limit,),
            )
            top_authors = [(row["target"], row["cnt"]) for row in cursor.fetchall()]

            return {"keywords": top_keywords, "authors": top_authors}

    def is_article_pushed(self, article_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM pushed_articles WHERE article_id = ?", (article_id,)
            )
            return cursor.fetchone() is not None

    def mark_article_pushed(self, article_id: str, board: str) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO pushed_articles (article_id, board) VALUES (?, ?)",
                (article_id, board),
            )
            conn.commit()

    def clean_old_pushed_articles(self, days: int = 7) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM pushed_articles WHERE pushed_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            conn.commit()
            return cursor.rowcount

    def log_system_error(
        self, error_type: str, message: str, traceback_str: str = ""
    ) -> Dict[str, Any]:
        """Record or aggregate a system error. Returns dict with error id, count, and status."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, occurrence_count, last_seen 
                FROM system_errors 
                WHERE error_type = ? AND message = ? AND status = 'pending'
                ORDER BY last_seen DESC LIMIT 1
                """,
                (error_type, message),
            )
            row = cursor.fetchone()
            if row:
                err_id = row["id"]
                new_count = row["occurrence_count"] + 1
                cursor.execute(
                    """
                    UPDATE system_errors 
                    SET occurrence_count = ?, last_seen = CURRENT_TIMESTAMP, traceback = ?
                    WHERE id = ?
                    """,
                    (new_count, traceback_str, err_id),
                )
                conn.commit()
                return {
                    "id": err_id,
                    "occurrence_count": new_count,
                    "is_new": False,
                    "last_seen": row["last_seen"],
                }
            else:
                cursor.execute(
                    """
                    INSERT INTO system_errors (error_type, message, traceback)
                    VALUES (?, ?, ?)
                    """,
                    (error_type, message, traceback_str),
                )
                err_id = cursor.lastrowid
                conn.commit()
                return {
                    "id": err_id,
                    "occurrence_count": 1,
                    "is_new": True,
                    "last_seen": None,
                }

    def get_pending_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch pending system errors ordered by last_seen desc."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, error_type, message, occurrence_count, first_seen, last_seen, status
                FROM system_errors
                WHERE status = 'pending'
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_error_by_id(self, error_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a specific system error by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM system_errors WHERE id = ?", (error_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def resolve_error(self, error_id: int) -> bool:
        """Mark a system error as resolved."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE system_errors SET status = 'resolved' WHERE id = ?", (error_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

