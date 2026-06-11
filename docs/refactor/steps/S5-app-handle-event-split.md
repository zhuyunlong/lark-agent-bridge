# S5：app/handle_event.py 拆分

## 动机
1813 行主控 Mixin：48 组件初始化、事件入口、40+ 路由分支、结果交付、@提及解析、进度卡片混在一处。

## 改动
用通用拆分工具 `tmp/split_mixin.py`（AST 驱动，S1/S2 教训已内置）拆出 4 个主题 Mixin（同目录，沿用 `from ._shared import *` 风格）：
- `routes.py`（649 行）`_RoutesMixin`：路由矩阵 + 仲裁路由 + would_route 判定
- `delivery.py`（285 行）`_DeliveryMixin`：结果交付/卡片发送/报告就绪通知
- `mention.py`（160 行）`_MentionMixin`：@bot 提及解析
- `progress_cards.py`（182 行）`_ProgressCardsMixin`：进度卡片管理
- `handle_event.py` 缩为 516 行：初始化 / 事件入口 / 核心分发 / 运维入口，类声明改为
  `class _HandleEventMixin(_RoutesMixin, _DeliveryMixin, _MentionMixin, _ProgressCardsMixin)`，
  `BridgeApp` 与全部对外契约不变。

## 验证
- 定向：test_app_bug_request + test_app_group_bug + test_app_followup_reply = 100 passed
- 全量：1323 passed, 3 skipped（与基线一致）
