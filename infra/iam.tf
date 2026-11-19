# ============================================================================
# IAM 委托：授予 FunctionGraph 运行期访问 LTS / SMN / GeminiDB 的权限
#
# 不声明 duration：provider 默认已是 FOREVER；显式写 "FOREVER" 在部分区域
# 会报 IAM.0011 duration is invalid。省略即取默认值。
# 角色名须与 IAM 权限列表逐字一致，写错时委托创建不报错，运行期才 403。
# ============================================================================

resource "huaweicloud_identity_agency" "fgs" {
  name                  = local.agency_name
  description           = "Agency allowing FunctionGraph to read LTS logs and publish notifications"
  delegated_domain_name = var.agency_delegated_domain

  project_role {
    project = var.region
    roles = [
      "LTS FullAccess",
      # IAM 里没有 "SMN FullAccess"（只有 ReadOnlyAccess，不能发消息）；
      # 该角色策略恰为 smn:topic:publish
      "CTSAgencySmnPolicy",
      "GeminiDB FullAccess",
    ]
  }
}
