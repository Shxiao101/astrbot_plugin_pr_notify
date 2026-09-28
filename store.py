import sqlite3


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
        """)

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
