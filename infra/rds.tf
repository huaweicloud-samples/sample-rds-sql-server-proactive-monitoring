# ============================================================================
# RDS for SQL Server：参数组 + 实例 + 账号/库
# ============================================================================

# ---- 参数组 ----
resource "huaweicloud_rds_parametergroup" "main" {
  name        = local.param_group_name
  description = "Parameter group for the SQL Server monitoring instance"

  datastore {
    type    = "sqlserver"
    version = var.sqlserver_version
  }

  values = var.param_group_values

  lifecycle {
    # 参数当前值运行期可能被控制台/内核回写，所有权让给运行期
    ignore_changes = [values]
  }
}

# ---- 数据库实例（规格 / 版本 / 存储显式固定）----
resource "huaweicloud_rds_instance" "sqlserver" {
  name                  = local.rds_name
  flavor                = var.rds_flavor
  vpc_id                = huaweicloud_vpc.main.id
  subnet_id             = huaweicloud_vpc_subnet.main.id
  security_group_id     = huaweicloud_networking_secgroup.main.id
  availability_zone     = local.rds_availability_zones
  enterprise_project_id = var.enterprise_project_id
  param_group_id        = huaweicloud_rds_parametergroup.main.id
  ha_replication_mode   = var.rds_ha_mode == "ha" ? "async" : null
  tags                  = local.common_tags

  db {
    type    = "sqlserver"
    version = var.sqlserver_version
    port    = var.db_port
  }

  volume {
    type = var.rds_volume_type
    size = var.rds_volume_size
  }

  backup_strategy {
    start_time = "03:00-04:00"
    keep_days  = var.backup_keep_days
  }

  lifecycle {
    # 规格、存储容量、备份策略运行期可能被热改/自动扩容/控制台调整，让出所有权
    ignore_changes = [
      flavor,
      volume,
      backup_strategy,
      parameters,
    ]
  }
}

# ---- 高权限账号（SQL Server 专用资源）----
resource "huaweicloud_rds_sqlserver_account" "admin" {
  name        = var.db_user_name
  password    = var.db_password
  instance_id = huaweicloud_rds_instance.sqlserver.id
}

# ---- 业务数据库 ----
# character_set 由服务端根据实例排序规则自动决定，不作为可配置项写入。
resource "huaweicloud_rds_sqlserver_database" "app" {
  name        = var.app_db_name
  instance_id = huaweicloud_rds_instance.sqlserver.id
}
