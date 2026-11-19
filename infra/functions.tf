# ============================================================================
# FunctionGraph：错误日志解析 + 去重 + 通知的无服务器处理
# ============================================================================

# -----------------------------------------------------------------------------
# 代码包存放：私有桶 + 只读的委托访问策略
#
# 函数代码以 zip 形式存放在本栈自建的私有桶内，构建方式见
# src/functions/build_packages.py，包路径由 var.notifier_package 给出。
# -----------------------------------------------------------------------------

resource "huaweicloud_obs_bucket" "code" {
  bucket        = local.code_bucket
  storage_class = "STANDARD"
  acl           = "private"
  force_destroy = true
  encryption    = true

  tags = merge(local.common_tags, {
    Purpose = "function-code-package"
  })
}

# 匿名访问一律阻断：代码包桶不需要任何公开读
resource "huaweicloud_obs_bucket_bpa" "code" {
  bucket                  = huaweicloud_obs_bucket.code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "huaweicloud_obs_bucket_object" "notifier_code" {
  bucket       = huaweicloud_obs_bucket.code.id
  key          = local.notifier_zip_key
  source       = var.notifier_package
  content_type = "application/zip"
}

# 函数以委托身份运行，取代码包走对象存储数据面：
# 数据面按桶策略判定访问权，仅给委托授予角色不足以放行。
resource "huaweicloud_obs_bucket_policy" "code_agency" {
  bucket        = huaweicloud_obs_bucket.code.id
  policy_format = "obs"
  depends_on    = [huaweicloud_obs_bucket_bpa.code]

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid    = "AllowFunctionReadCode"
        Effect = "Allow"
        Principal = {
          ID = ["domain/${var.agency_delegated_domain}:agency/${local.agency_name}"]
        }
        Action = [
          "GetObject",
          "ListBucket",
        ]
        Resource = [
          huaweicloud_obs_bucket.code.bucket,
          "${huaweicloud_obs_bucket.code.bucket}/${local.notifier_zip_key}",
        ]
      }
    ]
  })
}

resource "huaweicloud_fgs_function" "notifier" {
  name        = local.fgs_name
  app         = "default"
  runtime     = var.fgs_runtime
  handler     = var.fgs_handler
  memory_size = var.fgs_memory_size
  timeout     = var.fgs_timeout
  agency      = huaweicloud_identity_agency.fgs.name
  code_type   = "obs"
  code_url    = local.notifier_code_url

  vpc_id     = huaweicloud_vpc.main.id
  network_id = huaweicloud_vpc_subnet.main.id

  # FunctionGraph 写入 LTS 需要四个参数齐全（组 id/名 + 流 id/名）。
  # 只给 id 或缺 name 时创建报 FSS.1212 Lts config error, group or stream not existed。
  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.errorlog.id
  log_group_name  = huaweicloud_lts_group.errorlog.group_name
  log_stream_id   = huaweicloud_lts_stream.notifier_runtime.id
  log_stream_name = huaweicloud_lts_stream.notifier_runtime.stream_name

  description = "Parse SQL Server error logs, de-duplicate via GeminiDB state, and publish notifications"

  # 运行期配置：通知地址、状态存储与通知主题在此声明，
  # webhook 与密码均为占位符，部署时由使用者替换为真实值
  user_data = jsonencode({
    NOTIFY_WEBHOOK_URL            = var.notify_webhook_url
    STATE_COLLECTION              = var.geminidb_error_table
    GEMINIDB_ENDPOINT             = huaweicloud_geminidb_instance.state.lb_ip_address
    GEMINIDB_PORT                 = tostring(huaweicloud_geminidb_instance.state.port)
    GEMINIDB_USER                 = "root"
    GEMINIDB_PASSWORD             = var.geminidb_password
    SMN_TOPIC_URN                 = huaweicloud_smn_topic.notify.topic_urn
    PROJECT_ID                    = data.huaweicloud_account.current.current_project_id
    REGION                        = var.region
    NOTIFICATION_COOLDOWN_MINUTES = tostring(var.notification_cooldown_minutes)
    TTL_HOURS                     = tostring(var.state_ttl_hours)
  })

  tags = local.common_tags

  lifecycle {
    # 代码包更新即重建函数，确保云端运行的是本地刚构建的那一份。
    # 函数名、规格与运行配置由本栈持有，不让出所有权。
    replace_triggered_by = [huaweicloud_obs_bucket_object.notifier_code]
  }
}

# 以 LTS 日志流作为触发源，实例错误日志写入后调用函数处理。
resource "huaweicloud_fgs_trigger" "lts" {
  function_urn = huaweicloud_fgs_function.notifier.urn
  type         = "LTS"
  status       = "ACTIVE"

  lts {
    log_group_id = huaweicloud_lts_group.errorlog.id
    log_topic_id = huaweicloud_lts_stream.sqlserver_errorlog.id
  }

  lifecycle {
    # 触发器启用状态可能由运维切换，让出所有权
    ignore_changes = [status]
  }
}
