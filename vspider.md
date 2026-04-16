# 纯视觉网页智能体 (Visual Web Agent) - 离线内网版

## 1. 角色设定
你是一个资深的 Python 后端工程师和 Playwright 网页自动化专家。请帮我从零构建一个基于“纯视觉大模型 (VLM)”驱动的网页自动化 Agent。

## 2. 项目背景与硬性约束（极其重要）
- **运行环境**：纯内网环境（无外网连接）。
- **可用大模型**：只有一个本地部署的 72B VLM（视觉大模型），该模型提供兼容 OpenAI 格式的 API。
- **技术栈限制**：
  - **必须**使用 `Python 3.10+` 和 `async Playwright`。
  - **严禁**使用任何传统 OCR 库（如 Tesseract、PaddleOCR）。
  - **严禁**使用臃肿的 Agent 框架（如 LangChain、AutoGPT）。我们需要极简、可控的自研核心代码。
- **核心交互逻辑 (SoM - Set of Mark)**：
  因为模型无法直接获取 DOM 坐标，我们必须通过向网页注入一段 JavaScript (SoM 脚本)，给页面所有可交互元素（按钮、输入框等）画上红框并标注数字序号。截图后，发给 VLM 进行决策。

## 3. 项目目录结构设计
请按照以下结构帮我生成初始代码：

```text
visual_web_agent/
├── main.py                # 主程序入口，包含 Agent 的核心执行循环 (Loop)
├── config.py              # 配置文件（VLM 的 API URL、Key、超时时间等）
├── browser_env.py         # Playwright 封装类，负责页面导航、截图、执行点击/输入
├── vlm_client.py          # 封装对本地 72B VLM 的请求，负责图片 Base64 编码和 JSON 解析
├── prompts.py             # 存放 System Prompt
├── som_inject.js          # JavaScript 脚本：负责在网页上绘制带数字的标记框
└── requirements.txt       # 依赖包列表

## 4. 核心模块详细开发要求
### 4.1 som_inject.js (视觉标记注入)
功能：遍历当前页面的可见元素（<a>, <button>, <input>, 以及带有 cursor: pointer 的元素）。

行为：在元素边界绘制一个半透明的红框，并在左上角绘制一个背景为黑色的白色数字标签（序号从 1 开始）。

关键机制：必须给被标记的原 DOM 元素加上一个自定义属性，例如 data-som-id="1"，以便后续 Playwright 可以通过 page.click('[data-som-id="1"]') 直接精准点击。

4.2 vlm_client.py (视觉模型客户端)
使用 aiohttp 或 openai 官方 SDK 发起异步请求。

输入参数：当前网页截图（Base64） + 用户目标文本。

强制要求：Prompt 必须要求 VLM 返回严格的 JSON 格式，Schema 如下：

JSON
{
  "thought": "对当前截图的分析过程",
  "action": "click" | "type" | "scroll" | "done",
  "target_id": 5, 
  "type_value": "如果是type操作，这里是输入内容，否则为空",
  "status": "success" | "captcha_detected" | "error"
}
4.3 browser_env.py (浏览器控制)
初始化 async_playwright，启动无头（Headless=False，方便我调试）的 Chromium 浏览器。

核心方法：

mark_and_screenshot()：注入 som_inject.js，等待 0.5 秒渲染后，截取全屏返回 base64 并在本地保存一张 debug.png。

execute_action(action_dict)：解析 VLM 返回的 JSON，如果 action 是 click，则执行 page.click(f'[data-som-id="{target_id}"]')。

4.4 main.py (主控循环)
实现 Agent 的核心运转逻辑（最多循环 15 次，防止死循环）：

接收用户的自然语言目标（如：“登录营销2.0系统并进入查询页面”）。

While 任务未完成：

调用 browser_env 标记并截图。

调用 vlm_client 获取下一步操作 JSON。

解析并执行 action。

如果 action == "done"，结束循环。

## 5. 你的第一步任务
请先向我确认你理解了以上架构。然后，请首先为我生成 requirements.txt 和最关键的 som_inject.js 代码。待我确认后，再继续生成 Python 端的代码。