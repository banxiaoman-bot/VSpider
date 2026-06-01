/**
 * SoM (Set of Mark) v6 — 融合 browser-use + Skyvern 精髓
 *
 * ═══════════════════════════════════════════════════════════════
 * 设计理念对照：
 *
 * [browser-use 精髓]
 *   1. 多层可见性计算：CSS display/visibility/opacity → 视口边界 → 最小尺寸 → 遮挡检测
 *   2. paint-order 遮挡算法：高 z-index / stacking context 的元素覆盖低层
 *   3. 虚拟组件拆分：复合控件（select/date/video）拆成 LLM 友好子组件
 *   4. 父子传播过滤：propagating 容器（a/button）自身不再打 ID，由内部真实交互子节点承载
 *
 * [Skyvern 精髓]
 *   1. 富状态提取：disabled/checked/expanded/required/readonly/selected 全量采集
 *   2. 多源交互判定：ARIA role → 原生标签 → onclick/jsaction → tabindex → cursor:pointer → jQuery/Angular 事件
 *   3. 装饰性容器剪枝：generic div/span/RootWebArea 等无交互意义的节点直接跳过
 *   4. 保留属性白名单：只输出 RESERVED_ATTRIBUTES 中的关键属性，压缩 token 消耗
 *
 * [VSpider 自研]
 *   1. 三级交互分层（L1/L2/L3）：表单控件 > 按钮链接 > cursor:pointer 候选
 *   2. SoM 红框视觉标记：为 VLM 提供「截图中的数字序号 ↔ 元素」的锚定关系
 *   3. 动态 MAX_LABELS：按 L1 密度自适应，防止关键输入框被截断
 *   4. Shadow DOM 递归穿透 + 同源 iframe 深入收集
 * ═══════════════════════════════════════════════════════════════
 *
 * v6 升级重点（相对 v5）:
 *   1. ★ 严密 5 层可见性引擎（借鉴 browser-use）
 *   2. ★ 富语义状态提取（借鉴 Skyvern）：disabled/checked/expanded/required/readonly/selected
 *   3. ★ 多源交互判定（借鉴 Skyvern）：onclick/jsaction/tabindex/cursor/Angular/jQuery
 *   4. ★ 隐式 ARIA Role 推导完善（借鉴 Skyvern INTERACTIVE_ROLES 集合）
 *   5. ★ 父子去重策略升级：propagating 容器（a/button/label）让位给内部真实子节点
 *   6. ★ 输出格式标准化：`[ID: N] Role: button, Name: "提交", State: disabled`
 *   7. ★ 健壮性加固：每个元素处理包裹 try-catch，单元素异常不中断全局
 */

(startIndex = 1) => {
    // ═══════════════════════════════════════════════════════════
    //  0. 清理上轮标记（继承 v5）
    // ═══════════════════════════════════════════════════════════
    if (typeof window.__removeSomMarks === 'function') window.__removeSomMarks();

    const _overlays = [];

    window.__clearSomOverlays = () => {
        _overlays.forEach(el => { try { el?.parentNode?.removeChild(el); } catch (_) {} });
        _overlays.length = 0;
    };

    window.__removeSomMarks = () => {
        window.__clearSomOverlays();
        const clearAttrs = el => {
            el.removeAttribute('data-som-id');
            el.removeAttribute('data-som-url');
        };
        document.querySelectorAll('[data-som-id],[data-som-url]').forEach(clearAttrs);
        try {
            document.querySelectorAll('iframe').forEach(iframe => {
                try {
                    const d = iframe.contentDocument || iframe.contentWindow?.document;
                    if (d) d.querySelectorAll('[data-som-id],[data-som-url]').forEach(clearAttrs);
                } catch (_) {}
            });
        } catch (_) {}
    };

    // ═══════════════════════════════════════════════════════════
    //  1. ★ 五层可见性引擎（借鉴 browser-use 严密过滤）
    //
    //  Layer 1: CSS 样式检查 (display/visibility/opacity)
    //  Layer 2: 最小尺寸检查 (width/height >= 5px)
    //  Layer 3: 视口边界检查 (上下左右是否在可见区域)
    //  Layer 4: pointer-events 检查 (不可交互的元素跳过)
    //  Layer 5: elementFromPoint 遮挡检测 (L2/L3 严格多点采样)
    // ═══════════════════════════════════════════════════════════

    /** 获取计算样式（缓存优化） */
    const _styleCache = new WeakMap();
    function cachedStyle(el) {
        if (_styleCache.has(el)) return _styleCache.get(el);
        try {
            const s = window.getComputedStyle(el);
            _styleCache.set(el, s);
            return s;
        } catch (_) { return null; }
    }

    function _classNameOf(el) {
        try {
            const cls = el?.className || '';
            return (cls.baseVal !== undefined ? cls.baseVal : cls).toString();
        } catch (_) { return ''; }
    }

    function _directText(el) {
        try {
            return Array.from(el.childNodes || [])
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => (n.textContent || '').trim())
                .join('')
                .trim();
        } catch (_) { return ''; }
    }

    function _hasIconChild(el) {
        try {
            return !!el?.querySelector?.('svg, img, i[class], [class*="icon" i]');
        } catch (_) { return false; }
    }

    function _isCompactIconShape(el) {
        try {
            const r = el.getBoundingClientRect();
            return (
                r.width >= 20 && r.width <= 90 &&
                r.height >= 20 && r.height <= 90 &&
                Math.abs(r.width - r.height) <= 18
            );
        } catch (_) { return false; }
    }

    function _explicitIconName(el) {
        try {
            const innerIcon = el.querySelector?.('svg, img, i[class], [class*="icon" i]');
            const svgTitle = el.querySelector?.('svg > title, svg > desc');
            const raw = [
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('alt') || '',
                el.getAttribute('data-testid') || '',
                el.getAttribute('data-test') || '',
                el.id || '',
                _classNameOf(el),
                innerIcon?.getAttribute?.('aria-label') || '',
                innerIcon?.getAttribute?.('title') || '',
                innerIcon?.getAttribute?.('alt') || '',
                innerIcon?.getAttribute?.('data-testid') || '',
                innerIcon?.getAttribute?.('data-test') || '',
                innerIcon?.id || '',
                _classNameOf(innerIcon),
                svgTitle ? (svgTitle.textContent || '') : '',
            ].join(' ').toLowerCase();
            if (/(send|submit|paper[-_ ]?plane|airplane|arrow[-_ ]?up|arrow[-_ ]?right|发送|提交)/i.test(raw)) return 'send';
            if (/(mic|microphone|voice|语音|麦克风)/i.test(raw)) return 'mic';
            if (/(plus|add|attach|upload|attachment|附件|上传|添加)/i.test(raw)) return 'plus';
            if (/(code|terminal|slash|prompt|coding|码|代码)/i.test(raw)) return 'code';
            if (/(search|magnify|搜索)/i.test(raw)) return 'search';
            if (/(copy|clipboard|duplicate|copy[-_ ]?text)/i.test(raw)) return 'copy';
            if (/(retry|refresh|regenerate|reload|again)/i.test(raw)) return 'refresh';
            if (/(speaker|volume|audio|listen|read[-_ ]?aloud|voice[-_ ]?play)/i.test(raw)) return 'audio';
            if (/(bookmark|favorite|favourite|collect|star|save)/i.test(raw)) return 'favorite';
            if (/(share|forward|send[-_ ]?to)/i.test(raw)) return 'share';
            if (/(more|ellipsis|kebab|menu|dots?)/i.test(raw)) return 'more';
            if (/(thumbs?[-_ ]?up|like|praise|good|upvote)/i.test(raw)) return 'like';
            if (/(thumbs?[-_ ]?down|dislike|bad|downvote)/i.test(raw)) return 'dislike';
        } catch (_) {}
        return '';
    }

    function _visibleEditableRects() {
        try {
            const nodes = Array.from(document.querySelectorAll(
                'textarea, input:not([type="hidden"]), [contenteditable="true"], [role="textbox"]'
            ));
            return nodes
                .filter(el => {
                    try { return isVisible(el, { top: 0, left: 0 }); } catch (_) { return false; }
                })
                .map(el => ({ el, r: el.getBoundingClientRect() }))
                .filter(x => x.r.width >= 120 && x.r.height >= 24);
        } catch (_) { return []; }
    }

    function _isComposerIconCandidate(el) {
        if (!_hasIconChild(el) || !_isCompactIconShape(el)) return false;
        if (_directText(el).length > 4) return false;
        try {
            const r = el.getBoundingClientRect();
            for (const item of _visibleEditableRects()) {
                const er = item.r;
                const horizontallyInside = r.left >= er.left - 24 && r.right <= er.right + 24;
                const verticallyInside = r.top >= er.top - 24 && r.bottom <= er.bottom + 24;
                const inLowerHalf = r.top >= er.top + er.height * 0.45;
                const nearBottomBand = r.bottom >= er.bottom - 96 && r.top <= er.bottom + 24;
                if (horizontallyInside && verticallyInside && (inLowerHalf || nearBottomBand)) {
                    return true;
                }
            }
        } catch (_) {}
        return false;
    }

    function _isIconToolbarCandidate(el) {
        if (!_hasIconChild(el) || !_isCompactIconShape(el)) return false;
        if (_directText(el).length > 4) return false;
        try {
            const r = el.getBoundingClientRect();
            const cy = r.top + r.height / 2;
            let root = el.parentElement;
            for (let depth = 0; root && depth < 4; depth++, root = root.parentElement) {
                const rr = root.getBoundingClientRect?.();
                if (!rr || rr.width <= 0 || rr.height <= 0) continue;
                if (rr.height > 140 || rr.width * rr.height > 160000) continue;
                const rowMates = Array.from(root.querySelectorAll('*')).filter(node => {
                    if (node === el || node.contains?.(el) || el.contains?.(node)) return false;
                    if (!_hasIconChild(node) || !_isCompactIconShape(node)) return false;
                    if (_directText(node).length > 4) return false;
                    const nr = node.getBoundingClientRect();
                    const ncy = nr.top + nr.height / 2;
                    return Math.abs(ncy - cy) <= 18 && Math.abs(nr.left - r.left) <= 360;
                });
                if (rowMates.length >= 2) return true;
            }
        } catch (_) {}
        return false;
    }

    function _isRightmostComposerIcon(el) {
        if (!_isComposerIconCandidate(el)) return false;
        try {
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            for (const item of _visibleEditableRects()) {
                const er = item.r;
                const inSameEditor = (
                    r.left >= er.left - 24 && r.right <= er.right + 24 &&
                    r.top >= er.top - 24 && r.bottom <= er.bottom + 24
                );
                if (!inSameEditor) continue;
                const icons = Array.from(document.querySelectorAll('*'))
                    .filter(node => node !== el && _isComposerIconCandidate(node))
                    .map(node => {
                        const nr = node.getBoundingClientRect();
                        return { r: nr, cx: nr.left + nr.width / 2 };
                    })
                    .filter(x =>
                        x.r.left >= er.left - 24 && x.r.right <= er.right + 24 &&
                        x.r.top >= er.top - 24 && x.r.bottom <= er.bottom + 24
                    );
                const maxCx = Math.max(cx, ...icons.map(x => x.cx));
                return cx >= maxCx - 3 && cx >= er.left + er.width * 0.72;
            }
        } catch (_) {}
        return false;
    }

    /**
     * Layer 1-4 综合可见性检查
     * 借鉴 browser-use 的 _is_element_visible() + Skyvern 的 isElementVisible()
     */
    function isVisible(el, off) {
        if (!el?.getBoundingClientRect) return false;

        // ── Layer 1: CSS 样式 ──
        const s = cachedStyle(el);
        if (!s) return false;
        // browser-use: display === 'none' → 不可见
        if (s.display === 'none') return false;
        // Skyvern: display === 'contents' → 自身不渲染，但子元素可能可见
        // 对于交互检测我们忽略这类容器
        if (s.display === 'contents') return false;
        // browser-use: visibility === 'hidden' → 不可见
        if (s.visibility === 'hidden') return false;
        // browser-use: opacity <= 0 → 不可见（容错 parseFloat）
        if (parseFloat(s.opacity || '1') <= 0) return false;
        // Skyvern: clip-path / clip 隐藏（select2-offscreen 等）
        if (s.clip === 'rect(0px, 0px, 0px, 0px)' || s.clipPath === 'inset(100%)') return false;

        // ── Layer 2: 最小尺寸 ──
        const r = el.getBoundingClientRect();
        // browser-use: width/height < 5px → 不可见
        if (r.width < 5 || r.height < 5) return false;

        // ── Layer 3: 视口边界 ──
        // browser-use: 元素完全在视口外 → 不可见（上下各留 threshold 容差）
        const vw = window.innerWidth || document.documentElement.clientWidth || 0;
        const vh = window.innerHeight || document.documentElement.clientHeight || 0;
        const absT = r.top + (off?.top || 0);
        const absL = r.left + (off?.left || 0);
        if (absT + r.height < -50 || absT > vh + 50) return false;  // 上下 50px 容差
        if (absL + r.width < -50 || absL > vw + 50) return false;

        // ── Layer 4: pointer-events ──
        // 仅当元素不是 L1（表单控件）时才检查 pointer-events:none
        // L1 元素即使 pointer-events:none 也应标记（JS 可能动态解锁）
        if (s.pointerEvents === 'none') {
            const tag = el.tagName.toUpperCase();
            if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) && !el.isContentEditable) {
                return false;
            }
        }

        return true;
    }

    /**
     * ★ 全屏遮罩层/Loading 图层检测
     *
     * 判断 elementFromPoint 命中的元素是否为阻断性遮罩层：
     *   - position: fixed / absolute
     *   - 覆盖视口面积 > 70%
     * 典型命中：全屏 Loading Spinner、Modal Backdrop、cookie-consent 遮罩
     */
    function _isBlockingOverlay(hitEl) {
        try {
            const s = cachedStyle(hitEl);
            if (!s) return false;
            const pos = s.position;
            if (pos !== 'fixed' && pos !== 'absolute') return false;
            const r = hitEl.getBoundingClientRect();
            const vw = window.innerWidth || 1;
            const vh = window.innerHeight || 1;
            // 覆盖视口 70% 以上判定为遮罩层
            if (r.width > vw * 0.7 && r.height > vh * 0.7) return true;
            return false;
        } catch (_) { return false; }
    }

    /**
     * Layer 5: elementFromPoint 物理探针遮挡检测（增强版）
     *
     * 借鉴 browser-use paint-order 思路 + 新增遮罩层识别：
     *   - L1 元素：中心点检测 + 宽松判定（命中自身/后代/祖先链都通过）
     *   - L2/L3 元素：三点采样 + 严格判定（必须命中自身或后代）
     *   - 所有级别：如果 hit 元素被判定为「阻断性遮罩层」，直接判定遮挡
     */
    function isNotOccluded(el, doc, isL1) {
        try {
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;

            // ── 中心点在视口外，直接按遮挡处理 ──
            const vw = window.innerWidth || 0;
            const vh = window.innerHeight || 0;
            if (cx < 0 || cx > vw || cy < 0 || cy > vh) return false;

            if (isL1) {
                // L1 宽松检测：命中自身、后代、祖先链都通过
                const hit = doc.elementFromPoint(cx, cy);
                if (!hit) return true; // 异常宽松
                if (el === hit || el.contains(hit)) return true;
                // ★ 遮罩层检测：即使 L1 也不能穿透全屏遮罩
                if (_isBlockingOverlay(hit)) return false;
                // 检查 hit 是否是 el 的祖先（子元素遮挡父级的正常情况）
                if (hit.contains(el)) return true;
                return false;
            }

            // L2/L3 三点严格检测
            const pts = [
                [cx, cy],
                [r.left + 4, r.top + 4],
                [r.right - 4, r.bottom - 4],
            ];
            for (const [x, y] of pts) {
                const hit = doc.elementFromPoint(x, y);
                if (!hit) continue;
                if (el === hit || el.contains(hit)) return true;
                // ★ 遮罩层检测：探针扎到遮罩层则该采样点判负
                // （不直接 return false，其他采样点可能穿透遮罩边缘）
            }
            return false;
        } catch (_) {
            return true; // 异常宽松处理
        }
    }

    /**
     * ★ 终极防线：渲染阶段最终遮挡检测（补丁一核心）
     *
     * 在绘制 SoM 标记前做最后一次物理探针检查。
     * 与 isNotOccluded 的区别：
     *   1. 必须跳过我们自己绘制的 SoM overlay 容器，否则已绘制的红框会遮挡后续元素
     *   2. 对所有 Level 统一使用中心点检测（此时已过 8 层过滤，无需三点采样）
     *   3. 加入遮罩层识别
     *
     * @param {Element} el - 待检测元素
     * @param {Document} doc - 所属 document
     * @param {Element|null} somContainer - SoM overlay 容器（用于排除自身干扰）
     * @returns {boolean} true = 被遮挡，应跳过
     */
    function isOccludedFinal(el, doc, somContainer) {
        try {
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            // 中心点在视口外 → 遮挡
            if (cx < 0 || cx > (window.innerWidth || 0) ||
                cy < 0 || cy > (window.innerHeight || 0)) {
                return true;
            }
            const hit = doc.elementFromPoint(cx, cy);
            if (!hit) return false;
            // 跳过我们自己的 SoM overlay 元素
            if (somContainer && somContainer.contains(hit)) return false;
            // 命中自身或后代 → 未遮挡
            if (el.contains(hit)) return false;
            // 命中祖先 → 未遮挡（子元素覆盖父元素属正常情况）
            if (hit.contains(el)) return false;
            // ★ 被其他图层物理遮挡
            return true;
        } catch (_) {
            return false; // 异常时宽松放行
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  2. ★ 隐式 ARIA Role 推导（融合 Skyvern INTERACTIVE_ROLES）
    // ═══════════════════════════════════════════════════════════

    /**
     * 推导元素的语义角色（ARIA Role）
     * 优先级：显式 role 属性 → 标签隐式映射
     * 参考 Skyvern 的 INTERACTIVE_ROLES + WAI-ARIA implicit role 映射
     */
    function deriveRole(el) {
        try {
            const explicit = (el.getAttribute('role') || '').trim().toLowerCase();
            if (explicit) return explicit;
            const tag = el.tagName.toLowerCase();
            if (tag === 'a' && el.hasAttribute('href')) return 'link';
            if (tag === 'button' || tag === 'summary') return 'button';
            if (tag === 'input') {
                const t = (el.getAttribute('type') || 'text').toLowerCase();
                if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
                if (t === 'checkbox') return 'checkbox';
                if (t === 'radio') return 'radio';
                if (t === 'search') return 'searchbox';
                if (t === 'range') return 'slider';
                if (t === 'number') return 'spinbutton';
                if (t === 'email' || t === 'tel' || t === 'url') return 'textbox';
                return 'textbox';
            }
            if (tag === 'textarea') return 'textbox';
            if (tag === 'select') return 'combobox';
            if (tag === 'option') return 'option';
            if (tag === 'label') return 'label';
            if (tag === 'img') return 'img';
            if (tag === 'nav') return 'navigation';
            if (tag === 'header') return 'banner';
            if (tag === 'footer') return 'contentinfo';
            if (tag === 'main') return 'main';
            if (tag === 'aside') return 'complementary';
            if (tag === 'section') return 'region';
            if (el.isContentEditable) return 'textbox';

            // ═══════════════════════════════════════════════════
            // ★ 补丁二：动态语义提权 (Dynamic Role Promotion)
            //
            // 问题：现代前端大量滥用 <div>/<span> 绑定 @click 作为
            //       按钮，大模型会因 Role 为 div/span 而不敢点击。
            // 策略：当非交互标签拥有明确的点击事件绑定时，
            //       强制将其 Role 提升为 button，告诉 VLM 这是
            //       一个确切的可点击按钮。
            // ═══════════════════════════════════════════════════
            const _NON_SEMANTIC_TAGS = new Set([
                'div', 'span', 'p', 'li', 'td', 'i', 'em', 'strong',
                'section', 'article', 'dd', 'dt', 'figure',
            ]);
            if (_NON_SEMANTIC_TAGS.has(tag)) {
                const _hasClickBinding =
                    el.hasAttribute('onclick') ||
                    el.hasAttribute('jsaction') ||
                    el.hasAttribute('ng-click') ||
                    el.hasAttribute('data-ng-click') ||
                    el.hasAttribute('(click)') ||
                    el.hasAttribute('v-on:click') ||
                    el.hasAttribute('@click');
                if (_hasClickBinding) return 'button';
                // tabindex=0 + cursor:pointer 也是强交互信号
                if (el.getAttribute('tabindex') === '0') {
                    try {
                        if (cachedStyle(el)?.cursor === 'pointer') return 'button';
                    } catch (_) {}
                }
            }

            return tag;
        } catch (_) { return '?'; }
    }

    /**
     * 推导元素的可访问名称
     * 优先级：aria-labelledby → aria-label → title → placeholder → innerText → alt
     * 参考 W3C Accessible Name Computation 规范
     */
    function deriveName(el) {
        try {
            // aria-labelledby：多个 ID 空格分隔
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                const parts = labelledBy.split(/\s+/)
                    .map(id => document.getElementById(id))
                    .filter(Boolean)
                    .map(n => (n.textContent || '').trim());
                const joined = parts.join(' ').trim();
                if (joined) return joined.slice(0, 80);
            }
            const ariaLabel = el.getAttribute('aria-label');
            if (ariaLabel?.trim()) return ariaLabel.trim().slice(0, 80);
            const title = el.getAttribute('title');
            if (title?.trim()) return title.trim().slice(0, 80);
            const ph = el.getAttribute('placeholder') || el.getAttribute('data-placeholder');
            if (ph?.trim()) return ph.trim().slice(0, 80);
            // input/textarea：读 value（非 password）
            const tag = el.tagName.toUpperCase();
            if ((tag === 'INPUT' || tag === 'TEXTAREA') && el.type !== 'password') {
                const val = (el.value || '').trim();
                if (val) return val.slice(0, 60);
            }
            // contenteditable：读 textContent
            if (el.isContentEditable) {
                const text = (el.textContent || '').trim();
                if (text) return text.slice(0, 60);
            }
            // 通用文本
            const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            if (txt) return txt.slice(0, 80);
            const alt = el.getAttribute('alt');
            if (alt?.trim()) return alt.trim().slice(0, 80);

            // ★ 图标按钮兜底命名（解决 yiyan/豆包/ChatGPT 等 chat UI 的
            //   纯图标 div 按钮无法被 SoM 标号的问题）：依次尝试
            //   (a) 子 SVG 的 <title>/<desc>
            //   (b) 子 SVG/img 自己的 aria-label / title
            //   (c) icon class 命名（如 class="icon-send" → "send"）
            //   (d) 通用占位符 [icon]
            try {
                const svgTitle = el.querySelector('svg > title, svg > desc');
                if (svgTitle) {
                    const t = (svgTitle.textContent || '').trim();
                    if (t) return t.slice(0, 80);
                }
                const innerIcon = el.querySelector('svg, img, i[class], [class*="icon" i]');
                if (innerIcon) {
                    const il = (innerIcon.getAttribute('aria-label') || '').trim();
                    if (il) return il.slice(0, 80);
                    const it = (innerIcon.getAttribute('title') || '').trim();
                    if (it) return it.slice(0, 80);
                    const ialt = (innerIcon.getAttribute('alt') || '').trim();
                    if (ialt) return ialt.slice(0, 80);
                    const explicit = _explicitIconName(el);
                    if (explicit) return '[' + explicit + ']';
                    if (_isRightmostComposerIcon(el)) return '[send]';
                    // 从 class 命名提取语义（icon-send / icon_plus / sendIcon）
                    const cls = _classNameOf(innerIcon);
                    const m = cls.match(/(?:icon[_-]|[_-]icon\b|^icon)([a-z][a-z0-9_-]{1,20})/i)
                          || cls.match(/(send|submit|mic|voice|attach|upload|plus|add|search|menu|more|close|delete|edit|save)\b/i);
                    if (m && m[1]) {
                        return '[' + m[1].replace(/[_-]+/g, ' ').toLowerCase().slice(0, 30) + ']';
                    }
                    // 真的没线索 → 通用占位符，让 VLM 至少能看见这是个图标按钮
                    return '[icon]';
                }
            } catch (_) {}

            return '';
        } catch (_) { return ''; }
    }

    // ═══════════════════════════════════════════════════════════
    //  3. ★ 富语义状态提取（借鉴 Skyvern 的状态全量采集）
    //
    //  Skyvern 原始采集列表：
    //    required, checked, selected, readonly, disabled,
    //    aria-required, aria-checked, aria-selected, aria-readonly,
    //    aria-disabled, aria-expanded, aria-current
    // ═══════════════════════════════════════════════════════════

    /**
     * 提取元素的完整交互状态
     * @returns {Object} 包含 disabled/checked/expanded/required/readonly/selected 的状态对象
     */
    function extractState(el) {
        try {
            return {
                // Skyvern 的 DOM 属性优先级高于 HTML attribute（DOM property 反映实时状态）
                disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
                checked: el.checked === true || el.getAttribute('aria-checked') === 'true',
                selected: el.selected === true || el.getAttribute('aria-selected') === 'true',
                expanded: el.getAttribute('aria-expanded'),  // 'true' / 'false' / null
                required: el.required === true || el.getAttribute('aria-required') === 'true',
                readonly: el.readOnly === true || el.getAttribute('aria-readonly') === 'true',
                // Skyvern 特有：aria-current（导航高亮）
                current: el.getAttribute('aria-current') || '',
            };
        } catch (_) {
            return {};
        }
    }

    /**
     * 将状态对象序列化为简洁字符串
     * 输出例：'disabled, checked, expanded=true'
     * 无状态时返回空字符串
     */
    function serializeState(state) {
        const parts = [];
        if (state.disabled) parts.push('disabled');
        if (state.checked) parts.push('checked');
        if (state.selected) parts.push('selected');
        if (state.expanded === 'true' || state.expanded === 'false') {
            parts.push(`expanded=${state.expanded}`);
        }
        if (state.required) parts.push('required');
        if (state.readonly) parts.push('readonly');
        if (state.current && state.current !== 'false') {
            parts.push(`current=${state.current}`);
        }
        return parts.join(', ');
    }

    // ═══════════════════════════════════════════════════════════
    //  4. ★ 多源交互判定引擎（融合 Skyvern 12 层启发式）
    //
    //  Skyvern 的交互判定层次：
    //    1. ARIA widget role → 2. 原生表单标签 → 3. <a href> →
    //    4. onclick/jsaction 属性 → 5. contentEditable →
    //    6. Angular 事件 → 7. tabindex → 8. cursor:pointer →
    //    9. jQuery 事件
    //
    //  VSpider 三级分层（L1 > L2 > L3）继承 v5，各层内融入 Skyvern 检测逻辑
    // ═══════════════════════════════════════════════════════════

    // Skyvern 的 INTERACTIVE_ROLES（widget 角色集合）
    const WIDGET_ROLES = new Set([
        'button', 'checkbox', 'combobox', 'link', 'listbox',
        'menuitem', 'menuitemcheckbox', 'menuitemradio',
        'option', 'radio', 'searchbox', 'slider', 'spinbutton',
        'switch', 'tab', 'textbox', 'treeitem',
    ]);

    // L1：最高优先级输入控件（绝不被遮挡/面积过滤淘汰）
    const L1_ROLES = new Set([
        'textbox', 'searchbox', 'spinbutton', 'combobox',
    ]);

    // L2：按钮/链接/交互角色（受面积上限过滤）
    const L2_ROLES = new Set([
        'button', 'checkbox', 'radio', 'tab', 'link',
        'menuitem', 'menuitemcheckbox', 'menuitemradio',
        'option', 'switch', 'listbox', 'slider', 'treeitem',
        'gridcell', 'row',
    ]);

    /**
     * 判定元素的交互等级
     * @returns {number} 0=不标记, 1=输入控件, 2=按钮/链接, 3=cursor:pointer 候选
     */
    function interactLevel(el) {
        try {
            const tag = el.tagName.toUpperCase();

            // ── 跳过不可能交互的标签（Skyvern: isScriptOrStyle） ──
            if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'META', 'LINK', 'HEAD', 'BR', 'HR',
                 'SVG', 'PATH', 'CIRCLE', 'RECT', 'LINE', 'POLYGON', 'POLYLINE',
                 'ELLIPSE', 'DEFS', 'CLIPPATH', 'USE', 'G', 'SYMBOL',
                ].includes(tag)) return 0;

            // ── L1a: 原生表单控件 ──
            if (tag === 'INPUT') {
                const t = (el.getAttribute('type') || 'text').toLowerCase();
                if (t === 'hidden') return 0;  // hidden input 不可交互
                return 1;
            }
            if (tag === 'TEXTAREA' || tag === 'SELECT') return 1;

            // ── L1b: contentEditable（Skyvern: el.isContentEditable） ──
            if (el.isContentEditable) return 1;

            // ── L1c/L2a: ARIA role 分层 ──
            const role = (el.getAttribute('role') || '').trim().toLowerCase();
            if (L1_ROLES.has(role)) return 1;
            if (L2_ROLES.has(role)) {
                // 面积上限：防止巨大容器误判为交互元素
                const r = el.getBoundingClientRect();
                return (r.width * r.height < 200000) ? 2 : 0;
            }

            // ── L2b: BUTTON 标签 ──
            if (tag === 'BUTTON' || tag === 'SUMMARY') {
                const r = el.getBoundingClientRect();
                return (r.width * r.height < 200000) ? 2 : 0;
            }

            // ── L2c: 有效 <a href> ──
            if (tag === 'A') {
                const href = el.getAttribute('href') || '';
                if (href && !/^(#|javascript:\s*void|javascript:\s*;?\s*$)/i.test(href)) return 2;
                // 即使 href 无效，如果有 onclick 或 role，也算交互
                if (el.hasAttribute('onclick') || el.hasAttribute('jsaction')) return 2;
            }

            // ── L2d: LABEL（关联表单控件） ──
            if (tag === 'LABEL') {
                const forId = el.getAttribute('for');
                if (forId || el.querySelector('input, textarea, select')) return 2;
            }

            // ── L2e: onclick / jsaction 属性（Skyvern 检测） ──
            if (el.hasAttribute('onclick') || el.hasAttribute('jsaction')) {
                const r = el.getBoundingClientRect();
                if (r.width * r.height < 200000) return 2;
            }

            // ── L2f: Angular 事件绑定（Skyvern: hasAngularClickBinding） ──
            if (el.hasAttribute('ng-click') || el.hasAttribute('data-ng-click') ||
                el.hasAttribute('(click)') || el.hasAttribute('v-on:click') ||
                el.hasAttribute('@click')) {
                const r = el.getBoundingClientRect();
                if (r.width * r.height < 200000) return 2;
            }

            // ── L2g: tabindex >= 0 且有内容 ──
            const tabIdx = el.getAttribute('tabindex');
            if (tabIdx !== null && tabIdx !== '-1') {
                const r = el.getBoundingClientRect();
                const area = r.width * r.height;
                if (area > 0 && area < 200000 && deriveName(el).length > 0) return 2;
            }

            // ── L3: cursor:pointer 小元素（最后手段，Skyvern + browser-use 共有） ──
            const r = el.getBoundingClientRect();
            const area = r.width * r.height;
            if (area > 50000 || area < 25) return 0;  // 太大或太小都排除
            try {
                const s = cachedStyle(el);
                const composerIconCandidate = _isComposerIconCandidate(el);
                const toolbarIconCandidate = _isIconToolbarCandidate(el);
                const explicitIconName = _explicitIconName(el);
                if (s && (s.cursor === 'pointer' || composerIconCandidate || toolbarIconCandidate || explicitIconName)) {
                    const name = deriveName(el);
                    // ★ 图标按钮（无文字、含 SVG/img/icon 子元素）：当尺寸像
                    //   独立按钮（20-90px 接近正方形）时也接受。这条修复了
                    //   yiyan/豆包/ChatGPT 等 chat UI 的 + / mic / 飞机
                    //   send 等纯图标 div 按钮被 SoM 漏标的问题。
                    const hasIconChild = _hasIconChild(el);
                    const isCompactIconShape = _isCompactIconShape(el);
                    if (name.length < 1) {
                        if (!(hasIconChild && isCompactIconShape && (composerIconCandidate || toolbarIconCandidate || explicitIconName || s.cursor === 'pointer'))) return 0;
                        // fall through：让 deriveName 兜底名称生效
                        return 3;
                    }
                    // 排除只有长文本但无直接文字节点的包装容器
                    const directTxt = Array.from(el.childNodes)
                        .filter(n => n.nodeType === Node.TEXT_NODE)
                        .map(n => n.textContent.trim()).join('');
                    if (!directTxt && name.length > 25) return 0;
                    return 3;
                }
            } catch (_) {}

            return 0;
        } catch (_) {
            return 0;
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  5. 属性提取与输出格式化
    // ═══════════════════════════════════════════════════════════

    /**
     * 提取元素的详细属性描述（供 inputDesc 字段）
     * 借鉴 Skyvern 的 RESERVED_ATTRIBUTES 白名单：
     *   只输出对 LLM 有价值的属性，压缩无用 token
     */
    function getInputDesc(el) {
        try {
            const tag = el.tagName.toUpperCase();

            // <a> 标签：暴露 href
            if (tag === 'A') {
                const raw = (el.getAttribute('href') || '').trim();
                const full = (el.href || '').trim();
                if (!raw || /^(#|javascript:|mailto:|tel:)/i.test(raw)) return '';
                const href = (full && !full.endsWith('#')) ? full : raw;
                return `href="${href.slice(0, 150)}"`;
            }

            // contentEditable 元素
            if (el.isContentEditable) {
                const ph = el.getAttribute('placeholder')
                    || el.getAttribute('data-placeholder')
                    || el.getAttribute('aria-placeholder')
                    || '';
                const parts = ['type=text', 'contenteditable=true'];
                if (ph) parts.push(`placeholder="${ph}"`);
                const val = (el.textContent || '').trim();
                if (val) parts.push(`value="${val.slice(0, 50)}"`);
                return parts.join(', ');
            }

            // INPUT / TEXTAREA
            if (tag === 'INPUT' || tag === 'TEXTAREA') {
                const type = (el.getAttribute('type') || 'text').toLowerCase();
                const parts = [`type=${type}`];
                const ph = el.getAttribute('placeholder');
                const name = el.getAttribute('name');
                if (ph) parts.push(`placeholder="${ph}"`);
                if (name) parts.push(`name=${name}`);
                // 状态类属性（Skyvern 保留列表中的关键字段）
                if (type === 'checkbox' || type === 'radio') {
                    parts.push(`checked=${el.checked}`);
                } else if (type !== 'password' && type !== 'hidden') {
                    const val = (el.value || '').trim();
                    if (val) parts.push(`value="${val.slice(0, 50)}"`);
                }
                if (el.maxLength > 0 && el.maxLength < 524288) {
                    parts.push(`maxlength=${el.maxLength}`);
                }
                if (el.pattern) parts.push(`pattern="${el.pattern}"`);
                return parts.join(', ');
            }

            // SELECT
            if (tag === 'SELECT') {
                const selected = el.options?.[el.selectedIndex];
                const parts = ['type=select'];
                if (selected) parts.push(`selected="${(selected.text || '').trim().slice(0, 40)}"`);
                parts.push(`options=${el.options?.length || 0}`);
                return parts.join(', ');
            }

            // IMG
            if (tag === 'IMG') {
                const alt = el.getAttribute('alt') || '';
                const src = (el.getAttribute('src') || '').slice(0, 80);
                const parts = [];
                if (alt) parts.push(`alt="${alt.slice(0, 60)}"`);
                if (src) parts.push(`src="${src}"`);
                return parts.join(', ');
            }

            return '';
        } catch (_) { return ''; }
    }

    /** 获取元素父容器的语境文本（列表页排名/评分等上下文） */
    function getParentContext(el) {
        try {
            const container = el.closest(
                'li, tr, article, .item, .card, .list-item, .entry, .result, [role="listitem"]'
            );
            const parent = container || el.parentElement;
            if (!parent || parent === document.body || parent === document.documentElement) return '';
            const raw = (parent.innerText || parent.textContent || '').replace(/\s+/g, ' ').trim();
            const selfText = (el.innerText || el.textContent || '').trim();
            if (raw.length <= selfText.length + 10) return '';
            return raw.slice(0, 150);
        } catch (_) { return ''; }
    }

    // ═══════════════════════════════════════════════════════════
    //  6. 候选元素收集（含 Shadow DOM 递归穿透）
    // ═══════════════════════════════════════════════════════════

    function collectFromRoot(root, doc, off, results) {
        try {
            const allElements = root.querySelectorAll('*');
            for (let i = 0; i < allElements.length; i++) {
                const el = allElements[i];
                try {
                    // Shadow DOM 递归穿透
                    if (el.shadowRoot) {
                        collectFromRoot(el.shadowRoot, doc, off, results);
                    }

                    const level = interactLevel(el);
                    if (level === 0) continue;
                    if (!isVisible(el, off)) continue;

                    results.push({ el, doc, off, level });
                } catch (elErr) {
                    // ★ 单元素异常不中断全局（健壮性要求）
                    // console.warn('[SoM v6] 单元素处理异常:', elErr);
                }
            }
        } catch (e) {
            console.warn('[SoM v6] 收集异常:', e);
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  7. 主收集逻辑
    // ═══════════════════════════════════════════════════════════

    let allCandidates = [];

    // 7.1 主页面（含 Shadow DOM）
    collectFromRoot(document, document, { top: 0, left: 0 }, allCandidates);

    // 7.2 同源 iframe
    try {
        const iframes = document.querySelectorAll('iframe');
        for (let idx = 0; idx < iframes.length; idx++) {
            try {
                const iframe = iframes[idx];
                const iDoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (!iDoc) continue;
                const ir = iframe.getBoundingClientRect();
                if (ir.width < 10 || ir.height < 10) continue;
                const off = { top: ir.top, left: ir.left };
                const sub = [];
                collectFromRoot(iDoc, iDoc, off, sub);
                sub.forEach(c => { c.frameIndex = idx; c.frameSrc = iframe.src || ''; });
                allCandidates = allCandidates.concat(sub);
            } catch (_) {}
        }
    } catch (_) {}

    // ═══════════════════════════════════════════════════════════
    //  8. ★ 多层过滤管道（升级版）
    //
    //  8.1 elementFromPoint 遮挡检测
    //  8.2 ★ 父子去重（升级：propagating 容器让位给内部子节点）
    //  8.3 跨 document 几何包含过滤
    //  8.4 文本去重
    //  8.5 空内容兜底
    //  8.6 排序
    // ═══════════════════════════════════════════════════════════

    let filtered = allCandidates;

    // ── 8.1 elementFromPoint 遮挡检测 ──
    filtered = filtered.filter(c => {
        if (c.doc !== document) return true;  // iframe 内元素跳过主页面遮挡检测
        return isNotOccluded(c.el, document, c.level === 1);
    });

    // ── 8.2 ★ 父子去重（升级版：借鉴 browser-use propagating 策略） ──
    // 核心思想：如果一个 <a>/<button>/<label> 内部包含了更具体的交互子元素，
    // 则父容器让位（不打 SoM ID），只标记内部真实交互主体。
    // 但 L1 元素（表单控件）永远保留，不参与让位。
    {
        const byDoc = new Map();
        for (const c of filtered) {
            if (!byDoc.has(c.doc)) byDoc.set(c.doc, []);
            byDoc.get(c.doc).push(c);
        }
        const next = [];
        for (const [, group] of byDoc) {
            const elSet = new Set(group.map(c => c.el));
            for (const c of group) {
                // L1 永远保留
                if (c.level === 1) { next.push(c); continue; }
                // 检查是否包含标记过的子元素
                let hasMarkedChild = false;
                for (const other of elSet) {
                    if (other !== c.el && c.el.contains(other)) {
                        hasMarkedChild = true;
                        break;
                    }
                }
                if (!hasMarkedChild) next.push(c);
            }
        }
        filtered = next;
    }

    // ── 8.3 跨 document 几何包含过滤 ──
    // 大容器完全包裹小元素且面积 > 1.5 倍时，移除大容器（保留小元素）
    {
        const rects = filtered.map(c => {
            const r = c.el.getBoundingClientRect();
            const o = c.off || { top: 0, left: 0 };
            return {
                t: r.top + o.top,
                l: r.left + o.left,
                b: r.bottom + o.top,
                r: r.right + o.left,
                area: r.width * r.height,
            };
        });
        const remove = new Set();
        for (let i = 0; i < filtered.length; i++) {
            if (remove.has(i) || filtered[i].level === 1) continue;
            const a = rects[i];
            for (let j = 0; j < filtered.length; j++) {
                if (i === j || remove.has(j)) continue;
                const b = rects[j];
                if (a.t <= b.t + 2 && a.b >= b.b - 2 &&
                    a.l <= b.l + 2 && a.r >= b.r - 2 &&
                    a.area > b.area * 1.5) {
                    remove.add(i);
                    break;
                }
            }
        }
        filtered = filtered.filter((_, i) => !remove.has(i));
    }

    // ── 8.4 文本去重（保留面积最小的元素） ──
    {
        const textMap = new Map();
        for (let i = 0; i < filtered.length; i++) {
            const text = deriveName(filtered[i].el);
            if (!text || text.length < 2) continue;
            if (text === '[icon]') continue;
            const r = filtered[i].el.getBoundingClientRect();
            const area = r.width * r.height;
            if (!textMap.has(text)) textMap.set(text, []);
            textMap.get(text).push({ i, area });
        }
        const remove = new Set();
        for (const [, entries] of textMap) {
            if (entries.length > 1) {
                entries.sort((a, b) => a.area - b.area);
                for (let k = 1; k < entries.length; k++) remove.add(entries[k].i);
            }
        }
        filtered = filtered.filter((_, i) => !remove.has(i));
    }

    // ── 8.5 空内容兜底 ──
    // 原生表单控件和 contentEditable 豁免空内容检查
    filtered = filtered.filter(c => {
        const tag = c.el.tagName.toUpperCase();
        if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'SUMMARY'].includes(tag)) return true;
        if (c.el.isContentEditable) return true;
        return deriveName(c.el).length > 0;
    });

    // ── 8.6 排序：从上到下、从左到右 ──
    filtered.sort((a, b) => {
        const ra = a.el.getBoundingClientRect();
        const rb = b.el.getBoundingClientRect();
        const ay = ra.top + (a.off?.top || 0);
        const by_ = rb.top + (b.off?.top || 0);
        const ax = ra.left + (a.off?.left || 0);
        const bx = rb.left + (b.off?.left || 0);
        return Math.abs(ay - by_) > 10 ? ay - by_ : ax - bx;
    });

    // ═══════════════════════════════════════════════════════════
    //  9. 动态 MAX_LABELS
    // ═══════════════════════════════════════════════════════════
    const l1Count = filtered.filter(c => c.level === 1).length;
    const MAX_LABELS = Math.min(150, 80 + l1Count);

    // ── 9.1 翻页链接保底：将翻页关键词元素提升到队列前部 ──
    // 防止 "More"/"Next"/"下一页" 等翻页入口被 MAX_LABELS 截断
    const PAGINATION_RE = /^(more|next|下一页|下页|next\s*page|load\s*more|›|»|▶|→)$/i;
    const paginationIndices = [];
    for (let i = 0; i < filtered.length; i++) {
        const c = filtered[i];
        const el = c.el;
        if (el.tagName === 'A' || el.tagName === 'BUTTON' || (el.getAttribute && el.getAttribute('role') === 'button')) {
            const txt = (el.textContent || '').trim();
            if (PAGINATION_RE.test(txt)) {
                paginationIndices.push(i);
            }
        }
    }
    // 将翻页元素移到队列末尾之前的安全位置（MAX_LABELS - 5 处）
    if (paginationIndices.length > 0) {
        const safeSlot = Math.max(0, MAX_LABELS - 5);
        for (const pi of paginationIndices) {
            if (pi >= MAX_LABELS) {
                // 翻页链接在截断区域外，需要移入
                const [item] = filtered.splice(pi, 1);
                filtered.splice(Math.min(safeSlot, filtered.length), 0, item);
            }
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  10. 绘制标记 + 生成标准化结果
    //
    //  输出格式对齐：
    //    resultMap[].id / tag / role / name / state / inputDesc / parentContext / rect
    //  供 Python 层组装为：
    //    [ID: 15] Role: button, Name: "提交表单", State: disabled
    // ═══════════════════════════════════════════════════════════

    const resultMap = [];
    let somId = startIndex;

    const container = document.createElement('div');
    container.id = '__som_overlay_container';
    container.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    if (document.body) {
        document.body.appendChild(container);
        _overlays.push(container);
    }

    for (const c of filtered) {
        if (resultMap.length >= MAX_LABELS) {
            console.warn('[SoM v6] 达到标记上限:', MAX_LABELS);
            break;
        }

        try {
            const el = c.el;
            const r = el.getBoundingClientRect();
            const off = c.off || { top: 0, left: 0 };
            const absT = r.top + off.top;
            const absL = r.left + off.left;
            if (r.width < 3 || r.height < 3) continue;

            // ★ 补丁一终极防线：渲染前最终遮挡检测
            // 在 8 层过滤管道之后，SoM 标记绘制之前，做最后一次物理探针检查。
            // 捕获在过滤阶段后动态出现的遮罩层（如延迟加载的 Loading、cookie 弹窗）。
            // 跳过 iframe 内元素（无法跨 document 做 elementFromPoint）和 L1 表单控件。
            if (c.doc === document && c.level !== 1) {
                if (isOccludedFinal(el, document, container)) {
                    continue; // 被遮罩层/弹窗物理遮挡，跳过
                }
            }

            // 打 SoM ID 属性
            el.setAttribute('data-som-id', String(somId));
            const tagLow = el.tagName.toLowerCase();
            const url = tagLow === 'a' ? (el.href || '') : '';
            if (url) el.setAttribute('data-som-url', url);

            // 红色边框
            const border = document.createElement('div');
            border.style.cssText = [
                'position:fixed',
                'border:2px solid rgba(255,0,0,0.75)',
                'background:rgba(255,0,0,0.04)',
                'pointer-events:none',
                'box-sizing:border-box',
                'z-index:2147483647',
                `top:${absT}px`,
                `left:${absL}px`,
                `width:${r.width}px`,
                `height:${r.height}px`,
            ].join(';');
            container.appendChild(border);

            // 黑底白字序号标签
            const lbl = document.createElement('div');
            lbl.textContent = String(somId);
            lbl.style.cssText = [
                'position:fixed',
                'background:rgba(0,0,0,0.85)',
                'color:#fff',
                'font-size:12px',
                'font-weight:bold',
                'font-family:Arial,sans-serif',
                'line-height:16px',
                'padding:1px 4px',
                'border-radius:2px',
                'pointer-events:none',
                'z-index:2147483647',
                'white-space:nowrap',
                `top:${Math.max(0, absT - 18)}px`,
                `left:${Math.max(0, absL)}px`,
            ].join(';');
            container.appendChild(lbl);

            // ★ 标准化结果条目
            const role = deriveRole(el);
            const name = deriveName(el);
            const state = extractState(el);
            const stateStr = serializeState(state);

            const entry = {
                id: somId,
                tag: tagLow,
                role: role,
                name: name,
                state: stateStr,        // 新增：序列化状态字符串
                stateObj: state,         // 新增：结构化状态对象（供 Python 直接使用）
                rect: {
                    x: Math.round(absL),
                    y: Math.round(absT),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                },
            };

            // 详细属性描述
            const desc = getInputDesc(el);
            if (desc) entry.inputDesc = desc;

            // 父容器语境
            const parentCtx = getParentContext(el);
            if (parentCtx) entry.parentContext = parentCtx;

            // iframe 来源信息
            if (c.frameIndex !== undefined) {
                entry.frameIndex = c.frameIndex;
                entry.frameSrc = c.frameSrc || '';
            }

            resultMap.push(entry);
            somId++;
        } catch (entryErr) {
            // ★ 单元素绘制异常不中断全局
            console.warn('[SoM v6] 标记绘制异常:', entryErr);
        }
    }

    console.log(
        `[SoM v6] 标记完成: ${resultMap.length} 个元素 ` +
        `(L1: ${l1Count}, MAX: ${MAX_LABELS}, 候选: ${allCandidates.length}, ` +
        `过滤后: ${filtered.length})`
    );
    return { resultMap, nextId: somId };
};
