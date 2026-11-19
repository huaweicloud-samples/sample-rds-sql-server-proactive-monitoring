# ============================================================================
# 变量定义
# ============================================================================

# ---- 通用与区域 ----
variable "region" {
  description = "华为云区域"
  type        = string
  default     = "cn-north-4"
}

variable "name_prefix" {
  description = "资源命名前缀，统一作用于全部可命名资源"
  type        = string
  default     = "rds-sqlserver-monitor"
}

variable "az_primary" {
  description = "主可用区标识（形如 cn-north-4a）"
  type        = string
  default     = "cn-north-4a"
}

variable "az_standby" {
  description = "备可用区标识（用于高可用主备部署，形如 cn-north-4b）"
  type        = string
  default     = "cn-north-4b"
}

variable "enterprise_project_id" {
  description = "企业项目 ID，留空使用默认企业项目"
  type        = string
  default     = "0"
}

# ---- 网络 ----
variable "vpc_cidr" {
  description = "VPC 网段"
  type        = string
  default     = "10.20.0.0/16"
}

variable "subnet_cidr" {
  description = "子网网段"
  type        = string
  default     = "10.20.1.0/24"
}

variable "subnet_gateway_ip" {
  description = "子网网关地址"
  type        = string
  default     = "10.20.1.1"
}

# ---- RDS for SQL Server 实例（规格 / 版本 / 存储显式固定）----
variable "sqlserver_version" {
  description = "SQL Server 数据库版本（须与目标 region 商用可售版本一致，apply 前控制台/API 核实）"
  type        = string
  default     = "2022_web"
}

variable "rds_flavor" {
  description = "RDS for SQL Server 实例规格（显式固定；apply 前用 huaweicloud_rds_flavors 在目标 region 实查可用性）"
  type        = string
  default     = "sql.x2.large.4"
}

variable "rds_volume_type" {
  description = "RDS 存储类型（SQL Server 可选 ESSD / CLOUDSSD，apply 前用 huaweicloud_rds_storage_types 核实）"
  type        = string
  default     = "ESSD"
}

variable "rds_volume_size" {
  description = "RDS 存储容量（GB）"
  type        = number
  default     = 40
}

variable "rds_ha_mode" {
  description = "高可用模式：single（单机）或 ha（主备双可用区部署）"
  type        = string
  default     = "single"
  validation {
    condition     = contains(["single", "ha"], var.rds_ha_mode)
    error_message = "rds_ha_mode 只能为 single 或 ha。"
  }
}

variable "db_port" {
  description = "SQL Server 数据库端口"
  type        = number
  default     = 1433
}

variable "db_user_name" {
  description = "数据库高权限账号名"
  type        = string
  default     = "db_admin"
}

variable "db_password" {
  description = "数据库高权限账号密码（占位符 <DB_PASSWORD>，请勿写入真实值）"
  type        = string
  sensitive   = true
  default     = "<DB_PASSWORD>"
}

variable "app_db_name" {
  description = "业务数据库名（供错误日志解析后写入诊断库的实例名映射参考）"
  type        = string
  default     = "monitordb"
}

variable "backup_keep_days" {
  description = "RDS 自动备份保留天数"
  type        = number
  default     = 7
}

# ---- 参数组 ----
variable "param_group_name" {
  description = "RDS 参数组名称"
  type        = string
  default     = "rds-sqlserver-monitor-params"
}

variable "param_group_values" {
  description = "参数组自定义参数键值（运行期可能被控制台/内核回写，所有权让给运行期）"
  type        = map(string)
  default = {
    "error_verbosity" = "0"
  }
}

# ---- GeminiDB 表格服务（错误去重/冷却状态存储）----
variable "geminidb_mode" {
  description = "GeminiDB 实例模式"
  type        = string
  default     = "Cluster"
}

variable "geminidb_version" {
  description = "GeminiDB 接口版本"
  type        = string
  default     = "3.0"
}

variable "geminidb_spec_code" {
  description = "GeminiDB 节点规格（显式固定；apply 前在目标 region 实查可用性）"
  type        = string
  default     = "geminidb.ddm.xlarge.2.arm"
}

variable "geminidb_node_num" {
  description = "GeminiDB 节点数（集群最少 3 节点）"
  type        = number
  default     = 3
}

variable "geminidb_node_size" {
  description = "GeminiDB 单节点存储（GB）"
  type        = number
  default     = 100
}

variable "geminidb_password" {
  description = "GeminiDB 账号密码（占位符 <GEMINIDB_PASSWORD>，请勿写入真实值）"
  type        = string
  sensitive   = true
  default     = "<GEMINIDB_PASSWORD>"
}

variable "geminidb_error_table" {
  description = "错误去重状态表名（供函数侧建表/读写）"
  type        = string
  default     = "SqlServerErrorState"
}

# ---- 日志留存（LTS）----
variable "lts_group_ttl_days" {
  description = "LTS 日志组留存天数"
  type        = number
  default     = 30
}

variable "lts_stream_ttl_days" {
  description = "LTS 日志流留存天数"
  type        = number
  default     = 30
}

# ---- FunctionGraph 函数 ----
variable "fgs_runtime" {
  description = "FunctionGraph 运行时"
  type        = string
  default     = "Python3.9"
}

variable "fgs_handler" {
  description = "函数入口 handler，格式 <模块>.<函数名>"
  type        = string
  default     = "index.handler"
}

variable "fgs_memory_size" {
  description = "函数内存（MB）"
  type        = number
  default     = 256
}

variable "fgs_timeout" {
  description = "函数超时（秒）"
  type        = number
  default     = 300
}

# 函数代码包由 src/functions/build_packages.py 生成，
# 默认路径必须与该脚本的产物路径一致
variable "notifier_package" {
  description = "错误处理函数代码包（zip）的本地路径"
  type        = string
  default     = "../src/functions/build/sqlserver-error-notifier.zip"
}

# ---- 通知通道（SMN + 外部 webhook）----
variable "notify_topic_name" {
  description = "SMN 通知主题名称"
  type        = string
  default     = "sqlserver-error-notify"
}

variable "notify_webhook_url" {
  description = "通知接收端 webhook 地址（占位符 <WEBHOOK_URL>，请勿写入真实值）"
  type        = string
  sensitive   = true
  default     = "<WEBHOOK_URL>"
}

variable "notification_cooldown_minutes" {
  description = "同一错误的通知冷却窗口（分钟），注入函数环境变量"
  type        = number
  default     = 15
}

variable "state_ttl_hours" {
  description = "错误状态记录的存活时长（小时），注入函数环境变量"
  type        = number
  default     = 10
}

# ---- IAM 委托 ----
variable "agency_delegated_domain" {
  description = "委托所信任的账号域名（占位符 <HW_ACCOUNT_DOMAIN>）"
  type        = string
  default     = "<HW_ACCOUNT_DOMAIN>"
}
