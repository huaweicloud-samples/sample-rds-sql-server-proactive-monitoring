# ============================================================================
# 输出
# ============================================================================

output "rds_instance_id" {
  description = "RDS for SQL Server 实例 ID"
  value       = huaweicloud_rds_instance.sqlserver.id
}

output "rds_private_ips" {
  description = "RDS for SQL Server 内网地址"
  value       = huaweicloud_rds_instance.sqlserver.private_ips
}

output "lts_group_id" {
  description = "承接错误日志的 LTS 日志组 ID"
  value       = huaweicloud_lts_group.errorlog.id
}

output "lts_stream_id" {
  description = "承接错误日志的 LTS 日志流 ID"
  value       = huaweicloud_lts_stream.sqlserver_errorlog.id
}

output "notifier_function_urn" {
  description = "错误处理函数 URN"
  value       = huaweicloud_fgs_function.notifier.urn
}

output "geminidb_endpoint" {
  description = "GeminiDB 状态存储接入地址"
  value       = huaweicloud_geminidb_instance.state.lb_ip_address
}

output "geminidb_port" {
  description = "GeminiDB 状态存储端口"
  value       = huaweicloud_geminidb_instance.state.port
}

output "notify_topic_urn" {
  description = "SMN 通知主题 URN"
  value       = huaweicloud_smn_topic.notify.topic_urn
  sensitive   = true
}
