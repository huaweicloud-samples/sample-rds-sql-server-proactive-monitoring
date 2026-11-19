# ============================================================================
# 网络：VPC / 子网 / 安全组
# ============================================================================

resource "huaweicloud_vpc" "main" {
  name                  = "${local.name_prefix}-vpc"
  cidr                  = var.vpc_cidr
  enterprise_project_id = var.enterprise_project_id
  tags                  = local.common_tags
}

resource "huaweicloud_vpc_subnet" "main" {
  name       = "${local.name_prefix}-subnet"
  vpc_id     = huaweicloud_vpc.main.id
  cidr       = var.subnet_cidr
  gateway_ip = var.subnet_gateway_ip
  tags       = local.common_tags
}

resource "huaweicloud_networking_secgroup" "main" {
  name                  = local.secgroup_name
  description           = "Access control for SQL Server monitoring stack"
  enterprise_project_id = var.enterprise_project_id
}

# 仅放行 VPC 内对数据库端口的访问，避免公网暴露
resource "huaweicloud_networking_secgroup_rule" "allow_db_intra_vpc" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = var.db_port
  port_range_max    = var.db_port
  remote_ip_prefix  = var.vpc_cidr
  security_group_id = huaweicloud_networking_secgroup.main.id
}
