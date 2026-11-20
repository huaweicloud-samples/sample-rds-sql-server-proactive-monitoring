"""状态存储层单测：建表、TTL、去重与冷却判定。

运行：在 ``src/functions`` 目录下执行
``python -m unittest discover -s tests -v``
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import error_rules
import signing
import state_store
from credentials import TempCredentials
from logger import StructuredLogger
from support import ENV_BASE, FakeStateEngine

TABLE = ENV_BASE["STATE_COLLECTION"]


def _cfg(**overrides):
    env = dict(ENV_BASE)
    env.update(overrides)
    cfg = config.RuntimeConfig(env)
    cfg.max_attempts = 1
    cfg.retry_backoff_seconds = 0
    return cfg


def _store(engine=None, **overrides):
    engine = engine if engine is not None else FakeStateEngine()
    cfg = _cfg(**overrides)
    credentials = TempCredentials("FAKEAK", "FAKESK", "FAKETOKEN")
    store = state_store.StateStore(cfg, StructuredLogger(), credentials=credentials,
                                   transport=engine.transport)
    return store, engine


def _parsed(text=None):
    text = text or "Msg 823, Level 24, State 2, Server SQLHOST, Line 1 磁盘读取失败"
    return error_rules.parse_error(text)


def _record(timestamp_ms=None):
    class _Record(object):
        pass

    record = _Record()
    record.timestamp_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    record.host = "sqlserver-0"
    record.ip = "10.20.1.50"
    record.path = "MSSQL/ErrorLog"
    record.line_no = 1
    return record


class MarshallingTest(unittest.TestCase):
    def test_roundtrip_keeps_types(self):
        item = {"a": 1, "b": "text", "c": True, "d": None, "e": 1.5, "f": [1, "x"]}
        marshalled = state_store.marshal_item(item)
        self.assertEqual(marshalled["a"], {"N": "1"})
        self.assertEqual(marshalled["c"], {"BOOL": True})
        self.assertEqual(state_store.unmarshal_item(marshalled), item)
        self.assertEqual(state_store.unmarshal(state_store.marshal("plain")), "plain")


class TableBootstrapTest(unittest.TestCase):
    def test_creates_table_and_enables_ttl(self):
        store, engine = _store()
        self.assertTrue(store.ensure_ready())
        self.assertIn(TABLE, engine.tables)
        self.assertEqual(engine.ttl_settings[TABLE],
                         {"AttributeName": "expiry_time", "Enabled": True})
        self.assertTrue(store.ttl_enabled)

    def test_existing_table_is_reused(self):
        engine = FakeStateEngine()
        engine.tables[TABLE] = {}
        store, engine = _store(engine)
        self.assertTrue(store.ensure_ready())
        operations = [call["operation"] for call in engine.calls]
        self.assertNotIn("CreateTable", operations)

    def test_ttl_unsupported_falls_back_to_expiry_filter(self):
        store, engine = _store(FakeStateEngine(support_ttl=False))
        self.assertTrue(store.ensure_ready())
        self.assertFalse(store.ttl_enabled)

    def test_unavailable_store_disables_state_and_reopens_alerts(self):
        store, engine = _store(FakeStateEngine(unavailable=True))
        self.assertFalse(store.ensure_ready())
        self.assertFalse(store.enabled)
        allowed, reason, _latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "state_store_unavailable")

    def test_requests_are_signed(self):
        store, engine = _store()
        store.ensure_ready()
        first = engine.calls[0]
        self.assertTrue(first["authorization"].startswith(signing.PROTOCOL_ALGORITHM))
        self.assertIn(TABLE, first["body"]["TableName"])
        self.assertFalse(first["verify"])  # VPC 内私有地址，证书由实例侧签发


class DecisionTest(unittest.TestCase):
    def _seed(self, store, engine, **fields):
        now = int(time.time())
        item = {
            "error_number": {"N": "823"},
            "timestamp": {"N": str(now * 1000 - 1000)},
            "level": {"N": str(fields.get("level", 24))},
            "state": {"N": str(fields.get("state", 2))},
            "is_complete": {"BOOL": True},
            "last_notification": {"N": str(fields.get("last_notification", 0))},
            "expiry_time": {"N": str(fields.get("expiry_time", now + 3600))},
        }
        engine.seed(TABLE, [item])
        store._ready = True
        store.enabled = True
        return item, now

    def test_first_occurrence_is_allowed(self):
        store, engine = _store()
        store.ensure_ready()
        allowed, reason, latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "first_occurrence")
        self.assertEqual(latest, 0)

    def test_repeat_within_cooldown_is_suppressed(self):
        store, engine = _store()
        now = int(time.time())
        _item, _seeded_at = self._seed(store, engine, last_notification=now - 60)
        allowed, reason, latest = store.decide(823, 24, 2)
        self.assertFalse(allowed)
        self.assertEqual(reason, "suppressed_within_cooldown")
        self.assertEqual(latest, now - 60)

    def test_repeat_after_cooldown_is_allowed(self):
        store, engine = _store()
        now = int(time.time())
        _item, _seeded_at = self._seed(store, engine, last_notification=now - 3600)
        allowed, reason, _latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "cooldown_expired")

    def test_expired_record_is_not_counted(self):
        """存活期已过的记录不再抑制告警（TTL 未生效时的兜底判定）。"""
        store, engine = _store()
        now = int(time.time())
        self._seed(store, engine, last_notification=now - 60, expiry_time=now - 1)
        allowed, reason, _latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "first_occurrence")

    def test_level_and_state_are_part_of_the_identity(self):
        store, engine = _store()
        now = int(time.time())
        self._seed(store, engine, last_notification=now - 60, level=16)
        allowed, reason, _latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "first_occurrence")

    def test_query_failure_reopens_alerts(self):
        engine = FakeStateEngine()
        engine.tables[TABLE] = {}
        store, _engine = _store(engine)
        store._ready = True
        engine.unavailable = True
        allowed, reason, _latest = store.decide(823, 24, 2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "state_store_query_failed")


class ReadWriteTest(unittest.TestCase):
    def test_record_then_query_roundtrip(self):
        store, engine = _store()
        store.ensure_ready()
        parsed = _parsed()
        item = store.build_item(parsed, _record(), "lg-1", "ls-1")
        store.record_occurrence(item)
        found = store.query_occurrences(parsed.error_number, parsed.level, parsed.state)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["error_number"], 823)
        self.assertEqual(found[0]["message"], "磁盘读取失败")
        self.assertIs(found[0]["is_complete"], True)
        stored = state_store.unmarshal_item(list(engine.tables[TABLE].values())[0])
        self.assertEqual(stored["expiry_time"], item["expiry_time"])

    def test_item_expiry_follows_ttl_hours(self):
        store, _engine = _store(TTL_HOURS="10")
        store.ensure_ready()
        timestamp_ms = int(time.time() * 1000)
        item = store.build_item(_parsed(), _record(timestamp_ms), "lg-1", "ls-1")
        self.assertEqual(item["expiry_time"], item["occurred_at"] + 10 * 3600)
        self.assertEqual(item["timestamp"], timestamp_ms)

    def test_latest_incomplete_picks_most_recent(self):
        store, engine = _store()
        store.ensure_ready()
        now_ms = int(time.time() * 1000)
        engine.seed(TABLE, [
            {"error_number": {"N": "17881"}, "timestamp": {"N": str(now_ms - 5000)},
             "level": {"N": "16"}, "state": {"N": "1"}, "is_complete": {"BOOL": False},
             "expiry_time": {"N": str(int(time.time()) + 3600)}},
            {"error_number": {"N": "17882"}, "timestamp": {"N": str(now_ms - 1000)},
             "level": {"N": "16"}, "state": {"N": "1"}, "is_complete": {"BOOL": False},
             "expiry_time": {"N": str(int(time.time()) + 3600)}},
        ])
        pending = store.latest_incomplete()
        self.assertEqual(pending["error_number"], 17882)

    def test_latest_incomplete_paginates(self):
        store, engine = _store(FakeStateEngine(page_size=1))
        store.ensure_ready()
        now_ms = int(time.time() * 1000)
        engine.seed(TABLE, [
            {"error_number": {"N": "1788%d" % index}, "timestamp": {"N": str(now_ms - index * 1000)},
             "level": {"N": "16"}, "state": {"N": "1"}, "is_complete": {"BOOL": False},
             "expiry_time": {"N": str(int(time.time()) + 3600)}}
            for index in range(4)
        ])
        pending = store.latest_incomplete()
        self.assertEqual(pending["error_number"], 17880)
        scans = [call for call in engine.calls if call["operation"] == "Scan"]
        self.assertGreater(len(scans), 1)

    def test_attach_description_marks_complete(self):
        store, engine = _store()
        store.ensure_ready()
        now_ms = int(time.time() * 1000)
        pending = {"error_number": 17881, "timestamp": now_ms, "level": 16, "state": 1}
        engine.seed(TABLE, [{
            "error_number": {"N": "17881"},
            "timestamp": {"N": str(now_ms)},
            "level": {"N": "16"},
            "state": {"N": "1"},
            "message": {"S": ""},
            "is_complete": {"BOOL": False},
        }])
        merged = store.attach_description(pending, "长时间运行的事务已识别")
        self.assertEqual(merged["message"], "长时间运行的事务已识别")
        self.assertIs(merged["is_complete"], True)

    def test_mark_notified_updates_marker_and_extends_expiry(self):
        store, engine = _store()
        store.ensure_ready()
        now_ms = int(time.time() * 1000)
        item = store.build_item(_parsed(), _record(now_ms), "lg-1", "ls-1")
        store.record_occurrence(item)
        notified_at = store.mark_notified(item["error_number"], item["timestamp"])
        stored = state_store.unmarshal_item(list(engine.tables[TABLE].values())[0])
        self.assertEqual(stored["last_notification"], notified_at)
        self.assertIs(stored["notification_sent"], True)
        self.assertEqual(stored["expiry_time"], notified_at + store._cfg.ttl_seconds)


if __name__ == "__main__":
    unittest.main()
