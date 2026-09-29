import json
import sqlite3
import time


class Store:
    """Persist repository routes, code owners and delivery/message identities."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS repos (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                platform TEXT NOT NULL, self_id TEXT NOT NULL,
                kind TEXT NOT NULL, target TEXT NOT NULL, mode TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owners (
                repo TEXT NOT NULL REFERENCES repos(name) ON DELETE CASCADE,
                qq TEXT NOT NULL, PRIMARY KEY (repo, qq)
            );
            CREATE TABLE IF NOT EXISTS messages (
                repo TEXT NOT NULL REFERENCES repos(name) ON DELETE CASCADE,
                number INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                message_id TEXT NOT NULL,
                PRIMARY KEY (repo, number, kind, target)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                repo TEXT NOT NULL REFERENCES repos(name) ON DELETE CASCADE,
                delivery TEXT NOT NULL, destination TEXT NOT NULL,
                PRIMARY KEY (repo, delivery, destination)
            );
            CREATE TABLE IF NOT EXISTS panel_repos (
                name TEXT PRIMARY KEY REFERENCES repos(name) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS requests (
                repo TEXT PRIMARY KEY, received REAL NOT NULL, event TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL, delivery TEXT NOT NULL, number INTEGER NOT NULL,
                platform TEXT NOT NULL, self_id TEXT NOT NULL,
                kind TEXT NOT NULL, target TEXT NOT NULL,
                operation TEXT NOT NULL, content TEXT NOT NULL, test INTEGER NOT NULL,
                parent INTEGER, message_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
                duration REAL, error TEXT NOT NULL DEFAULT '',
                UNIQUE(repo, delivery, kind, target, operation)
            );
            CREATE INDEX IF NOT EXISTS tasks_pending ON tasks(status, next_at);
            CREATE INDEX IF NOT EXISTS tasks_order ON tasks(repo,number,kind,target,id);
        """)

    def enqueue(
        self,
        repo,
        delivery,
        number,
        kind,
        target,
        operation,
        content,
        test=False,
        parent=None,
        message_id=None,
    ):
        """Insert within the caller's transaction; duplicate deliveries keep their state."""
        if not test and self.delivered(repo["name"], delivery, f"{kind}:{target}"):
            return None
        now = time.time()
        self.db.execute(
            """INSERT OR IGNORE INTO tasks
            (repo,delivery,number,platform,self_id,kind,target,operation,content,test,
             parent,message_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                repo["name"],
                delivery,
                number,
                repo["platform"],
                repo["self_id"],
                kind,
                target,
                operation,
                json.dumps(content, ensure_ascii=False),
                int(test),
                parent,
                message_id,
                now,
                now,
            ),
        )
        return self.db.execute(
            "SELECT id FROM tasks WHERE repo=? AND delivery=? AND kind=? AND target=? AND operation=?",
            (repo["name"], delivery, kind, target, operation),
        ).fetchone()[0]

    def received(self, name, event):
        self.db.execute(
            "INSERT OR REPLACE INTO requests VALUES (?,?,?)", (name, time.time(), event)
        )

    def valid_task(self, task):
        repo = self.get_repo(task["repo"])
        if repo is None or any(repo[k] != task[k] for k in ("platform", "self_id")):
            return False
        if task["operation"] == "ping":
            return repo["kind"] == task["kind"] and repo["target"] == task["target"]
        owners = self.owners(task["repo"])
        if task["kind"] == "group":
            if (
                repo["mode"] not in ("group", "both")
                or repo["target"] != task["target"]
            ):
                return False
            if task["operation"] == "message":
                mentions = {
                    s["data"]["qq"]
                    for s in json.loads(task["content"])
                    if s["type"] == "at"
                }
                return bool(owners) and mentions.issubset(owners)
            return True
        return repo["mode"] in ("private", "both") and task["target"] in owners

    def cancel_stale(self):
        with self.db:
            for task in self.db.execute(
                "SELECT * FROM tasks WHERE status IN ('pending','running','failed','unknown')"
            ).fetchall():
                if not self.valid_task(task):
                    self.db.execute(
                        "UPDATE tasks SET status='cancelled',error='配置已变更，任务取消',updated=? WHERE id=?",
                        (time.time(), task["id"]),
                    )

    def recover(self):
        with self.db:
            self.db.execute(
                """UPDATE tasks SET status=CASE WHEN operation!='reaction' THEN 'unknown'
                WHEN attempts>=4 THEN 'failed' ELSE 'pending' END,
                error='发送中断，结果未确认',updated=? WHERE status='running'""",
                (time.time(),),
            )
        self.cancel_stale()
        self.prune()

    def prune(self):
        cutoff = time.time() - 30 * 86400
        with self.db:
            # Keep unresolved dependencies; clearing them would allow an out-of-order reaction.
            self.db.execute(
                """DELETE FROM tasks WHERE updated<? AND status IN ('success','cancelled')
                AND id NOT IN (SELECT parent FROM tasks WHERE parent IS NOT NULL)""",
                (cutoff,),
            )
            self.db.execute("DELETE FROM requests WHERE received<?", (cutoff,))
            self.db.execute(
                "UPDATE tasks SET error='',duration=NULL WHERE updated<?", (cutoff,)
            )

    def claim(self):
        task = self.db.execute(
            """SELECT t.* FROM tasks t WHERE t.status='pending' AND t.next_at<=?
            AND NOT EXISTS (SELECT 1 FROM tasks p WHERE p.repo=t.repo AND p.number=t.number
                AND p.kind=t.kind AND p.target=t.target AND p.id<t.id
                AND p.status IN ('pending','running','failed','unknown'))
            ORDER BY t.id LIMIT 1""",
            (time.time(),),
        ).fetchone()
        if task is not None:
            with self.db:
                self.db.execute(
                    "UPDATE tasks SET status='running',attempts=attempts+1,updated=? WHERE id=?",
                    (time.time(), task["id"]),
                )
            return self.task(task["id"])
        return None

    def task(self, task_id):
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def finish(self, task, status, duration, error="", next_at=0, message_id=None):
        with self.db:
            current = self.task(task["id"])
            if current["status"] == "cancelled":
                return
            if not self.valid_task(task):
                status, error = "cancelled", "配置已变更，任务取消"
            self.db.execute(
                """UPDATE tasks SET status=?,duration=?,error=?,next_at=?,
                message_id=COALESCE(?,message_id),updated=? WHERE id=?""",
                (status, duration, error, next_at, message_id, time.time(), task["id"]),
            )
            if status == "success" and not task["test"]:
                if task["operation"] == "message" and task["kind"] == "group":
                    self.db.execute(
                        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)",
                        (
                            task["repo"],
                            task["number"],
                            task["kind"],
                            task["target"],
                            message_id,
                        ),
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO deliveries VALUES (?,?,?)",
                    (
                        task["repo"],
                        task["delivery"],
                        f"{task['kind']}:{task['target']}",
                    ),
                )

    def retry(self, task_id):
        task = self.task(task_id)
        if (
            task is None
            or task["status"] not in ("failed", "unknown")
            or not self.valid_task(task)
        ):
            raise ValueError("任务不存在、无需重试或配置已变更")
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status='pending',attempts=0,next_at=0,error='',updated=? WHERE id=?",
                (time.time(), task_id),
            )

    def get_repo(self, name):
        return self.db.execute("SELECT * FROM repos WHERE name = ?", (name,)).fetchone()

    def sync_panel(self, entries):
        """Apply validated panel settings atomically, keeping command-owned repos.

        Args:
            entries: Normalized repository dictionaries from the plugin config.
        """
        with self.db:
            previous = {r[0] for r in self.db.execute("SELECT name FROM panel_repos")}
            current = {entry["name"] for entry in entries}
            for name in previous - current:
                self.db.execute("DELETE FROM repos WHERE name = ?", (name,))
            for entry in entries:
                name, route = entry["name"], entry["route"]
                existing = self.get_repo(name)
                if (
                    existing
                    and tuple(
                        existing[k] for k in ("platform", "self_id", "kind", "target")
                    )
                    != route
                ):
                    # Message IDs belong to the old bot and conversation.
                    self.db.execute("DELETE FROM repos WHERE name = ?", (name,))
                self.db.execute(
                    "INSERT INTO repos VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET mode=excluded.mode",
                    (name, *route, entry["mode"]),
                )
                self.db.execute("DELETE FROM owners WHERE repo = ?", (name,))
                self.db.executemany(
                    "INSERT INTO owners VALUES (?, ?)",
                    [(name, qq) for qq in entry["owners"]],
                )
                self.db.execute("INSERT OR IGNORE INTO panel_repos VALUES (?)", (name,))

    def panel_managed(self, name):
        return (
            self.db.execute(
                "SELECT 1 FROM panel_repos WHERE name = ?", (name,)
            ).fetchone()
            is not None
        )

    def repos(self):
        return self.db.execute("SELECT * FROM repos ORDER BY name").fetchall()

    def add_repo(self, name, route, mode):
        with self.db:
            self.db.execute(
                "INSERT INTO repos VALUES (?, ?, ?, ?, ?, ?)", (name, *route, mode)
            )

    def remove_repo(self, name):
        with self.db:
            self.db.execute("DELETE FROM repos WHERE name = ?", (name,))

    def set_mode(self, name, mode):
        with self.db:
            self.db.execute("UPDATE repos SET mode = ? WHERE name = ?", (mode, name))

    def owners(self, name):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT qq FROM owners WHERE repo = ? ORDER BY qq", (name,)
            )
        ]

    def change_owners(self, name, users, add):
        query = (
            "INSERT OR IGNORE INTO owners VALUES (?, ?)"
            if add
            else "DELETE FROM owners WHERE repo = ? AND qq = ?"
        )
        with self.db:
            self.db.executemany(query, [(name, qq) for qq in users])

    def messages(self, name, number):
        return self.db.execute(
            "SELECT * FROM messages WHERE repo = ? AND number = ?", (name, number)
        ).fetchall()

    def delivered(self, name, delivery, destination):
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE repo = ? AND delivery = ? AND destination = ?",
                (name, delivery, destination),
            ).fetchone()
            is not None
        )

    def remember(self, name, delivery, destination, message=None):
        with self.db:
            if message is not None:
                number, kind, target, message_id = message
                self.db.execute(
                    "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)",
                    (name, number, kind, target, message_id),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (name, delivery, destination),
            )

    def close(self):
        self.db.close()
