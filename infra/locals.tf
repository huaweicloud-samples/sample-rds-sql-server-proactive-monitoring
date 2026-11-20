# ============================================================================
# 局部值：统一命名与标签
# ============================================================================

data "huaweicloud_account" "current" {}

locals {
  name_prefix = var.name_prefix

  rds_name                = "${local.name_prefix}-sqlserver"
  param_group_name        = var.param_group_name
  lts_group_name          = "${local.name_prefix}-errorlog"
  lts_stream_name         = "${local.name_prefix}-sqlserver-errorlog"
  lts_runtime_stream_name = "${local.name_prefix}-notifier-runtime"
  fgs_name                = "${local.name_prefix}-error-notifier"
  agency_name             = "${local.name_prefix}-fgs-agency"
  secgroup_name           = "${local.name_prefix}-sg"

  # 函数代码包：由 src/functions/build_packages.py 构建后存入私有桶
  # 桶名要求全局唯一，追加账号 ID 作为后缀
  code_bucket           = "${local.name_prefix}-code-${data.huaweicloud_account.current.id}"
  obs_internal_endpoint = "obs.${var.region}.myhuaweicloud.com"
  notifier_zip_key      = "functions/sqlserver-error-notifier.zip"
  notifier_code_url     = "https://${local.code_bucket}.${local.obs_internal_endpoint}/${local.notifier_zip_key}"

  # 高可用：主备双可用区；单机：单可用区
  rds_availability_zones = var.rds_ha_mode == "ha" ? [var.az_primary, var.az_standby] : [var.az_primary]

  common_tags = {
    ManagedBy   = "terraform"
    Environment = "sample"
    Purpose     = "sqlserver-error-monitoring"
  }
}
