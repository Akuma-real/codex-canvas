# Contributing

## 开发前提

- Python `3.11` 或 `3.12`
- `uv`
- 已安装并登录的 `codex` CLI

## 本地开发

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run codexcanvas
```

也可以直接验证模块入口：

```bash
uv run python -m codex_canvas
```

## 提交约定

- 保持 `src/codex_canvas/` 下模块职责清晰，不再新增根目录脚本入口
- UI 或运行逻辑变更请补对应测试
- 新增依赖时同步更新 `uv.lock`
- 提交前至少跑一次 `ruff check`、`ruff format --check`、`pytest`

## 问题排查

- 如果启动时报找不到 `codex`，先确认 `codex` 在当前 `PATH`
- 如果 `codex exec` 成功但界面提示没找到新图，检查 `~/.codex/generated_images`
- 如果输出目录写入失败，先确认目标目录权限
