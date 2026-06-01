# JSON 输出格式规范

⚠️ 顶层结构铁律（违反将触发系统救援 + 警告）：
- JSON 顶层**必须**是 `{"actions": [...]}`，不允许省略 actions 包裹层
- **绝对禁止**直接返回单行数据如 `{"title":"...","points":"...","url":"..."}`
  这种是 extracted_data 的内容，必须包在 `actions[0].extracted_data` 里

## 完整 Action Schema

```json
{
  "actions": [
    {
      "progress_review": "先复盘全局历史和当前子目标进度；若任务已完成，明确写任务已完成",
      "current_state": "当前页面状态描述（URL/标题/可见内容摘要）",
      "thought": "本步的推理过程（基于三步决策法）",
      "action": "click | click_text | click_new_tab | fetch_link_content | fetch_links_batch | chat_extract | type | hover | hover_and_click | row_action | scroll | smooth_scroll | find_text | form_set | wait | select | press_key | goto | extract | extract_link | download_image | upload | close_tab | switch_tab | save_to_memory | done | ask_human | click_point | remove_element | drag_and_drop | next_page",
      "target_id": 0,
      "type_value": "",
      "memory_key": "",
      "status": "success|error|captcha_detected",
      "subgoal_status": "in_progress|completed",
      "extracted_data": []
    }
  ]
}
```

## 关键动作的字段要求（违反会被引擎拒绝）

- **fetch_link_content**：target_id 优先（从链接元素读 href），缺省时 type_value 直接填 URL；
  `memory_key` 必填——结果写入 `workflow_memory[memory_key] = {url, title, content}`。
  支持 type_value JSON `{"url":"...","selectors":["article","main"],"mode":"ax"}`
  限定 DOM 范围 + 去除 nav/footer/header/aside。
- **fetch_links_batch**：批量后台抓取多个链接；target_id=0，type_value 填 JSON：
  `{"target_ids":[16,32,43],"mode":"dom|ax","selectors":["article","main"]}`
  或 `{"urls":[...]}`。`memory_key` 必填。
- **chat_extract**：聊天/AI 助手页**专用回答提取**（取代通用 extract）。
  target_id=0；type_value 留空（高级：JSON `{"timeout":25,"min_length":80}`）；
  `memory_key` 必填（如 `ai_answer`）。引擎内置：主动滚到底 → 等流式完成 → 选择器级联
  定位回答容器（ai-answer / chat-answer / markdown-body / data-message-author-role=assistant 等）→
  排除 nav/footer/search-result/相关推荐 类容器 → 全页 innerText 兜底。
  **只要 host 在 yiyan.baidu.com / chat.baidu.com / chat.openai.com / claude.ai /
  tongyi.aliyun.com / kimi.moonshot.cn / www.doubao.com / chatglm.cn /
  yuanbao.tencent.com / chat.deepseek.com / gemini.google.com /
  copilot.microsoft.com / www.perplexity.ai 这类聊天域名，
  回答提取必须用 chat_extract，不要用通用 extract**。完成后通常下一步 `done`。

## 各字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| progress_review | string | 必填。复盘最近操作历史，确认进度 |
| current_state | string | 必填。当前页面状态描述 |
| thought | string | 必填。本步推理逻辑 |
| action | string | 必填。动作类型 |
| target_id | int | 截图红框编号 / @eN 编号 |
| type_value | string | 输入文本 / 滚动方向 / URL |
| memory_key | string | 跨页面记忆的变量名 |
| status | string | success / error / captcha_detected |
| subgoal_status | string | 当前子目标状态 |
| extracted_data | array | extract 动作时的结构化数据列表 |
