# ============================================================================
# LTS 日志通道：承接 SQL Server 错误日志，作为解析/去重/通知的触发源
# ============================================================================

resource "huaweicloud_lts_group" "errorlog" {
  group_name  = local.lts_group_name
  ttl_in_days = var.lts_group_ttl_days
  tags        = local.common_tags

  lifecycle {
    # 留存策略运行期可能被调整，让出所有权
    ignore_changes = [ttl_in_days]
  }
}

resource "huaweicloud_lts_stream" "sqlserver_errorlog" {
  group_id    = huaweicloud_lts_group.errorlog.id
  stream_name = local.lts_stream_name
  ttl_in_days = var.lts_stream_ttl_days

  lifecycle {
    ignore_changes = [ttl_in_days]
  }
}

# 函数自身运行日志单独落一条流：LTS 触发源只订阅 SQL Server 错误日志流，
# 两条流分开可避免函数写下的运行日志再次触发自己。
resource "huaweicloud_lts_stream" "notifier_runtime" {
  group_id    = huaweicloud_lts_group.errorlog.id
  stream_name = local.lts_runtime_stream_name
  ttl_in_days = var.lts_stream_ttl_days

  lifecycle {
    ignore_changes = [ttl_in_days]
  }
}

# 将 RDS for SQL Server 的错误日志投递到 LTS 日志流。
# engine = sqlserver，log_type = error_log 对应实例内部错误日志导出。
resource "huaweicloud_rds_lts_log" "error_log" {
  instance_id   = huaweicloud_rds_instance.sqlserver.id
  engine        = "sqlserver"
  log_type      = "error_log"
  lts_group_id  = huaweicloud_lts_group.errorlog.id
  lts_stream_id = huaweicloud_lts_stream.sqlserver_errorlog.id
}
