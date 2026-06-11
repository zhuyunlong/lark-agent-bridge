# S6–S8：app 结果域 / 资源域 / 上下文域拆分

工具：`tmp/split_mixin.py` + 各 spec（spec_result_bug / spec_log_resources / spec_context_from）。
模式同 S5：大 Mixin → 同目录主题小 Mixin，原文件保留核心方法并继承新 Mixin，`BridgeApp` 契约不变。

## S6 result_bug.py：1669 → 198
- `skill_clarify.py`（475）`_SkillClarifyMixin`：Skill/意图澄清选项构建、匹配、确认执行
- `request_exec.py`（401）`_RequestExecMixin`：6+ 业务请求执行入口与审批流
- `card_actions.py`（586）`_CardActionsMixin`：8 种卡片交互动作处理
- 残留核心：进度卡片完成/裁剪、消息 ID 映射、进度卡片构建
- 验证：test_app_bug_followup + test_app_direct_analysis = 58 passed；全量绿

## S7 log_resources.py：1513 → 143
- `progress_notify.py`（314）`_ProgressNotifyMixin`：进度通知与交付结果预处理
- `intent_dispatch.py`（328）`_IntentDispatchMixin`：意图决策分发与上下文模式
- `request_build.py`（246）`_RequestBuildMixin`：各域请求对象构建
- `version_lookup.py`（85）`_VersionLookupMixin`：ROM/导航版本回溯
- `resources.py`（408）`_ResourcesMixin`：资源收集/继承/本地授权
- 残留核心：卡片重析/升级动作、事件转换
- 验证：test_app_direct_analysis + test_app_bug_request = 80 passed；全量绿

## S8 context_from.py：1351 → 463
- `followup_clarify.py`（410）`_FollowupClarifyMixin`：时间/堆栈澄清、Skill 确认、直传恢复
- `existing_answer.py`（222）`_ExistingAnswerMixin`：既有产物快速回答与置信评分
- `context_lookup.py`（256）`_ContextLookupMixin`：会话上下文多级查找
- 残留核心：意图初始化（direct/perception/chat）、_handle_followup 主干、追问收尾
- 验证：test_app_followup_reply + test_app_bug_followup = 57 passed；全量绿

## 结果
app/ 目录现在 19 个文件，最大 649 行（routes.py），全部低于 800 行软目标。
每步后全量回归均为 1323 passed, 3 skipped（与基线一致）。
