# 分析历史与证据日志保留计划

## 目标

保留已经触发过的日志分析记录，并在后台提供专业的历史查询页。历史页需要能选择一次调查，展示调查过程、最终结论、报告链接和保留的证据日志；同时支持删除记录。当前不做权限管理，但删除接口和 UI 需要留下权限占位。

## 行为边界

- `listen` 启动不再清空 `data/jobs/*` 和已发布报告。
- 定时 cleanup 不再删除 job 输出、已发布报告、会话进度、会话上下文或 case 历史；仍允许清理临时 bug cache。
- 删除历史记录时，删除对应 activity session、case 记录、job 目录、已发布 report 目录；返回权限占位信息，后续可以接入用户角色判断。
- 历史查询以 `AgentActivityStore` 的 session 为主，因为它保存了调查过程 progress、最终 message、details、job/report 路径；`CaseStore` 继续保留 bug 维度索引和人工确认能力。
- 证据日志包在分析结束时写入 job 输出目录：优先根据报告/metadata 中引用到的 `log0`/`log1`/`log2` 判断问题落点；只保留命中 log 下的导航日志、`logd`，以及结论引用到或同级可识别的 `vehicle` 相关日志。

## 实施步骤

1. 先补测试：
   - activity store 支持删除 session。
   - case store 支持按 case id 和 job id 删除。
   - report server 暴露 `/api/analysis-history`、详情和 DELETE。
   - cleanup 不再删除历史 job/report/activity。
   - evidence log collector 选择命中 log 并复制导航/logd/vehicle。
2. 实现存储和 API：
   - 增加分析历史查询、详情、删除。
   - 删除逻辑限制在 `data/jobs` 和 `published_reports` 之内，避免误删外部路径。
3. 实现证据日志包：
   - 新增独立模块负责扫描引用、判断 focus log、复制证据文件并输出 manifest。
   - 在 bug/direct analysis 结果 details 中记录 `evidence_log_bundle` 和 `evidence_log_manifest`。
4. 更新后台页面：
   - 将历史页改成“分析历史”列表 + 详情双栏。
   - 展示调查过程、结论、报告、证据日志、删除按钮、权限占位状态。
5. 更新配置和文档：
   - 默认 `purge_all_on_listen_start=false`。
   - README / docs 描述新的历史保留策略。
