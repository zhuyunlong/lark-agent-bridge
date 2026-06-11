# S3：config 子包化 + load_config 分解

## 动机
`config.py` 1039 行，其中 `load_config` 单函数约 690 行：22 个 section 的解析逻辑、AI 预设应用、agent provider 推导全部内联，新增配置项只能继续往巨型函数里堆。

## 改动
- 新建 `configuration/` 子包：
  - `coercion.py`（80 行）：9 个值转换/校验辅助（_bool_value/_string_list/_resolve_path 等）
  - `paths.py`（97 行）：工作区/仓库路径推导 + 误配置告警
  - `logging.py`（19 行）：配置加载期可降级 logger
  - `presets.py`（103 行）：AI provider 预设加载/应用/推导
  - `sections.py`（524 行）：**每个 config section 一个构建器函数**（21 个），默认值与原实现逐字段一致；`AgentBindings` 类承载 bug/intent/source 三域 provider+command 的交叉推导
  - `loader.py`（130 行）：装配主干，load_config 从 690 行缩到约 90 行
- `config.py` 缩为 37 行 facade，re-export 公共 API 与全部私有辅助（防御性兼容）。

## 收益
- 新增 config section 只需在 sections.py 加一个构建器 + loader 一行接线 → **可扩展性达成**。
- S20 配置化新 section（limits/health/auth/state/cards）有了清晰落点。

## 验证
- 定向：test_config + test_cli + test_pydantic_agents + test_parser = 139 passed
- 全量：1323 passed, 3 skipped（与基线一致）→ 兼做 S4 里程碑回归 1
