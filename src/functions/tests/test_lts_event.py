"""日志投递事件解包单测。

事件形态取自日志服务官方触发示例：外层 ``{"lts": {"data": "<Base64>"}}``，
内层 ``logs`` 是一段被再次编码成字符串的数组。
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lts_event
from errors import EventFormatError
from support import build_event

# 官方示例事件（Base64 段内是 logs 字符串数组）
OFFICIAL_EXAMPLE = {
    "lts": {
        "data": ("eyJsb2dzIjoiW3tcIm1lc3NhZ2VcIjpcIjIwMTgtMDgtMDgvMDg6MDg6MDggW1dSTl0gW3Rlc3Qu"
                 "Z286MDhdVGhpcyBpcyBhIHRlc3QgbWVzc2FnZS5cIixcInRpbWVcIjoxNTMwMDA5NjUzMDU5LFwi"
                 "aG9zdF9uYW1lXCI6XCJlY3MtdGVzdFwiLFwiaXBcIjpcIjE5Mi4xNjguMS4xXCIsXCJwYXRoXCI6"
                 "XCJ2YXIvbG9nL3Rlc3QubG9nXCIsXCJsb2dfdWlkXCI6XCI2NjNkNjkzMC03OTJkLTExZTgtOGIw"
                 "OC0yODZlZDQ4OGNlNzBcIixcImxpbmVfbm9cIjoxfV0iLCJvd25lciI6IjYyODBlMTcwYmQ5MzRm"
                 "NjBhNGQ4NTFjZjVjYTA1MTI5IiwibG9nX2dyb3VwX2lkIjoiOTdhOWQyODQtNDQ0OC0xMWU4LThm"
                 "YTQtMjg2ZWQ0ODhjZTcwIiwibG9nX3RvcGljX2lkIjoiMWE5Njc1YTctNzg0ZC0xMWU4LTlmNzAt"
                 "Mjg2ZWQ0ODhjZTcwIn0=")
    }
}


class UnpackTest(unittest.TestCase):
    def test_official_example_event(self):
        event = lts_event.unpack(OFFICIAL_EXAMPLE)
        self.assertEqual(event.log_group_id, "97a9d284-4448-11e8-8fa4-286ed488ce70")
        self.assertEqual(event.log_stream_id, "1a9675a7-784d-11e8-9f70-286ed488ce70")
        self.assertEqual(len(event.records), 1)
        record = event.records[0]
        self.assertEqual(record.content,
                         "2018-08-08/08:08:08 [WRN] [test.go:08]This is a test message.")
        self.assertEqual(record.timestamp_ms, 1530009653059)
        self.assertEqual(record.host, "ecs-test")
        self.assertEqual(record.ip, "192.168.1.1")
        self.assertEqual(record.path, "var/log/test.log")
        self.assertEqual(record.log_uid, "663d6930-792d-11e8-8b08-286ed488ce70")
        self.assertEqual(record.line_no, 1)
        self.assertEqual(event.skipped, 0)

    def test_event_built_by_helper(self):
        event = lts_event.unpack(
            build_event([{"content": "Msg 823, Level 24, State 2, Server H, Line 1 磁盘失败"}],
                        now_ms=1700000000000)
        )
        self.assertEqual(event.log_group_id, "lg-fake")
        self.assertEqual(event.log_stream_id, "ls-fake")
        self.assertEqual(event.records[0].timestamp_ms, 1700000000000)

    def test_logs_as_native_list_and_message_key(self):
        inner = {
            "logs": [{"message": "line one", "time": 1700000000000},
                     {"content": "line two", "btime": 1700000001000}],
            "log_group_id": "lg-2",
            "log_topic_id": "ls-2",
        }
        event = lts_event.unpack({
            "lts": {"data": base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")}
        })
        self.assertEqual([r.content for r in event.records], ["line one", "line two"])

    def test_seconds_timestamp_promoted_to_millis(self):
        inner = {"logs": json.dumps([{"content": "x", "time": 1700000000}])}
        event = lts_event.unpack({
            "lts": {"data": base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")}
        })
        self.assertEqual(event.records[0].timestamp_ms, 1700000000000)

    def test_event_passed_as_json_string(self):
        event = lts_event.unpack(json.dumps(build_event([{"content": "纯文本行"}], now_ms=1)))
        self.assertEqual(event.records[0].content, "纯文本行")

    def test_data_at_top_level(self):
        inner = {"logs": json.dumps([{"content": "y", "time": 1700000000000}])}
        payload = base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")
        event = lts_event.unpack({"data": payload})
        self.assertEqual(event.records[0].content, "y")

    def test_entry_without_content_is_counted_as_skipped(self):
        inner = {"logs": json.dumps([{"time": 1700000000000}, {"content": "ok", "time": 1}])}
        payload = base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")
        event = lts_event.unpack({"lts": {"data": payload}})
        self.assertEqual(event.skipped, 1)
        self.assertEqual(len(event.records), 1)

    def test_plain_string_logs_entries(self):
        inner = {"logs": json.dumps(["第一行原文", "第二行原文"])}
        payload = base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")
        event = lts_event.unpack({"lts": {"data": payload}}, now_ms=1700000009000)
        self.assertEqual([r.content for r in event.records], ["第一行原文", "第二行原文"])
        self.assertEqual(event.records[0].timestamp_ms, 1700000009000)


class BadEventTest(unittest.TestCase):
    def test_non_json_event(self):
        with self.assertRaises(EventFormatError):
            lts_event.unpack("这不是 JSON")

    def test_event_without_log_segment(self):
        with self.assertRaises(EventFormatError) as caught:
            lts_event.unpack({"foo": "bar"})
        self.assertIn("不含日志投递段", str(caught.exception))

    def test_top_level_list(self):
        with self.assertRaises(EventFormatError):
            lts_event.unpack([1, 2, 3])

    def test_logs_not_a_list(self):
        inner = {"logs": {"not": "list"}}
        payload = base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")
        with self.assertRaises(EventFormatError):
            lts_event.unpack({"lts": {"data": payload}})


if __name__ == "__main__":
    unittest.main()
