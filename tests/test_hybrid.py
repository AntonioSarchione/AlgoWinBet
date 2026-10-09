import pytest

from algowinbet.snapshots import HybridConnection

DROPPED = ValueError("Hrana: `http error: `connection closed before message completed``")


class Remote:
    def __init__(self, fail):
        self.fail, self.sent, self.closed = fail, [], False

    def execute(self, sql, *args):
        if self.fail:
            self.fail -= 1
            raise DROPPED
        self.sent.append(sql)

    def commit(self):
        pass

    def close(self):
        self.closed = True


def hybrid(*fails):
    made = []

    def open_remote():
        made.append(Remote(fails[len(made)] if len(made) < len(fails) else 0))
        return made[-1]
    return HybridConnection(object(), open_remote), made


def test_first_write_of_a_transaction_retried_on_a_new_connection():
    h, made = hybrid(1)
    h.execute("INSERT INTO t VALUES(1)")
    assert len(made) == 2 and made[0].closed and made[1].sent == ["INSERT INTO t VALUES(1)"]


def test_later_write_not_retried():
    h, made = hybrid(0)
    h.execute("INSERT INTO t VALUES(1)")
    made[0].fail = 1
    with pytest.raises(ValueError):
        h.execute("INSERT INTO t VALUES(2)")
    h, made = hybrid(0)
    h.execute("INSERT INTO t VALUES(1)")
    h.commit()
    made[0].fail = 1
    h.execute("INSERT INTO t VALUES(2)")  # a new transaction: retried
    assert made[1].sent == ["INSERT INTO t VALUES(2)"]


def test_commit_after_writes_makes_the_next_read_sync():
    synced = []

    class Replica:
        def sync(self):
            synced.append(1)

        def execute(self, sql, *args):
            return sql
    h = HybridConnection(Replica(), lambda: Remote(0))
    h.execute("INSERT INTO t VALUES(1)")
    h.execute("SELECT 1")  # syncs (nothing committed yet)
    h.commit()
    h.execute("SELECT 1")  # the commit made the write visible: synced again
    assert len(synced) == 2


def test_one_commit_groups_the_bulk_writes():
    from algowinbet.snapshots import SnapshotStore
    s = SnapshotStore(":memory:")
    commits = []

    class Spy:
        def __init__(self, db):
            self.db = db

        def execute(self, *a):
            return self.db.execute(*a)

        def commit(self):
            commits.append(1)
            self.db.commit()
    s.db = Spy(s.db)
    with s.one_commit():
        s._bulk("INSERT OR REPLACE INTO jobs(name, done_at, detail)", [("a", "x", "")])
        s._bulk("INSERT OR REPLACE INTO jobs(name, done_at, detail)", [("b", "x", "")])
    assert len(commits) == 1
    s._bulk("INSERT OR REPLACE INTO jobs(name, done_at, detail)", [("c", "x", "")])
    assert len(commits) == 2
