"""端到端处理链单测：假事件 + 假状态表 + 假通知通道。

覆盖：首次错误告警、冷却期内重复抑制、噪音忽略、多行错误配对、
通知通道缺省跳过、坏事件明确失败、单条异常不中断整批。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import error_rules
import index
import notifier as notifier_module
import state_store
from credentials import TempCredentials
from errors import ConfigurationError, EventFormatError
from logger import StructuredLogger
from support import ENV_BASE, FakeContext, FakeStateEngine, RecordingTransport, build_event, lts_env

TABLE = ENV_BASE["STATE_COLLECTION"]

MSG_LINE = "Msg 823, Level 24, State 2, Server SQLHOST, Line 1 数据库页面读取失败"
ERROR_HEADER = "2026-10-15 10:00:00.123 spid62      Error: 17881, Severity: 16, State: 1."
ERROR_DETAIL = "2026-10-15 10:00:00.124 spid62      长时间运行的事务已识别，请检查应用侧提交。"
NOISE_LINE = "2026-10-15 10:00:01.000 spid51      Attempting to cycle error log."


class _Harness(object):
    """把真实处理链装进假传输层。"""

    def __init__(self, env=None, engine=None, smn=None, support_ttl=True):
        self.env = lts_env(**(env or {}))
        self.cfg = config.RuntimeConfig(self.env)
        self.cfg.max_attempts = 1
        self.cfg.retry_backoff_seconds = 0
        self.logger = StructuredLogger(request_id="req-test")
        self.credentials = TempCredentials("FAKEAK", "FAKESK", "FAKETOKEN")
        self.engine = engine if engine is not None else FakeStateEngine(support_ttl=support_ttl)
        self._now = time.time()
        self.store = state_store.StateStore(self.cfg, self.logger,
                                            credentials=self.credentials,
                                            transport=self.engine.transport,
                                            clock=lambda: self._now)
        self.notifications = smn if smn is not None else RecordingTransport()
        self.notify = notifier_module.Notifier(self.cfg, self.logger,
                                               credentials=self.credentials,
                                               transport=self.notifications.transport)

    def run(self, records, now_ms=None):
        resolved_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self._now = resolved_ms / 1000.0
        event = build_event([{"content": text} for text in records], now_ms=resolved_ms)
        return index.process(event, self.cfg, self.logger, self.store, self.notify)

    def stored_items(self):
        return [state_store.unmarshal_item(item)
                for item in self.engine.tables.get(TABLE, {}).values()]


class FirstAlertTest(unittest.TestCase):
    def test_first_error_notifies_and_marks_state(self):
        harness = _Harness(env={"NOTIFY_WEBHOOK_URL": "https://example.com/hooks/alert"})
        result = harness.run([MSG_LINE])

        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertEqual(result["counts"]["error_detected"], 1)
        action = result["actions"][0]
        self.assertEqual(action["error_number"], 823)
        self.assertEqual(action["reason"], "first_occurrence")
        self.assertEqual(action["channels"]["smn"], "sent:smn")
        self.assertEqual(action["channels"]["webhook"], "sent:webhook")

        # 主通道请求带签名、发布路径与正文
        published = harness.notifications.requests[0]
        self.assertIn("/notifications/topics/", published["url"])
        self.assertIn("sqlserver-error-notify", published["url"])
        self.assertTrue(published["authorization"].startswith("SDK-HMAC-SHA256"))
        self.assertIn("Msg 823", published["body"]["message"])
        self.assertIn("错误号: 823", published["body"]["message"])

        stored = harness.stored_items()[0]
        self.assertEqual(stored["error_number"], 823)
        self.assertGreater(stored["last_notification"], 0)
        self.assertIs(stored["notification_sent"], True)


class CooldownTest(unittest.TestCase):
    def test_repeat_within_cooldown_is_suppressed(self):
        harness = _Harness()
        base_ms = int(time.time() * 1000)
        first = harness.run([MSG_LINE], now_ms=base_ms)
        self.assertEqual(first["counts"][index.ACTION_NOTIFIED], 1)

        second = harness.run([MSG_LINE], now_ms=base_ms + 60 * 1000)
        self.assertEqual(second["counts"][index.ACTION_SUPPRESSED], 1)
        self.assertEqual(second["counts"][index.ACTION_NOTIFIED], 0)
        self.assertEqual(second["actions"][0]["reason"], "suppressed_within_cooldown")
        # 两次出现各留一行记录，抑制那次不刷新告警标记
        self.assertEqual(len(harness.stored_items()), 2)
        self.assertEqual(len(harness.notifications.requests), 1)

    def test_repeat_after_cooldown_alerts_again(self):
        harness = _Harness()
        base_ms = int(time.time() * 1000)
        harness.run([MSG_LINE], now_ms=base_ms)
        later = harness.run([MSG_LINE], now_ms=base_ms + (harness.cfg.cooldown_seconds + 60) * 1000)
        self.assertEqual(later["counts"][index.ACTION_NOTIFIED], 1)


class NoiseTest(unittest.TestCase):
    def test_noise_lines_are_skipped_without_state_write(self):
        harness = _Harness()
        for text in (
            NOISE_LINE,
            "2026-10-15 10:00:02.000 spid51      DBCC execution completed. "
            "If DBCC printed error messages, contact your system administrator.",
            "2026-10-15 10:00:03.000 spid51      This is an informational message only; no user action is required.",
            "2026-10-15 10:00:04.000 spid51      The error log has been reinitialized.",
            "2026-10-15 10:00:05.000 spid51      DBCC STDOUT: output",
            "2026-10-15 10:00:06.000 spid51      DBCC has extra input",
        ):
            self.assertTrue(error_rules.is_noise(text), text)

        result = harness.run([NOISE_LINE])
        self.assertEqual(result["counts"][index.ACTION_NOISE], 1)
        self.assertEqual(result["counts"]["error_detected"], 0)
        self.assertEqual(harness.engine.tables.get(TABLE, {}), {})
        self.assertEqual(harness.notifications.requests, [])

    def test_dbcc_zero_error_message_is_noise(self):
        text = "DBCC CHECKDB (monitordb) found 0 errors and repaired 0 errors."
        self.assertTrue(error_rules.is_noise(text))


class ContinuationTest(unittest.TestCase):
    def test_header_then_detail_alerts_once(self):
        harness = _Harness()
        result = harness.run([ERROR_HEADER, ERROR_DETAIL])

        self.assertEqual(result["counts"][index.ACTION_PENDING_DESCRIPTION], 1)
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertEqual(len(harness.notifications.requests), 1)
        published = harness.notifications.requests[0]["body"]["message"]
        self.assertIn("17881", published)
        self.assertIn("长时间运行的事务已识别", published)

        stored = [item for item in harness.stored_items() if item["error_number"] == 17881]
        self.assertEqual(len(stored), 1)
        self.assertIs(stored[0]["is_complete"], True)
        self.assertEqual(stored[0]["message"], "长时间运行的事务已识别，请检查应用侧提交。")

    def test_detail_without_pending_is_reported_as_unmatched(self):
        harness = _Harness()
        result = harness.run(["2026-10-15 10:00:07.000 spid9 一段无法解析的普通文本"])
        self.assertEqual(result["counts"][index.ACTION_UNMATCHED_TEXT], 1)
        self.assertEqual(result["statusCode"], 200)


class ChannelFallbackTest(unittest.TestCase):
    def test_webhook_placeholder_is_skipped_safely(self):
        harness = _Harness()  # 默认即 <WEBHOOK_URL> 占位符
        result = harness.run([MSG_LINE])
        channels = result["actions"][0]["channels"]
        self.assertEqual(channels["webhook"], "skipped:通知地址未配置")
        self.assertEqual(channels["smn"], "sent:smn")
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertTrue(any("跳过 Webhook 通知" == record["message"] for record in harness.logger.records))

    def test_both_channels_unconfigured_does_not_mark_cooldown(self):
        harness = _Harness(env={"SMN_TOPIC_URN": "<SMN_TOPIC_URN>",
                                "NOTIFY_WEBHOOK_URL": "<WEBHOOK_URL>"})
        result = harness.run([MSG_LINE])
        self.assertEqual(result["counts"][index.ACTION_NOTIFICATION_FAILED], 1)
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 0)
        stored = harness.stored_items()[0]
        self.assertEqual(stored["last_notification"], 0)
        self.assertIs(stored["notification_sent"], False)

    def test_channel_failure_is_reported_not_swallowed(self):
        recorder = RecordingTransport(status=503, fail_with="SMN0001", fail_times=5)
        harness = _Harness(smn=recorder, env={"NOTIFY_WEBHOOK_URL": ""})
        result = harness.run([MSG_LINE])
        self.assertEqual(result["counts"][index.ACTION_NOTIFICATION_FAILED], 1)
        self.assertTrue(result["actions"][0]["channels"]["smn"].startswith("failed:"))
        self.assertEqual(harness.stored_items()[0]["last_notification"], 0)

    def test_publish_without_credentials_skips_topic(self):
        harness = _Harness(env={"PROJECT_ID": ""})
        result = harness.run([MSG_LINE])
        channels = result["actions"][0]["channels"]
        self.assertTrue(channels["smn"].startswith("skipped:"))
        self.assertEqual(result["counts"][index.ACTION_NOTIFICATION_FAILED], 1)


class StateStoreFailureTest(unittest.TestCase):
    def test_state_store_unavailable_still_alerts(self):
        harness = _Harness(engine=FakeStateEngine(unavailable=True))
        result = harness.run([MSG_LINE])
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertFalse(result["state_store"]["enabled"])
        self.assertEqual(result["actions"][0]["reason"], "state_store_unavailable")
        self.assertTrue(any(error["stage"] == "record_occurrence" for error in result["errors"]))

    def test_ttl_not_supported_uses_expiry_filter(self):
        harness = _Harness(support_ttl=False)
        result = harness.run([MSG_LINE])
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertFalse(result["state_store"]["ttl_enabled"])


class BadEventTest(unittest.TestCase):
    def test_unrecognisable_event_raises(self):
        harness = _Harness()
        with self.assertRaises(EventFormatError):
            index.process({"nope": 1}, harness.cfg, harness.logger, harness.store,
                          harness.notify)

    def test_broken_entry_inside_event_does_not_crash_batch(self):
        import base64
        import json

        inner = {
            "logs": json.dumps([{"content": MSG_LINE, "time": 1700000000000},
                                {"time": 1700000001000}, "纯文本行", {"unexpected": None}]),
            "log_group_id": "lg-1",
            "log_topic_id": "ls-1",
        }
        event = {"lts": {"data": base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")}}
        harness = _Harness()
        result = index.process(event, harness.cfg, harness.logger, harness.store, harness.notify)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["skipped_records"], 2)
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)

    def test_empty_batch_returns_cleanly(self):
        harness = _Harness()
        result = harness.run([])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["records"], 0)


class BudgetTest(unittest.TestCase):
    def test_deadline_stops_processing_early(self):
        harness = _Harness()
        event = build_event([{"content": MSG_LINE}] * 5)

        class _Spent(object):
            def exhausted(self):
                return True

            def remaining_seconds(self):
                return 0

        result = index.process(event, harness.cfg, harness.logger, harness.store,
                               harness.notify, budget=_Spent())
        self.assertIn("deadline_exhausted", result["warnings"])
        self.assertEqual(result["unprocessed_records"], 5)
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 0)

    def test_max_records_per_invoke_truncates(self):
        harness = _Harness()
        harness.cfg.max_records_per_invoke = 2
        result = harness.run([MSG_LINE] * 5)
        self.assertIn("truncated_to_max_records_per_invoke", result["warnings"])
        self.assertEqual(result["records"], 5)


class HandlerEntryTest(unittest.TestCase):
    """函数入口：从环境变量取配置，从上下文取临时凭据。"""

    def _patch_transports(self, engine, recorder, holder):
        import notifier as notifier_module

        original_store = state_store._default_transport
        original_notifier = notifier_module._default_transport

        def cleanup():
            state_store._default_transport = original_store
            notifier_module._default_transport = original_notifier

        state_store._default_transport = engine.transport
        notifier_module._default_transport = recorder.transport
        holder.append(cleanup)
        return recorder

    def test_handler_end_to_end(self):
        engine = FakeStateEngine()
        recorder = RecordingTransport()
        holder = []
        self._patch_transports(engine, recorder, holder)
        env = dict(ENV_BASE)
        env["NOTIFY_WEBHOOK_URL"] = ""
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            result = index.handler(build_event([{"content": MSG_LINE}]), FakeContext())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for cleanup in holder:
                cleanup()
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["counts"][index.ACTION_NOTIFIED], 1)
        self.assertEqual(recorder.requests[0]["headers"]["Content-Type"], "application/json")

    def test_handler_raises_when_required_config_missing(self):
        env = dict(ENV_BASE)
        env["GEMINIDB_PASSWORD"] = "<GEMINIDB_PASSWORD>"
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            with self.assertRaises(ConfigurationError):
                index.handler(build_event([{"content": MSG_LINE}]), FakeContext())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
