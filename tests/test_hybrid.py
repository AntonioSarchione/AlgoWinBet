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
