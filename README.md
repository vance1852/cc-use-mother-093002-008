# 编制全球数贸统计版本协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/stat_release/：统计资料接收、校验与版本发布服务（规则版本、报送与修订、校验队列、豁免与复核、派生指标、候选版本会签发布、更正与审计重算）；
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
    PYTHONPATH=src python3 -m stat_release.acceptance

验收命令会在临时 SQLite 数据库中完成各自业务的端到端流程，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。统计模块的验收还会模拟服务重启，核对校验队列位置与发布锁保持不变，并复算两个发布版本的全部指标。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m stat_release.api --database stat_release.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 统计资料接收、校验与版本发布（stat_release）

### 角色

在基础服务的 admin、operator、reviewer、auditor 之上扩展：submitter（报送员，只能处理本机构资料）、analyst（分析员，在授权口径内创建派生指标、组卷）、approver（豁免批准人，不得批准本机构报送的豁免）、publisher（发布人，须持有发布锁）。批准豁免与发布由不同角色结构性隔离，服务在发布时再次核对“批准豁免的人不得发布同一版本”。

### 业务流程

1. admin 登记规则版本（指标目录、口径、汇率与分类映射的生效期、必需经济体、分组、一致性容忍度、覆盖度阈值、会签人数）；
2. submitter 提交带期间、口径、单位、币种、来源证明和保密级别的数据集；每次原始报送永久保留，修订必须声明 supersedes 与 revision_reason，被替代报送仅标记为 superseded；
3. analyst 将数据集按指定规则版本送入校验队列（队列位置持久化），依次完成结构检查、币种换算、分类映射、缺口标记与跨期一致性验证，自动检查结果写入 stat_findings；
4. approver 对错误级发现项登记人工豁免（stat_exemptions），reviewer 的复核意见单独记录（stat_review_opinions）；
5. analyst 组卷候选版本：快照固定数据集摘要、校验运行、豁免、派生指标与基线版本，并计算覆盖度；
6. 达到规定覆盖度、且由至少两个独立机构复核员会签后，publisher 持发布锁发布；公开后的更正必须形成新版本并声明 correction_of，系统自动列出受影响的经济体、指标、图表数据和文字结论；
7. auditor 以发布时固定的证据快照确定性重算任一指标（全球合计、分组值、贡献明细），并与发布结果摘要核对；
8. 公共接口 GET /public/stat/releases 无需身份，只返回允许公开（public 保密级别数据集）计算的结果。

### 确定性约定

- 金额一律使用 Decimal 并按 6 位小数 ROUND_HALF_UP 舍入，序列化为固定精度文本；
- 快照、结果均通过 canonical JSON 的 SHA-256 摘要固定，同一证据快照必然重算出同一结果；
- 汇率与分类映射按生效期（含起止的字符串区间）选择，同一期间命中多条汇率时取生效期最晚的一条；
- 跨期一致性以“当前公开版本”为基线，增速以“上一报告期公开版本”为基线，二者都随快照固定。

### 主要接口

- POST /stat/rules-versions、POST /stat/datasets、POST /stat/validations、POST /stat/validations/process-next；
- POST /stat/exemptions、POST /stat/review-opinions、POST /stat/analyst-scopes、POST /stat/derived-indicators；
- POST /stat/candidates、POST /stat/candidates/{id}/countersign、POST /stat/candidates/{id}/publish；
- POST /stat/locks/acquire、POST /stat/locks/release；
- GET /stat/releases/{id}/snapshot、GET /stat/releases/{id}/recompute?indicator_code=（auditor/admin）；
- GET /public/stat/releases?report_period=（公共，无需身份）。
