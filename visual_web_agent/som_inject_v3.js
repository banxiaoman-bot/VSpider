/**
 * SoM (Set of Mark) 视觉标记注入脚本 v3
 * 
 * v3 修复（基于百度登录弹窗实际截图反馈）:
 *   1. ★ 跨 document 几何包含过滤（主页面大框 vs iframe 内小框）
 *   2. ★ cursor:pointer 元素增加面积上限，防止大容器 div 混入
 *   3. ★ 对 input/textarea 额外标注 placeholder，帮助 VLM 区分用户名框 vs 密码框
 *   4. ★ 空文本 + 无功能的元素直接过滤（解决 [20][23][34][35] 等空框）
 *   5. ★ 优化 resultMap 输出，给 VLM 更多语义信息
 */

(startIndex = 1) => {
    // ========== 1. 清理上一轮标记 ==========
    if (typeof window.__removeSomMarks === 'function') {
        window.__removeSomMarks();
    }

    const overlayElements = [];

    window.__clearSomOverlays = function () {
        overlayElements.forEach(el => {
            if (el && el.parentNode) el.parentNode.removeChild(el);
        });
        overlayElements.length = 0;
    };

    window.__removeSomMarks = function () {
        window.__clearSomOverlays();
        document.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));
        document.querySelectorAll('[data-som-url]').forEach(el => el.removeAttribute('data-som-url'));
        try {
            document.querySelectorAll('iframe').forEach(iframe => {
                try {
                    const d = iframe.contentDocument || iframe.contentWindow?.document;
                    if (d) {
                        d.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));
                        d.querySelectorAll('[data-som-url]').forEach(el => el.removeAttribute('data-som-url'));
                    }
                } catch (e) { }
            });
        } catch (e) { }
    };

    // ========== 2. 辅助函数 ==========

    function isElementVisible(el, iframeOffset) {
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        const offTop = iframeOffset?.top || 0;
        const offLeft = iframeOffset?.left || 0;
        const absTop = rect.top + offTop;
        const absLeft = rect.left + offLeft;

        if (rect.width < 12 || rect.height < 12) return false;
        if (absTop + rect.height < 0 || absTop > window.innerHeight) return false;
        if (absLeft + rect.width < 0 || absLeft > window.innerWidth) return false;

        try {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
        } catch (e) { return false; }

        return true;
    }

    function getElementText(el) {
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel.trim().substring(0, 50);
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            return (el.getAttribute('placeholder') || el.value || el.getAttribute('name') || '').substring(0, 50);
        }
        return (el.innerText || el.textContent || '').trim().substring(0, 50);
    }

    /** ★ 获取 input 的详细描述，帮 VLM 区分不同输入框 */
    function getInputDescription(el) {
        const parts = [];
        const tag = el.tagName.toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA') {
            const type = el.getAttribute('type') || 'text';
            const placeholder = el.getAttribute('placeholder') || '';
            const name = el.getAttribute('name') || '';
            const id = el.getAttribute('id') || '';
            parts.push(`type=${type}`);
            if (placeholder) parts.push(`placeholder="${placeholder}"`);
            if (name) parts.push(`name=${name}`);
            if (id) parts.push(`id=${id}`);
            // ★ checkbox/radio 暴露勾选状态，让 VLM 知道当前是否已勾选
            if (type === 'checkbox' || type === 'radio') {
                parts.push(`checked=${el.checked}`);
            }
        }
        return parts.join(', ');
    }

    function hasNonEmptyText(el) {
        return getElementText(el).length > 0;
    }

    // ========== 3. 核心过滤逻辑 ==========

    /**
     * ★ 判断元素是否应该标记
     * 比 v2 更严格：cursor:pointer 增加面积上限，空框直接干掉
     */
    function shouldMark(el) {
        const tagName = el.tagName.toUpperCase();
        const rect = el.getBoundingClientRect();
        const area = rect.width * rect.height;

        // 第一优先级：真正的表单控件
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tagName)) {
            return true;
        }

        // BUTTON 标签（但过滤掉不可见的超大按钮容器）
        if (tagName === 'BUTTON') {
            return area < 200000; // 大于 ~450x450 的"按钮"肯定不是真按钮
        }

        // 链接：必须有有效 href
        if (tagName === 'A') {
            const href = el.getAttribute('href');
            if (!href || href === '#' || href === 'javascript:void(0)' || href === 'javascript:;') return false;
            return true;
        }

        // LABEL
        if (tagName === 'LABEL') {
            return hasNonEmptyText(el);
        }

        // ARIA role
        const role = el.getAttribute('role');
        if (role && ['button', 'checkbox', 'tab', 'link', 'menuitem', 'option', 'switch', 'radio'].includes(role)) {
            return area < 200000;
        }

        // ★ cursor:pointer 元素（严格条件）
        // 必须：有文本 + 面积不超过 50000px²（约 ~220x220）
        // 这样可以捕获"阅读并接受协议"、"忘记密码"等小链接
        // 但过滤掉整个弹窗容器、大 div 等
        try {
            const style = window.getComputedStyle(el);
            if (style.cursor === 'pointer') {
                if (hasNonEmptyText(el) && area < 50000) {
                    // ★ 额外检查：如果这个元素的直接文本很少但 innerText 很长
                    // 说明文本来自子元素，这个元素只是容器
                    const directText = Array.from(el.childNodes)
                        .filter(n => n.nodeType === Node.TEXT_NODE)
                        .map(n => n.textContent.trim())
                        .join('');
                    const totalText = (el.innerText || '').trim();
                    // 如果元素自己没有直接文本，且子元素文本很长，跳过
                    if (!directText && totalText.length > 30) return false;
                    return true;
                }
            }
        } catch (e) { }

        return false;
    }

    /**
     * ★ DOM 层级父子过滤（同一个 document 内）
     */
    function filterParentContainers(elements) {
        const elementSet = new Set(elements);
        const result = [];

        for (const el of elements) {
            const tagName = el.tagName.toUpperCase();

            // 表单控件永远保留
            if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tagName)) {
                result.push(el);
                continue;
            }

            let hasMarkedChild = false;
            for (const other of elementSet) {
                if (other !== el && el.contains(other)) {
                    hasMarkedChild = true;
                    break;
                }
            }

            if (!hasMarkedChild) {
                result.push(el);
            }
        }
        return result;
    }

    /**
     * ★ 跨 document 几何包含过滤
     * 解决：主页面的大框（如整个弹窗 div）包住了 iframe 内的小框
     * 如果 A 的绝对矩形完全包含 B 的绝对矩形，且 A 面积 > B 面积 * 2，剔除 A
     */
    function filterCrossDocGeometric(candidates) {
        const rects = candidates.map(c => {
            const r = c.el.getBoundingClientRect();
            const off = c.iframeOffset || { top: 0, left: 0 };
            return {
                top: r.top + off.top,
                left: r.left + off.left,
                bottom: r.top + off.top + r.height,
                right: r.left + off.left + r.width,
                area: r.width * r.height
            };
        });

        const toRemove = new Set();

        for (let i = 0; i < candidates.length; i++) {
            if (toRemove.has(i)) continue;
            const a = rects[i];
            const tagA = candidates[i].el.tagName.toUpperCase();

            // 表单控件不会被剔除
            if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tagA)) continue;

            for (let j = 0; j < candidates.length; j++) {
                if (i === j || toRemove.has(j)) continue;
                const b = rects[j];

                // A 完全包含 B，且面积大得多
                if (a.top <= b.top + 2 && a.bottom >= b.bottom - 2 &&
                    a.left <= b.left + 2 && a.right >= b.right - 2 &&
                    a.area > b.area * 1.5) {
                    toRemove.add(i);
                    break;
                }
            }
        }

        return candidates.filter((_, i) => !toRemove.has(i));
    }

    /**
     * ★ 文本去重：相同文本只保留最小面积的元素
     */
    function deduplicateByText(candidates) {
        const textMap = new Map();

        for (let i = 0; i < candidates.length; i++) {
            const c = candidates[i];
            const text = getElementText(c.el);
            if (!text) continue; // 无文本的（如 input）不参与去重

            const r = c.el.getBoundingClientRect();
            const area = r.width * r.height;

            if (!textMap.has(text)) {
                textMap.set(text, []);
            }
            textMap.get(text).push({ index: i, area });
        }

        const toRemove = new Set();
        for (const [, entries] of textMap) {
            if (entries.length > 1) {
                entries.sort((a, b) => a.area - b.area);
                for (let k = 1; k < entries.length; k++) {
                    toRemove.add(entries[k].index);
                }
            }
        }

        return candidates.filter((_, i) => !toRemove.has(i));
    }

    // ========== 4. 从 document 中收集元素 ==========
    function collectFromDocument(doc, iframeOffset) {
        const candidates = [];
        const selector = [
            'input', 'button', 'a', 'select', 'textarea', 'label',
            '[role="button"]', '[role="checkbox"]', '[role="tab"]',
            '[role="link"]', '[role="menuitem"]', '[role="option"]',
            '[role="switch"]', '[role="radio"]',
            'div', 'span', 'li', 'p'
        ].join(', ');

        try {
            doc.querySelectorAll(selector).forEach(el => {
                if (!isElementVisible(el, iframeOffset)) return;
                if (!shouldMark(el)) return;
                candidates.push({
                    el,
                    iframeOffset: iframeOffset || { top: 0, left: 0 },
                    doc
                });
            });
        } catch (e) {
            console.warn('[SoM] 收集失败:', e);
        }
        return candidates;
    }

    // ========== 5. 主逻辑 ==========
    let allCandidates = [];

    // 5.1 主页面
    allCandidates = allCandidates.concat(collectFromDocument(document, { top: 0, left: 0 }));

    // 5.2 ★ 遍历 iframe
    try {
        document.querySelectorAll('iframe').forEach((iframe, idx) => {
            try {
                const iframeDoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (!iframeDoc) return;
                const iframeRect = iframe.getBoundingClientRect();
                if (iframeRect.width < 10 || iframeRect.height < 10) return;

                const iframeOffset = { top: iframeRect.top, left: iframeRect.left };
                const iframeCandidates = collectFromDocument(iframeDoc, iframeOffset);
                iframeCandidates.forEach(c => {
                    c.frameIndex = idx;
                    c.frameSrc = iframe.src || '';
                });
                allCandidates = allCandidates.concat(iframeCandidates);
            } catch (e) {
                console.log(`[SoM] iframe #${idx} 跨域 (${iframe.src})`);
            }
        });
    } catch (e) { }

    // ========== 6. 多层过滤 ==========

    // 6.1 同 document 内 DOM 父子过滤
    const byDoc = new Map();
    for (const c of allCandidates) {
        if (!byDoc.has(c.doc)) byDoc.set(c.doc, []);
        byDoc.get(c.doc).push(c);
    }

    let filtered = [];
    for (const [doc, candidates] of byDoc) {
        const els = candidates.map(c => c.el);
        const kept = filterParentContainers(els);
        const keptSet = new Set(kept);
        candidates.filter(c => keptSet.has(c.el)).forEach(c => filtered.push(c));
    }

    // 6.2 ★ 跨 document 几何包含过滤
    filtered = filterCrossDocGeometric(filtered);

    // 6.3 文本去重
    filtered = deduplicateByText(filtered);

    // 6.4 ★ 最后一道：过滤掉完全空白（无文本、无 placeholder、非表单控件）的元素
    filtered = filtered.filter(c => {
        const tag = c.el.tagName.toUpperCase();
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return true;
        if (tag === 'BUTTON') return true;
        return hasNonEmptyText(c.el);
    });

    // 6.5 排序：从上到下、从左到右
    filtered.sort((a, b) => {
        const ra = a.el.getBoundingClientRect();
        const rb = b.el.getBoundingClientRect();
        const ay = ra.top + (a.iframeOffset?.top || 0);
        const by_ = rb.top + (b.iframeOffset?.top || 0);
        const ax = ra.left + (a.iframeOffset?.left || 0);
        const bx = rb.left + (b.iframeOffset?.left || 0);
        if (Math.abs(ay - by_) > 10) return ay - by_;
        return ax - bx;
    });

    // ========== 7. 绘制标记 ==========
    const resultMap = [];
    let somId = startIndex;

    const overlayContainer = document.createElement('div');
    overlayContainer.id = '__som_overlay_container';
    overlayContainer.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    document.body.appendChild(overlayContainer);
    overlayElements.push(overlayContainer);

    const MAX_LABELS = 60; // ★ 降低上限，减少 VLM 认知负担

    for (let i = 0; i < filtered.length; i++) {
        if (resultMap.length >= MAX_LABELS) {
            console.warn(`[SoM v3] 达到上限 (${MAX_LABELS})`);
            break;
        }

        const c = filtered[i];
        const el = c.el;
        const off = c.iframeOffset || { top: 0, left: 0 };
        const rect = el.getBoundingClientRect();
        const absTop = rect.top + off.top;
        const absLeft = rect.left + off.left;

        if (rect.width < 3 || rect.height < 3) continue;

        // 设置 data-som-id
        el.setAttribute('data-som-id', String(somId));

        // 提取 URL
        let extractedUrl = '';
        const tagName = el.tagName.toLowerCase();
        if (tagName === 'img') extractedUrl = el.src || el.getAttribute('data-src') || '';
        else if (tagName === 'a') extractedUrl = el.href || '';
        if (extractedUrl) el.setAttribute('data-som-url', extractedUrl);

        // 绘制边框
        const border = document.createElement('div');
        border.className = '__som_border';
        border.style.cssText = [
            'position:fixed',
            'border:2px solid rgba(255, 0, 0, 0.6)',
            'background:rgba(255, 0, 0, 0.05)',
            'pointer-events:none !important',
            'box-sizing:border-box',
            'z-index:2147483647 !important',
            `top:${absTop}px`,
            `left:${absLeft}px`,
            `width:${rect.width}px`,
            `height:${rect.height}px`
        ].join(';');
        overlayContainer.appendChild(border);

        // 数字标签
        const label = document.createElement('div');
        label.className = '__som_label';
        label.textContent = String(somId);
        label.style.cssText = [
            'position:fixed',
            'background:rgba(0, 0, 0, 0.85)',
            'color:#FFFFFF',
            'font-size:12px',
            'font-weight:bold',
            'font-family:Arial,sans-serif',
            'line-height:16px',
            'padding:1px 4px',
            'border-radius:2px',
            'pointer-events:none !important',
            'z-index:2147483647 !important',
            'white-space:nowrap',
            `top:${Math.max(0, absTop - 18)}px`,
            `left:${Math.max(0, absLeft)}px`
        ].join(';');
        overlayContainer.appendChild(label);

        // ★ 增强的 resultMap 条目
        const mapEntry = {
            id: somId,
            tag: tagName,
            type: el.getAttribute('type') || '',
            text: getElementText(el),
            rect: {
                x: Math.round(absLeft),
                y: Math.round(absTop),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
            }
        };

        // ★ 对 input/textarea 附加详细描述
        const desc = getInputDescription(el);
        if (desc) mapEntry.inputDesc = desc;

        if (c.frameIndex !== undefined) {
            mapEntry.frameIndex = c.frameIndex;
            mapEntry.frameSrc = c.frameSrc || '';
        }

        resultMap.push(mapEntry);
        somId++;
    }

    console.log(`[SoM v3] 共标记 ${resultMap.length} 个元素`);
    return { resultMap, nextId: somId };
}