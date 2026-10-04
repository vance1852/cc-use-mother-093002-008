# 编制全球数贸统计版本协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

当前包含两个服务：

- `digital_trade_foundation`：合作机构、操作者、业务节点与领域资料的登记，幂等回执与审计链；
- `digital_trade_statistics`：统计资料接收、校验与版本发布服务，支撑《全球数字贸易发展报告》的多轮补报、口径统一、版本会签发布与审计重算。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/digital_trade_statistics/：统计规则版本、校验管线、报送与发布服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m digital_trade_statistics.acceptance

验收命令在临时 SQLite 数据库中执行完整链路，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。统计服务验收覆盖：报送与修订、校验队列重启恢复、豁免与会签职责分离、发布锁跨重启保持、版本公开、审计确定性重算、公共接口脱敏与公开后更正。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m digital_trade_statistics.api --database statistics.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态、校验队列位置、发布锁和审计历史继续保留。统计服务的 `/public/*` 接口不要求操作者身份，只返回允许公开的结果。

## 统计资料接收、校验与版本发布服务

### 角色

在基础角色（admin、operator、reviewer、auditor）之上扩展：

- `submitter`（报送员）：只能提交、修订和查看本机构的数据集；
- `analyst`（分析员）：在授权指标范围内创建派生指标，创建候选版本，驱动校验队列；
- `exemption_approver`（豁免批准人）：批准自动检查发现项的人工豁免；
- `release_approver`（会签/发布人）：会签候选版本、持有发布锁并发布版本；
- 职责分离：候选版本创建人不得会签或发布；批准过本版本豁免的人不得会签或发布；发布人不得兼任会签人。

### 报送与修订

- 数据集携带期间（YYYY 或 YYYY-Qn）、口径、单位币种、来源证明和保密级别（public/internal/confidential）；
- 每次原始报送永久保留；修订必须声明 `revision_of`（替代对象）与 `revision_reason`（原因），且只能基于本机构最新版本修订，同一经济体、期间与口径只允许一个有效版本；
- 报送即进入持久化校验队列，队列位置在 SQLite 中单调分配，服务重启后位置不变，中断的运行自动退回待处理。

### 校验（按指定规则版本）

规则版本登记允许口径、应报经济体、基准币种、覆盖度阈值、跨期容差与所需会签人数；汇率与分类映射均带生效区间且禁止重叠。校验管线依次执行：

1. 结构检查（口径、保密级别、来源证明、观测形态、数值合法性）；
2. 币种换算（按期间匹配生效汇率，缺失记错误）；
3. 分类映射（按期间匹配生效映射，缺失记错误）；
4. 缺口标记（规则要求而未报的指标记警告并生成缺口观测）；
5. 跨期一致性（与上期有效值比较，超容差记警告）。

自动检查（stat_validation_findings）、人工豁免（stat_exemptions）与复核意见（stat_reviews）分别记录。存在未豁免错误发现项的数据集不进入发布。

### 版本发布与更正

- 候选版本汇总当期有效报送，覆盖度 = 已有非缺口值的（经济体 × 指标）格数 / 应有格数，须达到规则阈值；
- 达到规定会签人数且发布人持有发布锁才能公开；发布锁持久化在 SQLite，审阅期间重启不丢失；
- 公开时冻结证据快照（数据集摘要、校验运行、派生指标公式摘要）与全部指标结果（全球合计、分组值、分经济体贡献）；
- 公开后的更正形成新版本：登记受影响的经济体、指标、图表数据和文字结论，经同样的会签与发布流程后替代旧版本，旧版本保留可查。

### 派生指标

分析员只能在被授予的指标范围内创建派生指标（sum/ratio 公式），派生指标随版本一起冻结并参与重算。

### 审计重算

审计员可对任一发布版本执行确定性重算：按证据快照逐条核对数据集摘要，用同一规则版本重跑规范化与汇总，比对冻结的全球合计、分组值和贡献明细（含公开属性）。任何篡改（如直接改动原始载荷）都会导致重算不匹配并列出差异。全部金额计算使用 Decimal 并按 6 位小数固定精度量化，保证同一证据快照重算结果逐字节一致。

### 公共接口

`GET /public/releases` 与 `GET /public/indicators` 只返回已公开版本的结果；凡有非 public 保密级别报送参与计算的指标，其全球值、分组值与贡献明细一律不通过公共接口暴露，仅在 `restricted_indicators` 中列出指标代码。
