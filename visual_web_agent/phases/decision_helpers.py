"""Decision helper functions extracted from main.py (Slice 1).

Functions for classifying VLM decisions (completion detection, irrelevant
nav clicks, search controls, backtracking), goal preferences (visual
navigation, vision fallback, bulk extraction), and done/completion logic.
"""
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..vlm_client import TaskPlan

try:
    from ._env_utils import _text_matches_patterns
    from .goal_parser import _goal_is_form_fill, _goal_is_tooltip_extract
    from ..extraction_engine.strategies import infer_goal_output_contract
except ImportError:
    from phases._env_utils import _text_matches_patterns  # type: ignore[no-redef]
    from phases.goal_parser import _goal_is_form_fill, _goal_is_tooltip_extract  # type: ignore[no-redef]
    from extraction_engine.strategies import infer_goal_output_contract  # type: ignore[no-redef]


def _goal_prefers_visual_navigation(goal: str) -> bool:
    """
    某些任务天然依赖“看见列表中的具体条目再点击”，
    例如排行榜、搜索结果列表、按序号选择第 N 个项目等。
    这类场景如果 text-only 抽到的 DOM 太稀，应尽快回退到视觉模式。
    """
    patterns = [
        r"排名", r"榜单", r"列表", r"top\s*\d+", r"top250",
        r"第\s*\d+\s*(个|条|项|部|集|页|名|行|列|条记录)", r"第[一二三四五六七八九十两]+",
        r"第一[个条项目部集名行列]", r"第二[个条项目部集名行列]",
        r"点击列表中的", r"找到并点击", r"按.*排序", r"最多播放", r"最热", r"最新",
        r"搜索结果", r"结果页", r"查询结果", r"明细", r"详情", r"表格", r"报表",
        r"电影", r"视频", r"商品", r"文章", r"工单", r"告警", r"缺陷", r"台账",
        r"设备", r"站点", r"站所", r"变电站", r"线路", r"馈线", r"回路", r"台区",
        r"审批", r"流程", r"任务单", r"检修", r"巡检", r"户号", r"档案",
    ]
    return _text_matches_patterns(
        goal,
        patterns,
        extra_env_name="VSPIDER_EXTRA_VISUAL_NAV_KEYWORDS",
    )


def _goal_should_force_vision(goal: str) -> bool:
    """
    对“榜单/列表/表格/结果页里定位具体条目”的任务，默认优先视觉模式。
    这类任务在 text-only 下最容易误点站点全局导航，内网页面尤其如此。
    """
    return _env_flag("VSPIDER_FORCE_VISION_FOR_LIST_TASKS", default=True) and _goal_prefers_visual_navigation(goal)


def _should_fallback_to_vision(goal: str, text_snapshot: str) -> tuple[bool, str]:
    lines = [line.strip() for line in (text_snapshot or "").splitlines() if line.strip()]
    if not lines:
        return True, "text snapshot empty"

    id_lines = [line for line in lines if line.startswith("[ID:")]
    text_lines = [line for line in lines if line.startswith("[TEXT]")]

    if _goal_prefers_visual_navigation(goal):
        if len(lines) < 18:
            return True, f"goal needs list-item navigation but text snapshot is sparse ({len(lines)} lines)"
        if len(id_lines) < 12 and len(text_lines) < 4:
            return True, (
                "goal needs visual list discovery but current text snapshot mostly contains "
                "navigation chrome"
            )

    return False, ""


def _find_target_line(input_descriptions: str, target_id: int) -> str:
    if not input_descriptions or not target_id:
        return ""
    pattern = rf"^\[ID:\s*{int(target_id)}\](.*)$"
    for line in input_descriptions.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            return line.strip()
    return ""


def _decision_is_irrelevant_nav_click(decision: dict, goal: str, input_descriptions: str) -> bool:
    """
    当任务本质上是在列表/结果页里找具体条目时，
    若模型却去点击“电影/音乐/阅读/首页/工作台”之类的全站导航，应直接拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_prefers_visual_navigation(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    nav_patterns = [
        r">首页</", r">电影</", r">音乐</", r">阅读</", r">读书</", r">同城</", r">小组</",
        r">FM</", r">时间</", r">豆品</", r">播客</", r">工作台</", r">控制台</",
        r">系统管理</", r">帮助</", r">设置</", r">个人中心</", r">消息</",
    ]
    return _text_matches_patterns(
        line,
        nav_patterns,
        extra_env_name="VSPIDER_EXTRA_GLOBAL_NAV_KEYWORDS",
    )


def _goal_is_bulk_extraction(goal: str) -> bool:
    if _goal_is_tooltip_extract(goal):
        return False
    try:
        if infer_goal_output_contract(goal).get("mode") == "answer":
            return False
    except Exception:
        pass
    return bool(
        re.search(
            r"获取|提取|抓取|采集|爬取|抽取|\bextract(?:_link)?\b|\bscrape\b|\bcrawl\b",
            str(goal or ""),
            re.IGNORECASE,
        )
    )


def _decision_click_targets_extraction_control(
    decision: dict,
    input_descriptions: str,
) -> bool:
    action = (decision.get("action") or "").strip().lower()
    if action not in ("click", "click_text", "click_point"):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id) if target_id else ""
    text = " ".join(
        part
        for part in (
            line,
            str(decision.get("type_value") or ""),
            str(decision.get("thought") or ""),
        )
        if part
    )
    if not text:
        return False

    control_patterns = [
        r"\bnext\b", r"\bprev(?:ious)?\b", r"\bmore\b", r"load\s*more",
        r"\bpage\b", r"pagination", r"下一页", r"上一页", r"更多", r"加载更多",
        r"搜索", r"查询", r"\bsearch\b", r"\bquery\b", r"筛选", r"\bfilter\b",
        r"排序", r"\bsort\b", r"刷新", r"\brefresh\b", r"展开", r"收起",
        r"textbox", r"input", r"combobox", r"select", r"下拉",
    ]
    return _text_matches_patterns(
        text,
        control_patterns,
        extra_env_name="VSPIDER_EXTRA_EXTRACTION_CONTROL_KEYWORDS",
    )


def _goal_explicitly_requests_voice_or_camera(goal: str) -> bool:
    patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"qr", r"lens",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in patterns)


def _goal_is_plain_search_task(goal: str) -> bool:
    if _goal_explicitly_requests_voice_or_camera(goal):
        return False
    search_patterns = [
        r"搜索", r"查询", r"search", r"query",
        r"搜索框", r"关键词", r"输入.*搜索框", r"点击搜索按钮",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in search_patterns)


def _decision_is_auxiliary_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页常见误点：把语音搜索、相机/拍照搜索、扫码入口当成主搜索按钮。
    这类按钮通常不是用户要的“正常输入 + 搜索”路径，应优先拦截。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    aux_patterns = [
        r"语音", r"麦克风", r"voice", r"microphone",
        r"相机", r"camera", r"拍照", r"图片搜索", r"以图搜图",
        r"扫码", r"扫一扫", r"lens",
    ]
    return any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in aux_patterns)


def _decision_is_non_submit_search_control_click(
    decision: dict,
    goal: str,
    input_descriptions: str,
) -> bool:
    """
    搜索页另一个常见误判：把搜索建议项、清除按钮、历史记录入口当成"搜索提交按钮"。
    这类控件会让流程停留在输入态，随后模型又直接 done。
    """
    if (decision.get("action") or "").strip().lower() != "click":
        return False
    if not _goal_is_plain_search_task(goal):
        return False

    target_id = int(decision.get("target_id", 0) or 0)
    line = _find_target_line(input_descriptions, target_id)
    if not line:
        return False

    patterns = [
        r"搜索建议", r"建议", r"suggest", r"history", r"历史记录",
        r"删除", r"清除", r"clear", r"trigger",
    ]
    if not any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
        return False

    # 真实搜索按钮本身也可能带 search 字样；避免误杀明显 submit 场景
    submit_markers = [r"搜索", r"提交", r"submit", r"search button", r"百度一下"]
    if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in submit_markers):
        return False
    return True


def _search_goal_done_looks_premature(
    goal: str,
    start_url: str,
    current_url: str,
    page_summary: str,
    semantic_text: str,
) -> bool:
    """
    对常规搜索任务做一层完成态校验：
    - 若仍停留在起始搜索页/输入态，且出现搜索建议、删除、历史记录等痕迹，则不应 done
    - 若 URL 已带查询参数或页面明显进入结果态，则允许 done
    """
    if not _goal_is_plain_search_task(goal):
        return False

    current_url = (current_url or "").strip()
    start_url = (start_url or "").strip()
    summary_text = "\n".join(part for part in (page_summary, semantic_text) if part)

    result_markers = [
        r"结果页", r"搜索结果", r"results?", r"search results?",
        r"\bq=", r"\bquery=", r"\bwd=", r"\bkeyword=", r"\btext=",
    ]
    if any(re.search(pattern, current_url, flags=re.IGNORECASE) for pattern in result_markers):
        return False
    if any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in result_markers):
        return False

    input_stage_markers = [
        r"搜索建议", r"suggest", r"历史记录", r"history",
        r"删除", r"清除", r"clear",
    ]
    same_page = current_url.rstrip("/") == start_url.rstrip("/")
    if same_page and any(re.search(pattern, summary_text, flags=re.IGNORECASE) for pattern in input_stage_markers):
        return True

    return False


def _decision_implies_completion(decision: dict) -> bool:
    """
    当模型在 thought/current_state 中已经明确承认"任务已完成"，
    但 action 仍然输出 click/type 等动作时，进行通用兜底。
    """
    if (decision.get("action") or "").strip().lower() == "done":
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    follow_up_patterns = [
        r"进入下一阶段",
        r"进入下一子目标",
        r"进入下一步",
        r"推进至下一",
        r"推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"可推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"下一阶段",
        r"下一子目标",
        r"下一步",
        r"接下来",
        r"随后",
        r"然后",
        r"继续(?:执行|点击|选择|展开|翻页|填写|提取|搜索)?",
        r"填写(?:数据)?阶段",
        r"提交阶段",
        r"提取阶段",
        r"还需(?:要)?",
        r"仍需(?:要)?",
        r"需要继续",
        r"需要再",
        r"需(?:要)?(?:点击|选择|展开|输入|提取|翻页|确认)",
        r"点击[^\n]{0,40}以(?:展开|打开|进入|继续|完成|选择|获取)",
        r"选择[^\n]{0,40}以(?:展开|进入|继续|完成)",
        r"展开[^\n]{0,40}以(?:继续|完成|查看)",
        r"以展开其子项",
    ]
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in follow_up_patterns):
        return False

    completion_patterns = [
        r"任务已完成",
        r"任务已(?:经)?(?:全部)?完成",
        r"任务已(?:经)?(?:全部)?达成",
        r"任务[^\n]{0,30}达成退出标准",
        r"用户目标已(?:经)?(?:全部)?达成",
        r"目标已(?:经)?(?:全部)?达成",
        r"已全部达成",
        r"无需再操作",
        r"不需要再操作",
        r"无需再点击",
        r"不需要再点击",
        r"选择已(?:经)?完成",
        r"已成功选择",
        r"可直接结束任务",
        r"可以直接结束任务",
        r"直接结束任务",
        r"直接输出\s*done",
        r"准备输出\s*done",
        r"task (?:is )?complete(?:d)?",
        r"goal (?:has been )?achieved",
        r"already completed",
        r"no further action needed",
        r"all required steps have been completed",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in completion_patterns)


def _decision_mentions_follow_up_work(decision: dict) -> bool:
    """Whether the decision text still describes remaining user-visible work."""
    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    follow_up_patterns = [
        r"进入下一阶段",
        r"进入下一子目标",
        r"进入下一步",
        r"推进至下一",
        r"推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"可推进至[^\n]{0,30}(?:阶段|子目标|步骤)",
        r"下一阶段",
        r"下一子目标",
        r"下一步",
        r"接下来",
        r"随后",
        r"然后",
        r"继续(?:执行|点击|选择|展开|翻页|填写|提取|搜索)?",
        r"填写(?:数据)?阶段",
        r"提交阶段",
        r"提取阶段",
        r"还需(?:要)?",
        r"仍需(?:要)?",
        r"需要继续",
        r"需要再",
        r"需(?:要)?(?:点击|选择|展开|输入|提取|翻页|确认)",
        r"点击[^\n]{0,40}以(?:展开|打开|进入|继续|完成|选择|获取)",
        r"选择[^\n]{0,40}以(?:展开|进入|继续|完成)",
        r"展开[^\n]{0,40}以(?:继续|完成|查看)",
        r"以展开其子项",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in follow_up_patterns)


def _decision_claims_current_subgoal_completed(decision: dict) -> bool:
    """
    对 action=done 的决策做补充判定：若模型在 progress_review/thought/current_state
    中明确声明“当前子目标退出标准已满足”，即使漏填 subgoal_status，也视作当前
    子目标已完成。
    """
    if (decision.get("subgoal_status") or "").strip().lower() == "completed":
        return True

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    exit_met_patterns = [
        r"满足(?:了)?[^\n]{0,30}退出标准",
        r"符合[^\n]{0,30}退出标准",
        r"子目标[^\n]{0,20}(?:已完成|完成)",
        r"当前子目标[^\n]{0,20}(?:已完成|完成)",
        r"已验证[^\n]{0,40}(?:成功|完成|可见|已加载)",
        r"exit criteria (?:is )?met",
        r"current subgoal (?:is )?complete(?:d)?",
    ]
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in exit_met_patterns):
        return True

    completion_patterns = [
        r"任务已完成",
        r"任务已(?:经)?(?:全部)?完成",
        r"任务已(?:经)?(?:全部)?达成",
        r"任务[^\n]{0,30}达成退出标准",
        r"用户目标已(?:经)?(?:全部)?达成",
        r"目标已(?:经)?(?:全部)?达成",
        r"已全部达成",
        r"无需再操作",
        r"不需要再操作",
        r"无需再点击",
        r"不需要再点击",
        r"选择已(?:经)?完成",
        r"已成功选择",
        r"可直接结束任务",
        r"可以直接结束任务",
        r"直接结束任务",
        r"直接输出\s*done",
        r"准备输出\s*done",
        r"task (?:is )?complete(?:d)?",
        r"goal (?:has been )?achieved",
        r"already completed",
        r"no further action needed",
        r"all required steps have been completed",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in completion_patterns)


def _decision_should_finish_instead_of_operate(
    decision: dict,
    *,
    output_mode: str = "",
) -> bool:
    """Answer-only guard for contradictory "complete, but still click" decisions."""
    action = (decision.get("action") or "").strip().lower()
    if not action or action in {
        "done",
        "ask_human",
        "error",
        "extract",
        "chat_extract",
        "fetch_link_content",
        "fetch_links_batch",
        "download_image",
        "upload",
        "save_to_memory",
    }:
        return False
    if str(output_mode or "").strip().lower() != "answer":
        return False
    if decision.get("extracted_data"):
        return False
    if _decision_mentions_follow_up_work(decision):
        return False
    if not _decision_claims_current_subgoal_completed(decision):
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("progress_review", "current_state", "thought")
    ).strip()
    if not text:
        return False

    finish_patterns = [
        r"无需(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"无须(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"不需要(?:再|进一步)?(?:操作|点击|搜索|处理|交互)",
        r"应(?:该)?直接(?:结束|输出\s*done)",
        r"可(?:以)?直接(?:结束|输出\s*done)",
        r"任务目标(?:已|已经)?(?:达成|满足|完成)",
        r"用户(?:问题|需求|目标)[^\n]{0,80}已(?:完全)?(?:覆盖|满足|达成)",
        r"所有信息(?:均|都)?已(?:可见|覆盖|满足)",
        r"no further (?:action|operation|click|search) needed",
        r"should (?:finish|end|return done)",
        r"(?:task|goal) (?:is )?(?:complete|completed|satisfied|achieved)",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in finish_patterns)


def _done_targets_final_subgoal(plan: "TaskPlan | Any | None", decision: dict) -> bool:
    """Whether a done decision is completing the already-active final subgoal.

    PLAN GATE runs before Wave 2 advances current_idx, so the first done on the
    last subgoal should be allowed when the model explicitly marks that subgoal
    complete. Otherwise result-page form tasks pay an unnecessary wait/extract
    tail before the second done is accepted.
    """
    if plan is None:
        return False
    sub_goals = list(getattr(plan, "sub_goals", []) or [])
    if not sub_goals:
        return False
    try:
        cur_idx = int(getattr(plan, "current_idx", 0) or 0)
    except Exception:
        cur_idx = 0
    cur_idx = max(0, min(cur_idx, len(sub_goals) - 1))
    return cur_idx >= len(sub_goals) - 1 and _decision_claims_current_subgoal_completed(decision)


def _is_terminal_only_subgoal(subgoal: "TaskPlan | Any") -> bool:
    """识别仅用于收尾输出 done 的行政型尾子目标。"""
    description = str(getattr(subgoal, "description", "") or "").strip()
    exit_criteria = str(getattr(subgoal, "exit_criteria", "") or "").strip()
    if not (description or exit_criteria):
        return False

    def _strip_negated_noop_phrases(text: str) -> str:
        cleaned = text
        noop_patterns = [
            r"不执行任何交互",
            r"无需任何交互",
            r"不需要任何交互",
            r"无须任何交互",
            r"无需再操作",
            r"不需要再操作",
            r"无须再操作",
            r"无需任何操作",
            r"不需要任何操作",
            r"无须任何操作",
        ]
        for pattern in noop_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        return cleaned

    _action_text = _strip_negated_noop_phrases(
        "\n".join(part for part in (description, exit_criteria) if part)
    )
    physical_action_patterns = [
        r"点击",
        r"单击",
        r"\bclick(?:_new_tab|_point)?\b",
        r"输入",
        r"填写",
        r"\btype\b",
        r"提取",
        r"\bextract(?:_link)?\b",
        r"按(?:键|下)",
        r"\bpress_key\b",
        r"滚动",
        r"翻页",
        r"\bscroll\b",
        r"\bsmooth_scroll\b",
        r"悬停",
        r"\bhover\b",
        r"选择",
        r"\bselect\b",
        r"上传",
        r"\bupload\b",
        r"下载",
        r"导出",
        r"\bdownload(?:_image)?\b",
        r"拖拽",
        r"拖动",
        r"\bdrag(?:_and_drop)?\b",
        r"移除",
        r"\bremove_element\b",
        r"导航",
        r"跳转",
        r"\bgoto\b",
        r"登录",
        r"搜索",
        r"提交",
        r"关闭(?:弹窗|对话框|标签页)?",
        r"\bclose_tab\b",
        r"切换(?:标签页)?",
        r"\bswitch_tab\b",
        r"\bask_human\b",
        r"人工处理",
    ]
    if any(re.search(pattern, _action_text, flags=re.IGNORECASE) for pattern in physical_action_patterns):
        return False

    terminal_prefix_patterns = [
        r"^\s*任务完成(?:[:：,，。；!！\s].*)?$",
        r"^\s*结束任务(?:[:：,，。；!！\s].*)?$",
        r"^\s*任务终止(?:[:：,，。；!！\s].*)?$",
        r"^\s*终止任务(?:[:：,，。；!！\s].*)?$",
        r"^\s*确认目标达成(?:后)?(?:[:：,，。；!！\s].*)?$",
        r"^\s*完成\s*goal\s*全部要求(?:[:：,，。；!！\s].*)?$",
        r"^\s*完成用户全部需求(?:[:：,，。；!！\s].*)?$",
        r"^\s*(?:准备)?输出\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*action\s*=\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*直接\s*done(?:[:：,，。；!！\s].*)?$",
        r"^\s*ready to output done(?:[:：,，。；!！\s].*)?$",
        r"^\s*finish(?: the)? task(?:[:：,，。；!！\s].*)?$",
        r"^\s*terminate(?: the)? task(?:[:：,，。；!！\s].*)?$",
        r"^\s*不执行任何交互(?:[:：,，。；!！\s].*)?$",
        r"^\s*无需任何交互(?:[:：,，。；!！\s].*)?$",
    ]

    for candidate in (description, exit_criteria):
        if candidate and any(
            re.search(pattern, candidate, flags=re.IGNORECASE)
            for pattern in terminal_prefix_patterns
        ):
            return True
    return False


def _goal_explicitly_requests_backtracking(goal: str) -> bool:
    """用户目标若明确要求返回/回到某页，则不启用回退拦截。"""
    backtrack_patterns = [
        r"返回", r"回到", r"回退", r"上一页",
        r"go back", r"return to", r"back to",
    ]
    return any(re.search(pattern, goal, flags=re.IGNORECASE) for pattern in backtrack_patterns)


def _decision_is_regressive_backtrack(decision: dict, goal: str) -> bool:
    """
    结果页/确认页上最常见的误判是：为了"补走中间步骤"又返回首页重做。
    除非用户明确要求返回，否则这种回退一般应直接视为 done。
    """
    if _goal_explicitly_requests_backtracking(goal):
        return False

    action = (decision.get("action") or "").strip().lower()
    if action not in {"click", "goto", "switch_tab"}:
        return False

    text = "\n".join(
        str(decision.get(key, "") or "")
        for key in ("current_state", "thought")
    ).strip()
    if not text:
        return False

    result_page_patterns = [
        r"结果页", r"结果页面", r"搜索结果", r"查询结果",
        r"成功页", r"确认页", r"已提交", r"已执行完毕",
        r"search results?", r"results? page", r"confirmation page",
        r"success page", r"submitted successfully",
    ]
    backtrack_patterns = [
        r"返回首页", r"回到首页", r"返回主页", r"回到主页",
        r"返回主页面", r"回到主页面", r"返回上一页", r"回到上一页",
        r"返回上一步", r"回到上一步",
        r"go back", r"back to home", r"return to home",
        r"return to homepage", r"back to the main page",
    ]

    return (
        any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in result_page_patterns)
        and any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in backtrack_patterns)
    )


def _force_done_decision(decision: dict) -> dict:
    """将一条自相矛盾的决策强制纠正为 done。"""
    normalized = dict(decision)
    normalized["action"] = "done"
    normalized["target_id"] = 0
    normalized["type_value"] = ""
    normalized["memory_key"] = ""
    normalized["point"] = None
    return normalized


def _clean_user_visible_done_message(message: object) -> str:
    """Remove engine-only guard annotations from final user-visible done text."""
    text = str(message or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[ANSWER_DONE_INTENT_GUARD\]\s*", "", text)
    text = re.sub(r"\[(?:EXTRACT_NULL|ZERO_TARGET)_DOWNGRADE\]\s*", "", text)
    text = re.sub(
        r"页面答案已满足用户问题，系统将原动作\s*['\"][^'\"]*['\"]\s*改为\s*done[。.]?\s*",
        "",
        text,
    )
    text = re.sub(
        r"Answer-mode decision text says the task is complete;[^\n]*(?:\n|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()
