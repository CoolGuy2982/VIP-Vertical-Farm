"""Tiny in-memory stand-in for the parts of google-cloud-firestore we use."""
import copy
import threading
import uuid


def _merge(base: dict, new: dict):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


class Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class DocRef:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.id = path.rsplit("/", 1)[-1]

    def set(self, data, merge=False, timeout=None):
        self.db.timeouts.append(timeout)
        with self.db.lock:
            if merge and self.path in self.db.docs:
                _merge(self.db.docs[self.path], data)
            else:
                self.db.docs[self.path] = copy.deepcopy(data)
            self.db.writes.append(self.path)
        self.db._notify(self.path.rsplit("/", 1)[0])

    def get(self):
        with self.db.lock:
            return Snapshot(self.id, copy.deepcopy(self.db.docs.get(self.path)))


class Query:
    def __init__(self, coll, filters=(), limit=None):
        self.coll, self.filters, self._limit = coll, list(filters), limit

    def where(self, field=None, op=None, value=None, filter=None):
        if filter is not None:
            field, op, value = filter.field_path, filter.op_string, filter.value
        assert op == "=="
        return Query(self.coll, self.filters + [(field, value)], self._limit)

    def limit(self, n):
        return Query(self.coll, self.filters, n)

    def get(self, timeout=None):
        out = []
        with self.coll.db.lock:
            for path, data in self.coll.db.docs.items():
                if path.rsplit("/", 1)[0] != self.coll.path:
                    continue
                if all(data.get(f) == v for f, v in self.filters):
                    out.append(Snapshot(path.rsplit("/", 1)[-1], copy.deepcopy(data)))
        return out[: self._limit] if self._limit else out

    def on_snapshot(self, callback):
        watch = Watch(self, callback)
        self.coll.db.watches.append(watch)
        watch.fire()
        return watch


class Watch:
    def __init__(self, query, callback):
        self.query, self.callback, self.active = query, callback, True

    def fire(self):
        if self.active:
            self.callback(self.query.get(), [], None)

    def unsubscribe(self):
        self.active = False


class CollRef(Query):
    def __init__(self, db, path):
        self.db, self.path = db, path
        super().__init__(self)

    def document(self, doc_id=None):
        return DocRef(self.db, f"{self.path}/{doc_id or uuid.uuid4().hex[:12]}")


class FakeFirestore:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.writes: list[str] = []
        self.timeouts: list = []
        self.watches: list[Watch] = []
        self.lock = threading.RLock()

    def document(self, path):
        return DocRef(self, path)

    def collection(self, path):
        return CollRef(self, path)

    def _notify(self, coll_path):
        for w in list(self.watches):
            if w.query.coll.path == coll_path:
                w.fire()
