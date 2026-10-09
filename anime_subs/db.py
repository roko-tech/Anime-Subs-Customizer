"""SQLite connection pool + DatabaseManager."""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from queue import Queue, Empty
from typing import Optional, Dict, List, Any, Set, Tuple

from .runtime import *
from .runtime import file_hash  # noqa: F401

__all__ = [
    "shutdown_db_pool", "ConnectionPool", "get_db_pool", "get_db_connection",
    "DatabaseManager",
]


def shutdown_db_pool() -> None:
    """Close the global pool and block resurrection (called from AppState)."""
    global _db_pool, _db_pool_shutdown
    _db_pool_shutdown = True
    if _db_pool:
        _db_pool.close_all()
        _db_pool = None




# ----------------------------------------
# Database Connection Pool
# ----------------------------------------
class ConnectionPool:
    """Thread-safe SQLite connection pool."""
    
    def __init__(
        self, 
        db_path: Path, 
        pool_size: int = DB_POOL_SIZE,
        timeout: float = DB_TIMEOUT
    ):
        self.db_path = db_path
        self.pool_size = pool_size
        self.timeout = timeout
        self._pool: Queue = Queue(maxsize=pool_size)
        self._lock = threading.Lock()
        self._initialized = False
    
    def _initialize_pool_locked(self) -> None:
        """Initialize the connection pool. Must be called with _lock held."""
        if self._initialized:
            return

        try:
            for _ in range(self.pool_size):
                conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    timeout=self.timeout
                )
                try:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    # Not in the pool yet, so the cleanup below wouldn't close
                    # it; an open handle keeps a damaged file locked.
                    conn.close()
                    raise
                self._pool.put(conn)

            self._initialized = True
            logger.info(f"Initialized connection pool with {self.pool_size} connections")
        except Exception:
            # Drain and close any connections already enqueued
            while not self._pool.empty():
                try:
                    self._pool.get_nowait().close()
                except Exception:
                    pass
            raise

    @contextmanager
    def get_connection(self):
        """
        Get a connection from the pool.

        Yields:
            sqlite3.Connection: Database connection
        """
        with self._lock:
            if not self._initialized:
                self._initialize_pool_locked()

        try:
            conn = self._pool.get(timeout=self.timeout)
        except Empty:
            # Surface exhaustion as a typed DB error so the sqlite3.Error
            # handlers throughout the app catch it instead of a bare,
            # message-less queue.Empty slipping through.
            raise sqlite3.OperationalError(
                f"Database connection pool exhausted after {self.timeout}s"
            )
        try:
            yield conn
            conn.commit()
        except Exception:
            # Roll back on ANY error, not just sqlite3.Error: a non-DB
            # exception raised inside the caller's `with` block must not
            # let the connection return to the pool mid-transaction.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            self._pool.put(conn)
    
    def close_all(self) -> None:
        """Close all connections in the pool."""
        with self._lock:
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Exception:
                    pass
            self._initialized = False
            logger.info("Closed all database connections")


# Global connection pool instance
_db_pool: Optional[ConnectionPool] = None
_db_pool_lock = threading.Lock()
_db_pool_shutdown = False


def get_db_pool() -> ConnectionPool:
    """Get or create the global database connection pool."""
    global _db_pool
    if _db_pool_shutdown:
        # The app is tearing down. Refuse to lazily resurrect a fresh pool
        # (and a fresh set of WAL connections) for a late worker thread.
        raise sqlite3.OperationalError("Database pool has been shut down")
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = ConnectionPool(DB_PATH)
    return _db_pool


@contextmanager
def get_db_connection():
    """Context manager for database connections using the pool."""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        yield conn


# ----------------------------------------
# Database Management
# ----------------------------------------
class DatabaseManager:
    """Handle SQLite operations for ASS files and processed videos."""

    @staticmethod
    def init_db() -> None:
        """Initialize database tables.

        Clears the terminal shutdown flag first so init_db is a clean re-init
        point: after AppState.shutdown() poisons the pool (correct at process
        exit), an explicit re-init can still bring the DB layer back up rather
        than raising 'pool has been shut down'.
        """
        global _db_pool_shutdown
        _db_pool_shutdown = False
        try:
            DatabaseManager._init_schema()
        except sqlite3.DatabaseError as e:
            # A damaged index (power loss mid-checkpoint, disk error, AV
            # restore) must not stop the app from starting. It is bookkeeping
            # only: originals live in Original Subs/ and the backup rules don't
            # depend on it. Keep the damaged file for diagnosis and rebuild.
            DatabaseManager._move_corrupt_aside(e)
            DatabaseManager._init_schema()

    @staticmethod
    def _move_corrupt_aside(error: Exception) -> None:
        global _db_pool
        if _db_pool is not None:
            _db_pool.close_all()
            _db_pool = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(DB_PATH) + suffix)
            if src.exists():
                try:
                    os.replace(src, Path(f"{DB_PATH}{suffix}.corrupt-{stamp}"))
                except OSError as move_error:
                    logger.error(f"Could not move damaged {src.name} aside: {move_error}")
        record_error("database", DB_PATH.name,
                     f"the file index was damaged ({error}) and has been rebuilt; "
                     "your subtitles and backups are not affected")

    # PRAGMA user_version of the file index. 1 = adds the "restored" and
    # "foreign" statuses (additive: older builds read unknown statuses as
    # "indexed" and may restyle restored files after a downgrade).
    SCHEMA_VERSION = 1

    @staticmethod
    def _init_schema() -> None:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Check and create monitored_files table
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='monitored_files'"
            )
            exists = cursor.fetchone() is not None
            
            if exists:
                cursor.execute("PRAGMA table_info(monitored_files)")
                columns = [col[1] for col in cursor.fetchall()]
                if "last_modified" not in columns or "status" not in columns:
                    logger.info("Schema mismatch, dropping 'monitored_files' table")
                    cursor.execute("DROP TABLE monitored_files")
                    exists = False
                elif "content_hash" not in columns:
                    # Additive, nullable migration — preserves existing rows.
                    logger.info("Adding 'content_hash' column to 'monitored_files'")
                    cursor.execute(
                        "ALTER TABLE monitored_files ADD COLUMN content_hash TEXT"
                    )

            if not exists:
                cursor.execute("""
                    CREATE TABLE monitored_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT UNIQUE,
                        last_modified REAL,
                        status TEXT,
                        content_hash TEXT
                    )
                """)
                logger.info("Created 'monitored_files' table")

            # Check and create processed_videos table
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='processed_videos'"
            )
            if cursor.fetchone() is None:
                cursor.execute("""
                    CREATE TABLE processed_videos (
                        file_path TEXT PRIMARY KEY
                    )
                """)
                logger.info("Created 'processed_videos' table")

            cursor.execute("PRAGMA user_version")
            if (cursor.fetchone() or [0])[0] < DatabaseManager.SCHEMA_VERSION:
                cursor.execute(f"PRAGMA user_version = {DatabaseManager.SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def add_existing_files(
        folder_path: str, 
        batch_size: int = BATCH_SIZE
    ) -> int:
        """
        Batch add existing ASS files to database.
        
        Args:
            folder_path: Folder to scan
            batch_size: Number of files to insert per batch
            
        Returns:
            Number of files indexed
        """
        if not folder_path:
            return 0
        
        top = Path(folder_path)
        if not top.exists():
            logger.warning(f"Folder does not exist: {folder_path}")
            return 0
        
        files_batch: List[Tuple[str, float, str]] = []
        total_count = 0
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            for file_path in top.rglob("*.ass"):
                if "Original Subs" in file_path.parts:
                    continue
                # The app's own write temps (.tmp_XXXX.ass) are never subtitles.
                if file_path.name.startswith((".tmp_", ".bak_")):
                    continue

                # Validate path
                if not SecurityUtils.validate_path(file_path, top):
                    continue
                
                try:
                    last_modified = file_path.stat().st_mtime
                except OSError:
                    continue
                
                files_batch.append((
                    str(file_path),
                    last_modified,
                    FileStatus.INDEXED.value
                ))
                
                if len(files_batch) >= batch_size:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO monitored_files "
                        "(file_path, last_modified, status) VALUES (?, ?, ?)",
                        files_batch
                    )
                    conn.commit()
                    total_count += len(files_batch)
                    files_batch.clear()
            
            # Insert remaining
            if files_batch:
                cursor.executemany(
                    "INSERT OR IGNORE INTO monitored_files "
                    "(file_path, last_modified, status) VALUES (?, ?, ?)",
                    files_batch
                )
                conn.commit()
                total_count += len(files_batch)
        
        logger.info(f"Indexed {total_count} .ass files from {folder_path}")
        return total_count

    @staticmethod
    def add_file(file_path: Path) -> bool:
        """Add or update a single file in the database (status PROCESSED).

        Records a content hash of the (now on-disk, styled) file so the monitor
        can skip a full decode+parse when a later mtime change leaves the bytes
        unchanged.
        """
        if not file_path.exists():
            return False

        try:
            last_modified = file_path.stat().st_mtime
        except OSError as e:
            logger.error(f"Cannot stat file {file_path}: {e}")
            return False

        content_hash = file_hash(file_path)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO monitored_files "
                "(file_path, last_modified, status, content_hash) "
                "VALUES (?, ?, ?, ?)",
                (str(file_path), last_modified, FileStatus.PROCESSED.value, content_hash),
            )
        return True

    @staticmethod
    def set_status(file_path: Path, status: "FileStatus") -> bool:
        """Record `file_path` with `status` and its current mtime + content
        hash (restored / foreign files keep their own status)."""
        try:
            last_modified = file_path.stat().st_mtime
        except OSError as e:
            logger.error(f"Cannot stat file {file_path}: {e}")
            return False
        with get_db_connection() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO monitored_files "
                "(file_path, last_modified, status, content_hash) "
                "VALUES (?, ?, ?, ?)",
                (str(file_path), last_modified, status.value, file_hash(file_path)),
            )
        return True

    @staticmethod
    def update_file_timestamp(
        file_path: str,
        last_modified: float,
        content_hash: Optional[str] = None,
        status: Optional[str] = None,
    ) -> bool:
        """Record a file as processed (upsert).

        Uses INSERT OR REPLACE rather than a bare UPDATE: the already-styled
        branch of the monitor can be the first time a file is seen, in which
        case no row exists yet and an UPDATE would match zero rows — leaving the
        file un-recorded and re-parsed on every restart.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO monitored_files "
                "(file_path, last_modified, status, content_hash) "
                "VALUES (?, ?, ?, ?)",
                (str(file_path), last_modified,
                 status or FileStatus.PROCESSED.value, content_hash),
            )
        return True

    @staticmethod
    def was_processed(file_path: str) -> bool:
        """True if a row for this path is recorded as PROCESSED.

        Used by apply_changes to detect "a backup was already taken for this
        file" without trusting the (possibly externally-deleted) backup file.
        Robust to DB errors (returns False) so styling is never blocked by a DB
        hiccup — matching the "DB is bookkeeping, not the write" philosophy.
        """
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                # Any status the app sets after styling a file counts as history.
                cursor.execute(
                    "SELECT 1 FROM monitored_files "
                    "WHERE file_path = ? AND status IN (?, ?, ?) LIMIT 1",
                    (file_path, FileStatus.PROCESSED.value,
                     FileStatus.RESTORED.value, FileStatus.FOREIGN.value),
                )
                return cursor.fetchone() is not None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"was_processed check failed for {file_path}: {e}")
            return False

    @staticmethod
    def get_monitored_files() -> Dict[str, Dict[str, Any]]:
        """Get all monitored files from database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, last_modified, status, content_hash "
                "FROM monitored_files"
            )
            rows = cursor.fetchall()

        return {
            row[0]: {
                "last_modified": row[1],
                "status": row[2],
                "content_hash": row[3],
            }
            for row in rows
        }

    @staticmethod
    def clear_monitored_files() -> None:
        """Clear all monitored files from database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM monitored_files")
        logger.info("Cleared 'monitored_files' table")

    @staticmethod
    def add_processed_video(video_path: str) -> None:
        """Add a processed video to database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO processed_videos (file_path) VALUES (?)",
                (video_path,)
            )

    @staticmethod
    def remove_processed_video(video_path: str) -> None:
        """Remove a single processed video (used to retry a failed extraction)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM processed_videos WHERE file_path = ?",
                (video_path,)
            )

    @staticmethod
    def add_processed_videos_batch(video_paths: List[str]) -> None:
        """Batch add processed videos to database."""
        if not video_paths:
            return

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                "INSERT OR IGNORE INTO processed_videos (file_path) VALUES (?)",
                [(path,) for path in video_paths]
            )

    @staticmethod
    def get_processed_videos() -> Set[str]:
        """Get all processed videos from database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path FROM processed_videos")
            rows = cursor.fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def clear_processed_videos() -> None:
        """Clear all processed videos from database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_videos")
        logger.info("Cleared 'processed_videos' table")
