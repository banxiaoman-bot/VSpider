# VSpider - 纯视觉网页智能体 (Visual Web Agent)

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Playwright-async-green.svg)](https://playwright.dev/python/)

一个基于**纯视觉大模型 (VLM)** 驱动的网页自动化 Agent，专为**离线内网环境**设计。

## 🏗️ 架构

```
截图 → SoM 标注 → VLM 决策 → 浏览器操作 → 循环
```

```text
visual_web_agent/
├── main.py            # 主程序入口，Agent 核心执行循环
├── config.py          # 配置管理（.env 加载）
├── browser_env.py     # Playwright 浏览器封装（导航、截图、操作执行）
├── vlm_client.py      # VLM 异步客户端（请求、重试、历史记忆）
├── prompts.py         # System Prompt 定义
├── data_manager.py    # 数据管理（Excel 保存、去重、过滤）
├── som_inject.js      # SoM 视觉标记注入脚本
└── requirements.txt   # 依赖包
```

## ✨ 核心特性

- **纯视觉驱动**：通过 SoM (Set of Mark) 标注截图 + VLM 决策，无需解析 DOM 结构
- **Cookie/登录态持久化**：跨次运行复用浏览器会话
- **XHR/Fetch 自动拦截**：后台静默捕获 API 返回的报表数据
- **智能元素穿透**：即使 VLM 选中了 wrapper 元素，也能自动定位到真正的 input/button
- **反检测**：集成 playwright-stealth 隐身衣
- **对话历史记忆**：VLM 可感知最近操作历史，避免死循环
- **API 自动重试**：偶发网络错误自动重试
- **Dialog 自动处理**：alert/confirm/prompt 弹窗自动接受

## 🚀 快速开始

### 1. 安装依赖

```bash
cd visual_web_agent
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 VLM

```bash
cp .env.example .env
# 编辑 .env，填入你的 VLM API 地址和 Key
```

### 3. 运行

```bash
python main.py --url "http://your-target.com" --goal "你的任务描述"
```

## 📖 CLI 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--url` | ✅ | 目标网页 URL |
| `--goal` | ✅ | 自然语言任务描述 |
| `--user-data-dir` | ❌ | 浏览器数据目录（Cookie 持久化） |
| `--context` | ❌ | 额外上下文信息 |
| `--constraints` | ❌ | 操作约束和限制 |
| `--output` | ❌ | 输出要求 |

## 🎯 支持的操作

| 动作 | 说明 |
|------|------|
| `click` | 点击元素 |
| `type` | 输入文本（自动清空 + 逐字键入） |
| `hover` | 悬停展开下拉菜单 |
| `scroll` | 页面滚动 |
| `select` | 原生下拉框选择 |
| `press_key` | 键盘按键（Enter/Escape/Tab） |
| `goto` | URL 直接导航 |
| `extract` | 视觉数据提取 |
| `extract_link` | 提取元素链接 |
| `download_image` | 下载图片 |
| `upload` | 文件上传 |
| `done` | 任务完成 |

## 📋 运行示例

```bash
# 登录并查询数据
python main.py \
  --url "http://192.168.1.100/login" \
  --goal "登录营销2.0系统并进入查询页面"

# 带约束的复杂任务
python main.py \
  --url "https://example.com" \
  --goal "导出所有订单数据" \
  --constraints "如果遇到验证码，执行 ask_human" \
  --output "保存为 orders.xlsx"
```

## ⚙️ 环境变量

参见 [.env.example](visual_web_agent/.env.example) 了解所有可配置项。

## 📄 License

MIT
