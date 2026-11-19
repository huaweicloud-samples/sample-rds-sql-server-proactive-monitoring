"""日志分析告警函数入口。

链路：数据库错误日志 → 日志投递事件 → 逐行解析判定 → 状态表去重与冷却 → 通知。

处理策略：

* 事件结构不可识别、运行配置缺项 ⇒ 抛异常让本次调用明确失败（平台侧可观测、
  可按函数失败投递目标告警），绝不静默返回成功；
* 单条日志的存储或通知调用失败 ⇒ 记录失败原因并继续处理同批其它日志，
  失败明细随返回结果上报；
* 剩余执行时间不足 ⇒ 提前收尾，返回结果里标记未处理条目数。
"""

import json
import time

import config as runtime_config
import error_rules
import lts_event
import notifier as notifier_module
import state_store as state_store_module
from credentials import from_context
from errors import MonitoringError, StateStoreError
from logger import build as build_logger

ACTION_NOISE = "noise_skipped"
ACTION_NOTIFIED = "notified"
ACTION_SUPPRESSED = "suppressed"
ACTION_NOTIFICATION_FAILED = "notification_failed"
ACTION_PENDING_DESCRIPTION = "pending_description"
ACTION_UNMATCHED_TEXT = "unmatched_text"
ACTION_STATE_FAILED = "state_write_failed"

MAX_REPORTED_ACTIONS = 50


class _Budget(object):
    """本次调用的时间预算，来自函数上下文的剩余执行时间。"""

    def __init__(self, context, min_remaining_seconds):
        remaining = None
        try:
            remaining = _read_remaining_millis(context)
        except Exception:  # 运行时不暴露剩余时间时按固定预算处理
            remaining = None
        self._deadline = time.monotonic() + (remaining / 1000.0 if remaining else 300.0)
        self._min_remaining = min_remaining_seconds

    def remaining_seconds(self):
        return max(0.0, self._deadline - time.monotonic())

    def exhausted(self):
        return self.remaining_seconds() <= self._min_remaining


def _read_remaining_millis(context):
    from credentials import read_context_attribute

    value = read_context_attribute(context, "getRemainingTimeInMilliSeconds")
    if value is None:
        value = read_context_attribute(context, "getRemainingTimeInMillis")
    return int(value) if value else None


def handler(event, context):
    """FunctionGraph 事件函数入口。

    :param event: 日志投递事件（见 lts_event 模块的字段说明）
    :param context: 函数运行上下文
    """
    logger = build_logger(context)
    started = time.monotonic()

    try:
        credentials = from_context(context)
        cfg = runtime_config.load(context=context, logger=logger)
        logger.info(
            "开始处理日志事件",
            region=cfg.region,
            table=cfg.state_collection,
            state_endpoint=cfg.state_endpoint(),
            has_runtime_credentials=bool(credentials and credentials.available()),
            config=cfg.summary(),
        )
    except MonitoringError as exc:
        logger.error("函数启动失败", error=exc.as_dict())
        raise

    store = state_store_module.StateStore(cfg, logger, credentials=credentials)
    notify = notifier_module.Notifier(cfg, logger, credentials=credentials)
    budget = _Budget(context, cfg.min_remaining_seconds)

    try:
        result = process(event, cfg, logger, store, notify, budget=budget)
    except MonitoringError as exc:
        logger.error("日志事件处理失败", error=exc.as_dict(), duration_seconds=_elapsed(started))
        raise
    except Exception:  # 未预期异常必须带堆栈落日志后再抛出
        logger.error("日志事件处理出现未预期异常", duration_seconds=_elapsed(started))
        raise

    result["duration_seconds"] = _elapsed(started)
    logger.info("日志事件处理完成", **_result_summary(result))
    return result


def process(event, cfg, logger, store, notify, budget=None, now_ms=None):
    """解包事件并逐条判定，返回结构化处理结果。"""
    parsed_event = lts_event.unpack(event, now_ms=now_ms)
    logger.info(
        "事件解包完成",
        log_group_id=parsed_event.log_group_id,
        log_stream_id=parsed_event.log_stream_id,
        records=len(parsed_event.records),
        skipped_records=parsed_event.skipped,
    )

    store.ensure_ready()

    result = {
        "statusCode": 200,
        "log_group_id": parsed_event.log_group_id,
        "log_stream_id": parsed_event.log_stream_id,
        "records": len(parsed_event.records),
        "skipped_records": parsed_event.skipped,
        "actions": [],
        "warnings": [],
        "errors": [],
        "counts": {
            ACTION_NOISE: 0,
            ACTION_NOTIFIED: 0,
            ACTION_SUPPRESSED: 0,
            ACTION_NOTIFICATION_FAILED: 0,
            ACTION_PENDING_DESCRIPTION: 0,
            ACTION_UNMATCHED_TEXT: 0,
            ACTION_STATE_FAILED: 0,
            "error_detected": 0,
        },
        "state_store": {
            "table": cfg.state_collection,
            "enabled": store.enabled,
            "ttl_enabled": store.ttl_enabled,
        },
    }

    records = parsed_event.records[: cfg.max_records_per_invoke]
    if len(parsed_event.records) > len(records):
        result["warnings"].append("truncated_to_max_records_per_invoke")

    for index, record in enumerate(records):
        if budget is not None and budget.exhausted():
            result["warnings"].append("deadline_exhausted")
            result["unprocessed_records"] = len(records) - index
            logger.warn("剩余执行时间不足，提前结束", unprocessed=len(records) - index)
            break
        try:
            _handle_record(record, index, parsed_event, cfg, logger, store, notify, result)
        except StateStoreError as exc:
            result["errors"].append({"index": index, "stage": "state_store", **exc.as_dict()})
            logger.error("状态存储处理失败", index=index, code=exc.code)
        except MonitoringError as exc:
            result["errors"].append({"index": index, "stage": "record", **exc.as_dict()})
            logger.error("日志条目处理失败", index=index, error=exc.as_dict())

    result["counts"] = dict(result["counts"])
    result["actions"] = result["actions"][:MAX_REPORTED_ACTIONS]
    return result


def _handle_record(record, index, parsed_event, cfg, logger, store, notify, result):
    text = record.content

    if error_rules.is_noise(text):
        result["counts"][ACTION_NOISE] += 1
        result["actions"].append({
            "index": index,
            "action": ACTION_NOISE,
            "reason": error_rules.noise_rule(text),
        })
        logger.info("忽略噪音日志", index=index, rule=error_rules.noise_rule(text))
        return

    parsed = error_rules.parse_error(text)
    if parsed is not None:
        result["counts"]["error_detected"] += 1
        _process_error_line(parsed, record, index, parsed_event, cfg, logger, store, notify, result)
        return

    _process_continuation(text, record, index, logger, store, notify, result, cfg=cfg)


def _process_error_line(parsed, record, index, parsed_event, cfg, logger, store, notify, result):
    """命中解析规则的行：先做告警判定，再落状态记录。"""
    item = store.build_item(
        parsed,
        record,
        parsed_event.log_group_id,
        parsed_event.log_stream_id,
    )

    allowed, reason, latest = store.decide(parsed.error_number, parsed.level, parsed.state)
    if latest:
        # 同主键覆盖时保留历史告警时间，冷却窗口不会因重写而重置
        item["last_notification"] = latest

    try:
        store.record_occurrence(item)
    except StateStoreError as exc:
        result["counts"][ACTION_STATE_FAILED] += 1
        result["errors"].append({"index": index, "stage": "record_occurrence", **exc.as_dict()})
        logger.error("错误记录写入失败", index=index, error_number=parsed.error_number, code=exc.code)

    if not parsed.is_complete:
        result["counts"][ACTION_PENDING_DESCRIPTION] += 1
        result["actions"].append({
            "index": index,
            "action": ACTION_PENDING_DESCRIPTION,
            "error_number": parsed.error_number,
            "level": parsed.level,
            "state": parsed.state,
        })
        logger.info("错误头行已记录，等待后续行补齐描述",
                     index=index, error_number=parsed.error_number)
        return

    _notify_if_allowed(item, allowed, reason, index, cfg, logger, store, notify, result)


def _process_continuation(text, record, index, logger, store, notify, result, cfg=None):
    """未命中解析规则的行：尝试作为上一条未完成错误的描述补齐。"""
    try:
        pending = store.latest_incomplete()
    except StateStoreError as exc:
        result["errors"].append({"index": index, "stage": "latest_incomplete", **exc.as_dict()})
        logger.error("查找未完成错误失败", index=index, code=exc.code)
        return

    if not pending:
        result["counts"][ACTION_UNMATCHED_TEXT] += 1
        result["actions"].append({"index": index, "action": ACTION_UNMATCHED_TEXT})
        logger.info("未解析出错误结构且无待补齐的错误", index=index)
        return

    description = error_rules.clean_description(text)
    try:
        merged = store.attach_description(pending, description)
    except StateStoreError as exc:
        result["counts"][ACTION_STATE_FAILED] += 1
        result["errors"].append({"index": index, "stage": "attach_description", **exc.as_dict()})
        logger.error("补齐错误描述失败", index=index, code=exc.code)
        return

    allowed, reason, _latest = store.decide(
        merged.get("error_number"), merged.get("level"), merged.get("state")
    )
    _notify_if_allowed(merged, allowed, reason, index, cfg, logger, store, notify, result)


def _notify_if_allowed(item, allowed, reason, index, cfg, logger, store, notify, result):
    """放行则发送通知，成功后把告警时间写回状态表。"""
    if not allowed:
        result["counts"][ACTION_SUPPRESSED] += 1
        result["actions"].append({
            "index": index,
            "action": ACTION_SUPPRESSED,
            "error_number": item.get("error_number"),
            "reason": reason,
            "cooldown_seconds": cfg.cooldown_seconds,
        })
        logger.info("冷却窗口内重复错误，抑制通知",
                     error_number=item.get("error_number"), reason=reason)
        return

    send_result = notify.send(dict(item))
    entry = {
        "index": index,
        "action": ACTION_NOTIFIED if send_result.get("delivered") else ACTION_NOTIFICATION_FAILED,
        "error_number": item.get("error_number"),
        "level": item.get("level"),
        "state": item.get("state"),
        "reason": reason,
        "channels": send_result.get("channels"),
    }
    result["counts"][entry["action"]] += 1
    result["actions"].append(entry)

    if not send_result.get("delivered"):
        # 通知未送达不更新冷却标记，下一条同类日志仍会重试
        logger.warn("通知未送达，未更新冷却标记", error_number=item.get("error_number"))
        return

    try:
        store.mark_notified(item.get("error_number"), item.get("timestamp"))
    except StateStoreError as exc:
        result["errors"].append({"index": index, "stage": "mark_notified", **exc.as_dict()})
        logger.error("告警时间写回失败", index=index, code=exc.code)


def _elapsed(started):
    return round(time.monotonic() - started, 3)


def _result_summary(result):
    counts = result.get("counts") or {}
    return {
        "records": result.get("records"),
        "noise_skipped": counts.get(ACTION_NOISE),
        "notified": counts.get(ACTION_NOTIFIED),
        "suppressed": counts.get(ACTION_SUPPRESSED),
        "pending_description": counts.get(ACTION_PENDING_DESCRIPTION),
        "unmatched_text": counts.get(ACTION_UNMATCHED_TEXT),
        "notification_failed": counts.get(ACTION_NOTIFICATION_FAILED),
        "state_write_failed": counts.get(ACTION_STATE_FAILED),
        "errors": len(result.get("errors") or []),
        "warnings": result.get("warnings"),
    }


def dumps(result):
    """调试用：把返回结果序列化成字符串。"""
    return json.dumps(result, ensure_ascii=False, default=str)
