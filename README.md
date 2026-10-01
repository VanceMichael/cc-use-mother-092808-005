# 央企新兴产业价值贡献归集

核算央企战略性新兴产业、研发和公共服务的价值贡献。

## 参与方与事实

主要参与方包括中央企业数据办公室、产业板块、研究院、财务复核人员、规划管理人员。领域资料记录以下已经确认的事实：

- 规划提出2030年战略性新兴产业增加值占比再提升5个百分点
- 研发经费年均增长率目标大于7%
- 基础研究投入占比力争提高至15%以上

## 业务约束

- 组织与项目边界
- 贡献公式
- 内部交易抵销
- 研发属性
- 规划目标偏差

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/news_context_005.py` 负责读取和校验这些资料。

## 价值贡献治理后端

`src/valuegov/` 是集团价值贡献治理后端（纯标准库，SQLite 持久化），统一维护组织边界、产业分类、项目任务、投资批次、研发费用、基础研究属性、成果转化、公共服务贡献与内部交易抵销。

- `service.py`：核心治理服务。报告期关账后公式与组织范围随快照冻结；重组、项目拆分、跨单位协作按可审计的分配规则归属贡献；内部交易抵销后只保留净影响；业务单位提交证据但不得自行批准特殊归类；规划目标只影响开放期间；历史错误一律通过更正版本处理；批量导入识别重送并把冲突材料交给复核；未完成的关账与证据催补由可恢复作业保证重启不丢。
- `metrics.py`：版本化公式引擎，计算战新增加值占比、基础研究投入占比、研发经费增长率等指标。
- `reporting.py`：管理层视图——占比变化、项目明细、费用归属、抵销过程、审批责任与距离规划目标的真实差额（含关账后更正的重述视图）。
- `api.py`：HTTP JSON API（`make_server(service, host, port)`），变更请求需 `X-Actor` 头。

金额约定：接口以万元为单位，内部按百元整数汇总。快速体验：

```python
from src.valuegov import ValueGovService, period_report

svc = ValueGovService("valuegov.db")          # 重启自动恢复未完成作业
svc.open_period("system-admin", "2030")
# ... 记账、审批、抵销 ...
svc.request_period_close("system-admin", "2030")
report = period_report(svc, "2030")           # 冻结快照 + 重述视图
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
