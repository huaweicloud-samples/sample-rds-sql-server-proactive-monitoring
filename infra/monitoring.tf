# ============================================================================
# 状态存储 + 通知通道
# ============================================================================

# ---- GeminiDB 表格服务：错误去重 / 冷却状态存储 ----
resource "huaweicloud_geminidb_instance" "state" {
  name                  = "${local.name_prefix}-state"
  mode                  = var.geminidb_mode
  password              = var.geminidb_password
  availability_zone     = var.az_primary
  vpc_id                = huaweicloud_vpc.main.id
  subnet_id             = huaweicloud_vpc_subnet.main.id
  security_group_id     = huaweicloud_networking_secgroup.main.id
  enterprise_project_id = var.enterprise_project_id
  tags                  = local.common_tags

  datastore {
    type           = "DynamoDB"
    storage_engine = "rocksdb"
    version        = var.geminidb_version
  }

  flavor {
    num       = var.geminidb_node_num
    size      = var.geminidb_node_size
    spec_code = var.geminidb_spec_code
    storage   = "ULTRA"
  }

  backup_strategy {
    start_time = "04:00-05:00"
    keep_days  = var.backup_keep_days
  }

  lifecycle {
    # 节点规格与备份策略运行期可能扩缩容/调整，让出所有权
    ignore_changes = [flavor, backup_strategy]
  }
}

# ---- SMN 主题：通知汇聚点 ----
resource "huaweicloud_smn_topic" "notify" {
  name         = var.notify_topic_name
  display_name = "SQL Server error notifications"
}

# ---- SMN 订阅：将主题消息推送到接收端 webhook（占位符，真实地址部署时注入）----
resource "huaweicloud_smn_subscription" "webhook" {
  topic_urn = huaweicloud_smn_topic.notify.topic_urn
  protocol  = "https"
  endpoint  = var.notify_webhook_url
  remark    = "External alerting endpoint"
}
