# 部署指导

本文给出从零到可验证部署的分步命令。每一步的取值、资源名与预期输出都取自本仓库的
`infra/*.tf` 与 `src/functions/` 实现；请在本仓库根目录执行标有「仓库根目录」的命令，
其余命令在执行前用 `cd infra` 进入基础设施目录。

## 部署前准备

### 1. 工具与凭据

| 项目 | 要求 |
|-|-|
| Terraform | `>= 1.5`（约束见 `infra/versions.tf`） |
| Provider | `huaweicloud/huaweicloud`，`~> 1.70` |
| Python | 3.9 及以上可跑单元测试与构建脚本；依赖自检需 3.10 及以上解释器才完整执行 |
| 访问密钥 | 在华为云控制台「服务与支持 → 我的凭证 → 我的访问密钥」获取访问密钥 ID 与秘密访问密钥，配置到执行 Terraform 的环境（如 `HW_ACCESS_KEY`、`HW_SECRET_KEY`、`HW_REGION_NAME`）；不要写入代码、`terraform.tfvars` 或提交到仓库 |
| 账号名 | 「我的凭证 → 账号信息」中的账号名，即 `agency_delegated_domain` 的取值（下文写作 `<HW_ACCOUNT_DOMAIN>`） |
| 权限 | 所用身份需能在目标区域创建 RDS、GeminiDB、LTS、FunctionGraph、OBS、SMN、VPC 资源，并能在统一身份认证里创建委托 |

### 2. 两条硬依赖必须先行确认

这两项是本实践的前置依赖，未确认不要执行 `terraform apply`。

**依赖一：需要一台真实的 Microsoft SQL Server 实例，版本与许可由使用者自备。**
本栈通过 `huaweicloud_rds_instance` 创建该实例，参数为：

- `sqlserver_version`（默认 `2022_web`）：区域实际可售的 SQL Server 版本；
- `rds_flavor`（默认 `sql.x2.large.4`）：实例规格；
- `rds_volume_type` / `rds_volume_size`（默认 `ESSD` / `40`）：存储类型与容量。

确认方式：在 RDS 控制台「购买关系型数据库 SQL Server」页面按所选区域看可选版本与规格，
或自行声明 `huaweicloud_rds_engine_versions`、`huaweicloud_rds_flavors`、
`huaweicloud_rds_storage_types` 数据源查询（这三个数据源本栈未声明，需你按需临时加到工作目录再查询）。
把控制台确认到的组合写进 `terraform.tfvars`。SQL Server 的版本选型与相应许可由使用者自备。

**依赖二：GeminiDB 实例的规格码与接口版本需按目标区域实查后填入，未填则不可 apply。**
本栈声明的是表格接口实例，取值为：

- `geminidb_mode`（默认 `Cluster`）、`geminidb_node_num`（默认 `3`，集群最少三节点）、
  `geminidb_node_size`（默认 `100`）、`geminidb_version`（默认 `3.0`）、
  `geminidb_spec_code`（默认 `geminidb.ddm.xlarge.2.arm`）；
- 实例侧固定 `datastore.type = "DynamoDB"`、`storage_engine = "rocksdb"`、`flavor.storage = "ULTRA"`。

`geminidb_spec_code` 与 `geminidb_version` 当前是**占位待确认**取值。请在 GeminiDB 控制台
「购买实例 → 表格接口」页面按所选区域与可用区确认「接口版本 + 节点规格」的真实可选组合，
替换后再 apply；直接沿用默认值会在创建实例这一步失败。

### 3. 需要提前准备的外部信息

| 占位符 | 用途 | 获取位置 |
|-|-|---|
| `<HW_ACCESS_KEY>` / `<HW_SECRET_KEY>` | 执行 Terraform | 「我的凭证 → 我的访问密钥」 |
| `<HW_ACCOUNT_DOMAIN>` | 委托信任的账号名（`agency_delegated_domain`） | 「我的凭证 → 账号信息」的账号名 |
| `<DB_PASSWORD>` | RDS 高权限账号 `db_admin` 的口令（`db_password`） | 使用者自备，需满足 RDS 的口令复杂度要求 |
| `<GEMINIDB_PASSWORD>` | GeminiDB 实例 `root` 账号口令（`geminidb_password`，函数侧 `GEMINIDB_PASSWORD` 取同一个值） | 使用者自备，需满足 GeminiDB 的口令复杂度要求 |
| `<WEBHOOK_URL>` | 通知接收端（`notify_webhook_url`，SMN 订阅端点与函数辅助通道共用） | 你自己的告警接收服务；必须是可解析的 HTTPS 地址，占位符会让 SMN 订阅创建失败 |

---

## 步骤 1：建基础设施

### 1.1 准备参数文件（仓库根目录）

```bash
cd infra
copy terraform.tfvars.example terraform.tfvars     # Windows
# cp terraform.tfvars.example terraform.tfvars     # Linux/macOS
```

打开 `terraform.tfvars`，把四个占位符替换为真实值，并按「部署前准备 → 2」修正两条硬依赖的规格取值。
需要改动的关键行：

```terraform
sqlserver_version = "2022_web"              # 按区域实查结果填写
rds_flavor        = "sql.x2.large.4"        # 按区域实查结果填写
geminidb_spec_code = "geminidb.ddm.xlarge.2.arm"  # 占位待确认，必须换成实查值
geminidb_version   = "3.0"                  # 占位待确认，必须换成实查值
db_password       = "<DB_PASSWORD>"          # 替换为强口令
geminidb_password  = "<GEMINIDB_PASSWORD>"   # 替换为强口令
notify_webhook_url = "<WEBHOOK_URL>"         # 替换为可解析的 HTTPS 地址
agency_delegated_domain = "<HW_ACCOUNT_DOMAIN>"
```

### 1.2 先构建函数代码包（仓库根目录，`apply` 的前置动作）

`infra/functions.tf` 里 `huaweicloud_obs_bucket_object.notifier_code` 的 `source` 指向
`var.notifier_package`，默认值 `../src/functions/build/sqlserver-error-notifier.zip`。
该文件不存在时 `terraform plan`/`apply` 会直接报读取包文件失败，因此**必须先构建再 apply**。

```bash
python src/functions/build_packages.py
```

预期输出：

```text
notifier -> src/functions/build/sqlserver-error-notifier.zip (29105 bytes)
  sha256: cc3d305a8f2da488423c238c55e61961ff1f04e8cb8c6a636d9d30fc49df3421
  - config.py
  - credentials.py
  - error_rules.py
  - errors.py
  - index.py
  - logger.py
  - lts_event.py
  - net.py
  - notifier.py
  - signing.py
  - state_store.py
```

`sha256` 随源码变更，字节数由包内文件决定；只要源码不变，重复构建得到的包字节一致。
包内条目不含 `tests/`、`build/` 与构建脚本自身。

只想确认清单而不写文件：

```bash
python src/functions/build_packages.py --list
```

### 1.3 静态校验（`infra/` 目录）

```bash
terraform init
terraform validate
```

`terraform init` 拉取 Provider 需要联网；`terraform validate` 预期输出：

```text
Success! The configuration is valid.
```

（若你在本机配置了 Provider 覆盖项用于离线校验，输出会附带一句 `dev_overrides` 提示，属预期，
结论仍是配置有效。）

### 1.4 执行部署（`infra/` 目录）

```bash
terraform plan
terraform apply
```

`terraform plan` 末尾形如 `Plan: 22 to add, 0 to change, 0 to destroy.`（本栈声明 22 个资源，
另有 1 个账号数据源）。`terraform apply` 输入 `auto-approving` 或确认 `yes` 后执行，
末尾形如：

```text
Apply complete! Resources: 21 added, 0 changed, 0 destroyed.
```

随后打印八个输出项（名称与 `infra/outputs.tf` 一致）：

| 输出 | 含义 |
|-|-|
| `rds_instance_id` | RDS for SQL Server 实例 ID |
| `rds_private_ips` | 实例内网地址（验证时连接用） |
| `lts_group_id` | 承接错误日志的日志组 ID |
| `lts_stream_id` | 承接错误日志的日志流 ID |
| `notifier_function_urn` | 错误处理函数 URN |
| `geminidb_endpoint` | 状态存储接入地址（内网） |
| `geminidb_port` | 状态存储端口 |
| `notify_topic_urn` | SMN 通知主题 URN（敏感输出） |

单项查看：

```bash
terraform output rds_private_ips
terraform output notifier_function_urn
```

预期分别打印形如 `10.20.1.<x>` 的地址与形如
`urn:fgs:cn-north-4:<账号ID>:function:<函数ID>` 的 URN。

> RDS 实例与 GeminiDB 实例创建耗时较长，apply 期间请保持会话可用。若中途失败，
> 修正参数后重跑 `terraform apply` 即可继续，已创建的资源不会被重复创建。

## 步骤 2：构建并上传函数代码包

代码包的上传统一由本栈完成，不需要单独执行上传命令：

1. `huaweicloud_obs_bucket.code` 创建私有桶，桶名为
   `<name_prefix>-code-<账号ID>`（默认前缀 `rds-sqlserver-monitor`，见 `infra/locals.tf`），
   开启服务端加密并设置 `force_destroy`；
2. `huaweicloud_obs_bucket_bpa.code` 阻断公开读与公开策略；
3. `huaweicloud_obs_bucket_object.notifier_code` 把本地包上传到对象键
   `functions/sqlserver-error-notifier.zip`；
4. `huaweicloud_obs_bucket_policy.code_agency` 只给委托授予读取对象的权限；
5. `huaweicloud_fgs_function.notifier` 以 `code_type = "obs"`、
   `code_url = https://<桶名>.obs.<region>.myhuaweicloud.com/functions/sqlserver-error-notifier.zip` 取包。

确认上传结果（`infra/` 目录）：

```bash
terraform state show huaweicloud_obs_bucket_object.notifier_code
```

预期输出里可看到 `bucket`、`key = "functions/sqlserver-error-notifier.zip"`、
`content_type = "application/zip"` 与 `source` 指向本地包路径。

代码包更新方式：改完 `src/functions/` 下的源码后重新执行
`python src/functions/build_packages.py`，再 `terraform apply`。函数资源的
`replace_triggered_by = [huaweicloud_obs_bucket_object.notifier_code]` 会在包内容变化时重建函数，
确保云端运行的就是本地刚构建的那一份。

手工上传方式（不想由 Terraform 上传时）：在 OBS 控制台把包上传到上述对象键，并把
`var.notifier_package` 指向一个已存在且内容一致的本地文件，以保持 `source` 校验通过。

## 步骤 3：建库、建账号与参数组

这三件事由同一份基础设施声明完成，apply 成功后即已就绪，无需手工执行 SQL 创建：

| 对象 | 资源 | 取值 |
|-|-|-|
| 高权限账号 | `huaweicloud_rds_sqlserver_account.admin` | 账号名 `var.db_user_name`（默认 `db_admin`），口令 `var.db_password` |
| 业务数据库 | `huaweicloud_rds_sqlserver_database.app` | 库名 `var.app_db_name`（默认 `monitordb`）；字符集由服务端按实例排序规则决定，不作为配置项 |
| 参数组 | `huaweicloud_rds_parametergroup.main` | 名称 `var.param_group_name`，`datastore` 为 `sqlserver` + `var.sqlserver_version`，`values = var.param_group_values`（默认 `{ "error_verbosity" = "0" }`），并通过 `param_group_id` 绑定到实例 |

确认与检查（`infra/` 目录）：

```bash
terraform state show huaweicloud_rds_sqlserver_account.admin
terraform state show huaweicloud_rds_sqlserver_database.app
terraform state show huaweicloud_rds_parametergroup.main
```

预期依次看到账号 `name = "db_admin"`、库 `name = "monitordb"`、参数组 `values` 含
`error_verbosity`。参数组的 `values` 已做运行期让权，控制台或内核回写不会在你下次 apply 时被覆盖。

从 VPC 内的客户端确认实例与库（本栈不开公网访问，地址取 `rds_private_ips`，端口取 `db_port`，默认 `1433`）：

```sql
SELECT @@VERSION;
SELECT DB_NAME() AS current_db, SUSER_SNAME() AS current_login;
```

预期返回 SQL Server 版本信息，以及当前库名 `monitordb` 与登录名 `db_admin`。

安全组只有一条入方向规则（VPC 网段到 `db_port` 的 TCP）。若你的客户端不在 `vpc_cidr` 覆盖的网段内，
请自行补充入方向规则，不要为了验证而放开公网。

## 步骤 4：打开错误日志投递

投递通道由 `huaweicloud_rds_lts_log.error_log` 声明，apply 后即已开启：

| 字段 | 取值 | 说明 |
|-|-|-|
| `instance_id` | `huaweicloud_rds_instance.sqlserver.id` | 被投递的实例 |
| `engine` | `sqlserver` | 引擎 |
| `log_type` | `error_log` | 投递的是实例内部错误日志 |
| `lts_group_id` | 本栈日志组 ID | 与 `lts_group_id` 输出一致 |
| `lts_stream_id` | 本栈日志流 ID | 与 `lts_stream_id` 输出一致 |

确认（`infra/` 目录）：

```bash
terraform state show huaweicloud_lts_group.errorlog
terraform state show huaweicloud_lts_stream.sqlserver_errorlog
terraform state show huaweicloud_rds_lts_log.error_log
terraform state show huaweicloud_fgs_trigger.lts
```

预期：日志组名为 `rds-sqlserver-monitor-errorlog`、错误日志流名为
`rds-sqlserver-monitor-sqlserver-errorlog`、函数运行日志流名为
`rds-sqlserver-monitor-notifier-runtime`（均由 `name_prefix` 派生），`ttl_in_days` 为
`lts_group_ttl_days` / `lts_stream_ttl_days` 的取值；投递资源状态可用；触发器 `type = "LTS"`、
`status = "ACTIVE"`，指向 `notifier_function_urn`，并且只订阅错误日志那一条流。

再到 LTS 控制台「日志检索」选择错误日志流，确认已能看到实例错误日志的原始行（实例启动阶段通常已产生
若干行）。函数的运行日志出现在另一条 `-notifier-runtime` 流里，不会成为触发源。数据库侧需要把消息写进错误日志，链路才有输入；本栈给实例绑定参数组的初始值为
`error_verbosity = 0`，请结合自己的错误类型确认数据库侧是否需要调整该参数。

## 步骤 5：验证通知链路

### 5.1 先做离线校验（不联网，部署前也可执行）

```bash
python -m pytest src/functions -q
```

预期输出末行：

```text
50 passed in 0.07s
```

这 50 条用例覆盖解析规则、噪音过滤、多行错误配对、去重与冷却语义、TTL 退化路径、
通知双通道与失败上报、坏事件明确失败、单条异常不中断整批、时间预算与批量截断。

### 5.2 制造一条可复现的错误日志

从 VPC 内客户端以 `db_admin` 连接 `rds_private_ips:1433`，执行：

```sql
RAISERROR (N'monitoring smoke test, error logging pipeline', 16, 1) WITH LOG;
GO
```

该语句把消息写入实例错误日志，在错误日志里形成
`Error: 50000, Severity: 16, State: 1` 形态的头行，描述紧随其后，
正好走「头行先落状态表、续行补齐描述后再判定」这条路径。
若当前账号权限不足以写入错误日志，请改用具备相应权限的账号，或等业务真实错误进入错误日志。

### 5.3 确认采集与函数被唤起

- LTS 日志流里检索到包含 `Error: `、`Severity: `、`State: ` 的原始行；
- FunctionGraph 控制台该函数的「调用记录」出现成功调用，函数运行日志是「一行一条 JSON」，
  可看到 `开始处理日志事件`、`事件解包完成`、`状态表就绪`、`日志事件处理完成` 等条目；
- 返回结果字段与 `src/functions/index.py` 一致：

```json
{
  "statusCode": 200,
  "records": 2,
  "counts": {
    "noise_skipped": 0,
    "notified": 1,
    "suppressed": 0,
    "notification_failed": 0,
    "pending_description": 1,
    "unmatched_text": 0,
    "state_write_failed": 0,
    "error_detected": 1
  },
  "state_store": { "table": "SqlServerErrorState", "enabled": true, "ttl_enabled": true },
  "warnings": [],
  "errors": []
}
```

`pending_description` 记的是头行，`notified` 记的是续行补齐描述后发出的那一条通知。

### 5.4 确认去重与冷却生效

在 `notification_cooldown_minutes`（默认 15 分钟）内重复执行同一条 `RAISERROR`，
再次查看函数返回结果：

```json
"counts": { "notified": 0, "suppressed": 1 }
```

`actions` 里对应条目形如（字段名取自代码）：

```json
{ "index": 1, "action": "suppressed", "error_number": 50000, "reason": "suppressed_within_cooldown", "cooldown_seconds": 900 }
```

冷却判定按「错误号 + 级别 + 状态」三者一致计算，只重复同一错误号但级别或状态不同不会抑制。
超过冷却窗口后再执行，`reason` 变为 `cooldown_expired`，重新告警一次。
状态表侧可确认 `last_notification` 已写入告警时间：函数首启建的表主键是 `error_number` + `timestamp`，
`expiry_time` 为告警时间加 `state_ttl_hours`（默认 10 小时）。

### 5.5 用函数测试入口单独跑一遍（可选）

在仓库根目录生成一份符合真实投递结构（外层 Base64 + 内层 `logs` 字符串）的事件 JSON，
粘贴到函数控制台的测试页运行。命令复用仓库内的测试支撑代码，每次都用当前时间生成时间戳
——这一点必须注意，过期记录不参与冷却判定：

```bash
python -c "import sys,json;sys.path[:0]=['src/functions','src/functions/tests'];import support;print(json.dumps(support.build_event([{'content':'Error: 50000, Severity: 16, State: 1.'}], log_group_id='lg-demo', log_stream_id='ls-demo'),ensure_ascii=False))"
```

预期输出形如：

```text
{"lts": {"data": "eyJsb2dzIjogIlt7XCJjb250ZW50XCI6IFwiRXJyb3I6IDUwMDAwLCBTZXZlcml0eTogMTYsIFN0YXRlOiAxLlwiLCBcInRpbWVcIj..."}}
```

这条事件只有头行、没有续行，预期返回里 `pending_description` 为 `1`、`notified` 为 `0`；
把 `content` 换成 `Msg 823, Level 24, State 2, Server SQLHOST, Line 1 数据库页面读取失败`
这类完整行形态，预期 `notified` 或 `suppressed` 出现计数。

### 5.6 确认通知到达

- 主通道：SMN 主题的 HTTPS 订阅把消息推给 `<WEBHOOK_URL>`；
  函数的发布调用需要运行上下文提供项目编号与临时凭据，任一缺失时函数日志里给出
  `跳过消息通知主题发布` 与原因（`项目编号未配置` 或 `运行凭据不可用`），
  此时依赖辅助通道；
- 辅助通道：函数直投的 JSON 含 `title`、`text`、`error_number`、`level`、`state`、`log_timestamp`、
  `message`、`log_group_id`、`log_stream_id`；
- 正文格式由 `src/functions/notifier.py` 决定，标题为 `SQL Server 错误告警：<错误号>`，
  逐行给出错误号、级别、状态、行号、日志时间、消息、原文、日志组、日志流、来源主机、解析规则。

返回结果里 `channels` 的取值形如 `"sent:smn"`、`"sent:webhook"`、
`"skipped:通知地址未配置"`、`"failed:<错误码>"`。只要有一个以 `sent` 开头即视为送达，
两条都失败时 `counts.notification_failed` 加一，且不更新冷却标记。

> 部署完成后请按本节 5.1–5.6 在真实环境逐项验证：错误日志进入日志流、函数被唤起并返回预期计数、
> 冷却窗口内重复错误被抑制、通知确实到达你的接收端。全部通过后方可视为交付完成。

## 步骤 6：释放资源

```bash
cd infra
terraform destroy
```

确认后执行，预期输出末行：

```text
Destroy complete! Resources: N destroyed.
```

要点：

- 本栈 22 个资源全部在销毁范围内：委托、OBS 桶与对象策略、SMN 主题与订阅、日志组与两条日志流、
  投递通道、GeminiDB 实例、RDS 实例与账号/库/参数组、安全组、子网与 VPC；
- 代码包桶设置了 `force_destroy = true`，销毁时会连带清空桶内对象；
- 若个别资源因外部依赖（例如你在控制台手工关联了其它资源）删除失败，请清理该依赖后重跑
  `terraform destroy`；
- 及时释放资源、避免残留；同时清理你在接收端为本次验证添加的临时配置；
- 本地构建产物 `src/functions/build/sqlserver-error-notifier.zip` 由构建脚本生成，可随时删除，
  重新构建即可复现。

## 常见问题与排查

| 卡点 | 定位方式 | 处理 |
|-|-|-|
| `plan` 报找不到代码包文件 | 检查 `src/functions/build/sqlserver-error-notifier.zip` 是否存在 | 先执行 `python src/functions/build_packages.py`，再 `plan` |
| 依赖自检提示非标准库模块 | 构建脚本用 `ast` 解析每个模块的顶层导入并打印问题清单 | 移除该第三方依赖，函数运行期只用标准库 |
| apply 在 SMN 订阅失败 | 该资源直接用 `notify_webhook_url` 作端点 | 填入真实可解析的 HTTPS 地址 |
| apply 在 GeminiDB 实例报规格不可用 | `geminidb_spec_code`、`geminidb_version` 仍是占位取值 | 按区域实查后替换 |
| apply 在 RDS 实例报版本或规格不可选 | `sqlserver_version`、`rds_flavor`、`rds_volume_type` 与可售组合不符 | 按区域实查后替换 |
| apply 在委托报参数非法 | `agency_delegated_domain` 是占位符 | 填入「我的凭证」里的账号名 |
| 函数调用失败并提示缺必需配置 | 函数日志里 `函数启动失败` 附带缺失键清单 | `STATE_COLLECTION`、`GEMINIDB_ENDPOINT`、`GEMINIDB_USER`、`GEMINIDB_PASSWORD` 必须有真实值；尖括号形式的占位符等同未配置 |
| 函数一直查不到状态存储 | 状态存储只走 VPC 内网地址 | 确认函数 `vpc_id`/`network_id` 与实例同网、口令正确；本栈未给状态存储开放公网 |
| 告警只走辅助通道 | 函数日志 `跳过消息通知主题发布` 的 `reason` 字段 | 需要主通道时确认委托角色与项目权限 |
| 收到大量重复告警 | 返回结果 `state_store.enabled` 为 `false` | 状态存储不可用时函数按放行处理，先修好连通性再判定 |
| 批次里没有错误却反复调用 | 返回结果 `unmatched_text` 计数升高 | 函数运行日志单独落 `<名称前缀>-notifier-runtime` 流，触发源只订阅错误日志流；若仍回环，核对两条流是否被运行期改动 |
