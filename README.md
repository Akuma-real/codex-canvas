# CodexCanvas

CodexCanvas 是一个基于 `Textual` 的终端界面，用来把本机已经可用的 `codex exec` 图像能力包装成更稳定、可持续使用的工作流。

它不是独立模型服务，也不直接调用远端图片 API。它依赖你本机已经登录并可运行的 `Codex CLI`。

## 项目身份

- 产品显示名：`CodexCanvas`
- 仓库目录 / slug：`codex-canvas`
- Python 包：`codex_canvas`
- 命令名：`codexcanvas`

## 当前能力

- 输入自然语言提示词
- 从系统图片剪贴板导入多张参考图
- 手动指定主参考图
- 通过 `codex exec --image ...` 传入参考图
- 设置高级图像参数：
  - 生成动作：自动 / 纯生成 / 参考图编辑
  - 输出格式：PNG / JPEG / WebP
  - 背景：自动 / 不透明 / 透明
  - 压缩质量：`0-100`，仅 JPEG / WebP 可用
  - 尺寸：自动 / `1024x1024` / `1536x1024` / `1024x1536`
  - 质量：自动 / 低 / 中 / 高
- 自动识别 `$CODEX_HOME/generated_images` 或 `~/.codex/generated_images` 中本次新增结果
- 将最终图片复制到目标输出目录
- 在界面中展示阶段、耗时、输出路径、原始文件、参考图摘要、`stdout` / `stderr` 摘要

## 设计边界

- 真正的硬能力是参考图通过 `--image` 传给 `codex exec`
- `action / format / background / compression / size / quality` 都会写进 prompt，属于提示性约束
- 最终效果仍取决于当前 `Codex CLI` 的图像能力

## 运行前提

- Python `>= 3.11`
- 已安装 `uv`
- 已安装并可直接执行 `codex`
- 当前机器上的 `codex` 已完成登录

如果你设置了 `CODEX_HOME`，程序会改从 `$CODEX_HOME/generated_images` 识别新图片。

## 剪贴板图片依赖

### macOS

当前使用 `pngpaste` 读取系统图片剪贴板：

```bash
brew install pngpaste
```

### Linux

Linux 当前策略是：

1. 优先尝试原生桌面剪贴板 API
   - GTK4
   - GTK3
2. 原生后端不可用时，回退到命令行工具
   - Wayland：`wl-paste`
   - X11：`xclip`

如果你想尽量走“应用自己读取剪贴板”的路径，建议至少具备：

```bash
sudo pacman -S python-gobject gtk4
```

如果你想保留最稳的命令行回退，也建议安装：

```bash
sudo pacman -S wl-clipboard xclip
```

说明：

- Wayland 常用 `wl-clipboard` 提供的 `wl-paste`
- X11 常用 `xclip`
- 没有原生后端也没有这些回退工具时，参考图导入会直接报错

## 安装

开发方式：

```bash
uv sync --dev
```

作为本地工具安装：

```bash
uv tool install .
```

## 启动

推荐使用控制台脚本：

```bash
uv run codexcanvas
```

也支持模块入口：

```bash
uv run python -m codex_canvas
```

## 使用方式

1. 在“提示词”里输入文本描述
2. 如需参考图：
   - 点击“从剪贴板粘贴图片”
   - 或尝试快捷键 `Ctrl+V`、`Ctrl+Shift+V`、`Shift+Insert`、`Cmd+V`
3. 如有多张参考图，可手动切换主参考图
4. 选择高级图像参数
5. 设置“输出目录”；留空时默认写到当前目录下的 `generated_images/`
6. 按 `Ctrl+Enter` 或 `F5` 开始生成

启动后默认焦点会落在“提示词”输入框。

## 参考图与参数说明

- 主参考图优先决定主体、构图与风格
- 其余参考图仅作补充参考
- 删除当前主图后，如果列表仍非空，会自动把第一张剩余图片设为主图
- 清空参考图后会恢复到无参考图状态
- 当输出格式不是 JPEG / WebP 时，压缩质量输入会自动禁用
- `参考图编辑` 模式要求至少有 1 张参考图

## 常见失败场景

### 未找到 `codex`

界面会直接报错：

```text
未找到 codex 命令，请先安装 Codex CLI，并确认它在 PATH 中。
```

先确认：

```bash
codex --help
```

能在当前 shell 中直接执行。

### 剪贴板中没有图片

会直接报错：

```text
剪贴板中没有可读取的图片。
```

先确认你复制的是位图图片，而不是文件路径、文本或浏览器中的普通链接。

### Linux 上无法读取剪贴板图片

先按顺序检查：

1. `python-gobject` / GTK 运行库是否可用
2. Wayland 下是否有 `wl-paste`
3. X11 下是否有 `xclip`

如果这些都没有，程序无法导入参考图。

### `codex exec` 成功，但没识别到新图

CodexCanvas 只会从 `~/.codex/generated_images` 或 `$CODEX_HOME/generated_images` 里识别本次新增或更新的图片。若没有识别到结果，先检查这两个目录是否真的有新文件。

### 输出目录不可写

程序会在开始阶段创建目标目录；如果目录权限不足，会直接在任务摘要里显示失败原因。

### TUI 能启动，但生成阶段长期无输出

界面会在空闲一段时间后提示 `codex exec` 仍在运行。这通常意味着：

- Codex CLI 还在排队或生成
- 当前登录态失效，需要重新登录
- 本机网络或本地环境导致 CLI 阻塞

## 开发与验证

```bash
uv sync --dev
env UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
env UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .
env UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
env UV_CACHE_DIR=/tmp/uv-cache uv build
```

GitHub Actions 默认覆盖 Python `3.11` 和 `3.12`，执行与本地同一套质量门禁。

## 项目结构

```text
.
├── src/codex_canvas/
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   ├── app.tcss
│   ├── clipboard.py
│   ├── models.py
│   └── runner.py
├── tests/
├── .github/workflows/ci.yml
├── CONTRIBUTING.md
├── LICENSE
├── pyproject.toml
└── uv.lock
```
