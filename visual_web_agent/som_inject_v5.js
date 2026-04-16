/**
 * SoM (Set of Mark) v5
 *
 * 升级重点（相对 v4）:
 *   1. ★ contenteditable 元素升级为 L1 — 修复百度文库等现代 SPA 搜索框漏标问题
 *   2. ★ role=textbox / searchbox / combobox / spinbutton 升级为 L1 — 语义等价于原生输入框
 *   3. ★ MAX_LABELS 动态化 — 根据视口内候选密度自适应，避免截断关键元素
 *   4. ★ isTopVisible 对 L1 宽松化 — L1 元素只做轻量中心点检查，不因子元素命中误删
 *   5. ★ getInputDesc / getLabel 补充 contenteditable 语义信息
 *   6. 继承 v4 全部能力：Shadow DOM 穿透 / elementFromPoint / 父子过滤 / 文本去重
 */

(startIndex = 1) => {
    // ═══════════════════════════════════════════════════════════
    //  1. 清理上轮标记
    // ═══════════════════════════════════════════════════════════
    if (typeof window.__removeSomMarks === 'function') window.__removeSomMarks();

    const _overlays = [];

    window.__clearSomOverlays = () => {
        _overlays.forEach(el => el?.parentNode?.removeChild(el));
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
    //  2. 工具函数
    // ═══════════════════════════════════════════════════════════

    /** 视口内可见性检查 */
    function isVisible(el, off) {
        if (!el?.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) return false;
        const absT = r.top + (off?.top || 0);
        const absL = r.left + (off?.left || 0);
        if (absT + r.height < 0 || absT > window.innerHeight) return false;
        if (absL + r.width < 0 || absL > window.innerWidth) return false;
        try {
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
            // 额外检查 pointer-events:none（不可交互）
            if (s.pointerEvents === 'none') return false;
        } catch (_) { return false; }
        return true;
    }

    /**
     * ★ elementFromPoint 点穿透测试（v5 改进版）
     *
     * 对 L1 元素（原生表单 + contenteditable）只做中心点单次检测，
     * 且放宽条件：命中自身 OR 命中任何祖先都视为通过（修复子元素遮挡误删问题）。
     * 对 L2/L3 元素保持 v4 的严格三点检测。
     */
    function isTopVisible(el, doc, isL1) {
        try {
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width  / 2;
            const cy = r.top  + r.height / 2;

            if (isL1) {
                // L1 宽松：中心点命中自身或祖先即通过
                const hit = doc.elementFromPoint(cx, cy);
                if (!hit) return true; // 异常宽松处理
                // 自身、后代、祖先都算命中
                if (el === hit || el.contains(hit) || hit.closest('[data-som-id]') === el) return true;
                // 再检查 hit 是否在 el 内部（兜底）
                let node = hit;
                while (node) {
                    if (node === el) return true;
                    node = node.parentElement;
                }
                return false;
            }

            // L2/L3 严格三点检测（继承 v4）
            const pts = [
                [cx, cy],
                [r.left + 4,  r.top + 4],
                [r.right - 4, r.bottom - 4],
            ];
            for (const [x, y] of pts) {
                const hit = doc.elementFromPoint(x, y);
                if (hit && (el === hit || el.contains(hit))) return true;
            }
            return false;
        } catch (_) {
            return true; // 异常时宽松处理，不误删
        }
    }

    /** 获取元素代表性文本标签（供去重和 VLM 参考） */
    function getLabel(el) {
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel?.trim()) return ariaLabel.trim().slice(0, 60);
        const placeholder = el.getAttribute('placeholder') || el.getAttribute('data-placeholder');
        if (placeholder?.trim()) return placeholder.trim().slice(0, 60);
        const alt = el.getAttribute('alt');
        if (alt?.trim()) return alt.trim().slice(0, 60);
        const tag = el.tagName.toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA') {
            return (el.value || el.getAttribute('name') || '').slice(0, 60);
        }
        // ★ contenteditable：优先读 textContent（用户已输入的内容）
        if (el.isContentEditable) {
            const text = (el.textContent || '').trim();
            return text.slice(0, 60);
        }
        return (el.innerText || el.textContent || '').trim().slice(0, 60);
    }

    /**
     * 获取元素的详细属性描述，帮助 VLM 区分元素并理解其语义。
     * v5 新增：contenteditable 元素的描述
     */
    function getInputDesc(el) {
        const tag = el.tagName.toUpperCase();

        // ★ <a> 标签：暴露完整 href
        if (tag === 'A') {
            const raw  = (el.getAttribute('href') || '').trim();
            const full = (el.href || '').trim();
            if (!raw || /^(#|javascript:|mailto:|tel:)/i.test(raw)) return '';
            const href = (full && !full.endsWith('#')) ? full : raw;
            return `href="${href.slice(0, 150)}"`;
        }

        // ★ contenteditable 元素（v5 新增）
        if (el.isContentEditable) {
            const ph = el.getAttribute('placeholder')
                    || el.getAttribute('data-placeholder')
                    || el.getAttribute('aria-placeholder')
                    || el.getAttribute('aria-label')
                    || '';
            const role = el.getAttribute('role') || 'textbox';
            const parts = [`type=text`, `contenteditable=true`, `role=${role}`];
            if (ph) parts.push(`placeholder="${ph}"`);
            const val = (el.textContent || '').trim();
            if (val) parts.push(`value="${val.slice(0, 50)}"`);
            return parts.join(', ');
        }

        if (tag !== 'INPUT' && tag !== 'TEXTAREA') return '';
        const type = el.getAttribute('type') || 'text';
        const parts = [`type=${type}`];
        const ph   = el.getAttribute('placeholder');
        const name = el.getAttribute('name');
        const id   = el.getAttribute('id');
        if (ph)   parts.push(`placeholder="${ph}"`);
        if (name) parts.push(`name=${name}`);
        if (id)   parts.push(`id=${id}`);
        if (type === 'checkbox' || type === 'radio') {
            parts.push(`checked=${el.checked}`);
        } else if (type !== 'password' && type !== 'hidden') {
            const val = (el.value || '').trim();
            if (val) parts.push(`value="${val.slice(0, 50)}"`);
        }
        return parts.join(', ');
    }

    /** 获取元素父容器的语境文本（父节点文本聚合，用于列表页排名/评分等上下文） */
    function getParentContext(el) {
        try {
            const container = el.closest('li, tr, article, .item, .card, .list-item, .entry, .result');
            const parent = container || el.parentElement;
            if (!parent || parent === document.body) return '';
            const raw = (parent.innerText || parent.textContent || '').replace(/\s+/g, ' ').trim();
            const selfText = (el.innerText || el.textContent || '').trim();
            // 父节点文本与自身高度重合时（差距 ≤10 字符），跳过，避免信息冗余
            if (raw.length <= selfText.length + 10) return '';
            return raw.substring(0, 150);
        } catch (e) { return ''; }
    }

    // ═══════════════════════════════════════════════════════════
    //  3. 语义分级 (AOM 思维) — v5 升级版
    // ═══════════════════════════════════════════════════════════

    // ★ v5 升级：将 textbox / searchbox / combobox / spinbutton 纳入 L1 语义集
    const L1_ROLES = new Set([
        'textbox', 'searchbox', 'spinbutton', 'combobox',
    ]);

    const L2_ROLES = new Set([
        'button', 'checkbox', 'radio', 'tab', 'link',
        'menuitem', 'option', 'switch', 'listbox',
        'slider', 'treeitem', 'gridcell', 'row',
    ]);

    /**
     * 返回元素交互等级：
     *   1 = 原生表单控件 / contenteditable / 语义输入框（最高，不受面积/遮挡过滤）
     *   2 = button / 有效 a / ARIA 角色 / tabindex 元素
     *   3 = cursor:pointer 小元素（最低，严格条件）
     *   0 = 不标记
     */
    function interactLevel(el) {
        const tag = el.tagName.toUpperCase();

        // L1a：原生表单控件
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return 1;

        // ★ L1b：contenteditable 元素（v5 新增 — 修复现代 SPA 搜索框漏标）
        if (el.isContentEditable) return 1;

        // ★ L1c：ARIA 语义输入角色（等价于原生输入框）
        const role = (el.getAttribute('role') || '').trim().toLowerCase();
        if (L1_ROLES.has(role)) return 1;

        const r = el.getBoundingClientRect();
        const area = r.width * r.height;

        // L2a：button 标签（过滤掉不合理的超大"按钮"容器）
        if (tag === 'BUTTON') return area < 160000 ? 2 : 0;

        // L2b：有有效 href 的超链接
        if (tag === 'A') {
            const href = el.getAttribute('href') || '';
            if (href && !/^(#|javascript:)/i.test(href)) return 2;
        }

        // L2c：其余 ARIA 交互 role
        if (L2_ROLES.has(role)) return area < 160000 ? 2 : 0;

        // L2d：label 标签（点击激活关联控件）
        if (tag === 'LABEL') return getLabel(el).length > 0 ? 2 : 0;

        // L2e：tabindex >= 0（明确可键盘聚焦，且有内容）
        const tabIdx = el.getAttribute('tabindex');
        if (tabIdx !== null && tabIdx !== '-1') {
            if (area > 0 && area < 160000 && getLabel(el).length > 0) return 2;
        }

        // L3：cursor:pointer 的小元素（严格条件，最后手段）
        if (area > 40000) return 0;
        try {
            if (window.getComputedStyle(el).cursor === 'pointer') {
                const label = getLabel(el);
                if (label.length < 1) return 0;
                const directTxt = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim()).join('');
                if (!directTxt && label.length > 20) return 0;
                return 3;
            }
        } catch (_) {}

        return 0;
    }

    // ═══════════════════════════════════════════════════════════
    //  4. 候选元素收集（含 Shadow DOM 递归穿透）
    // ═══════════════════════════════════════════════════════════

    function collectFromRoot(root, doc, off, results) {
        try {
            root.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) collectFromRoot(el.shadowRoot, doc, off, results);

                const level = interactLevel(el);
                if (level === 0) return;
                if (!isVisible(el, off)) return;
                results.push({ el, doc, off, level });
            });
        } catch (e) {
            console.warn('[SoM v5] 收集异常:', e);
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  5. 主收集逻辑
    // ═══════════════════════════════════════════════════════════

    let allCandidates = [];

    // 5.1 主页面（含 Shadow DOM）
    collectFromRoot(document, document, { top: 0, left: 0 }, allCandidates);

    // 5.2 同源 iframe
    try {
        document.querySelectorAll('iframe').forEach((iframe, idx) => {
            try {
                const iDoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (!iDoc) return;
                const ir = iframe.getBoundingClientRect();
                if (ir.width < 10 || ir.height < 10) return;
                const off = { top: ir.top, left: ir.left };
                const sub = [];
                collectFromRoot(iDoc, iDoc, off, sub);
                sub.forEach(c => { c.frameIndex = idx; c.frameSrc = iframe.src || ''; });
                allCandidates = allCandidates.concat(sub);
            } catch (_) {}
        });
    } catch (_) {}

    // ═══════════════════════════════════════════════════════════
    //  6. 多层过滤管道
    // ═══════════════════════════════════════════════════════════

    let filtered = allCandidates;

    // ── 6.1 ★ elementFromPoint 点穿透测试（v5：L1 宽松模式）────
    filtered = filtered.filter(c => {
        if (c.doc !== document) return true;
        const isL1 = (c.level === 1);
        if (isL1) return isTopVisible(c.el, document, true);   // L1 宽松检测
        return isTopVisible(c.el, document, false);            // L2/L3 严格检测
    });

    // ── 6.2 同 document 内父子容器过滤 ──────────────────────
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
                if (c.level === 1) { next.push(c); continue; } // L1 永远保留
                let hasMarkedChild = false;
                for (const other of elSet) {
                    if (other !== c.el && c.el.contains(other)) { hasMarkedChild = true; break; }
                }
                if (!hasMarkedChild) next.push(c);
            }
        }
        filtered = next;
    }

    // ── 6.3 跨 document 几何包含过滤 ─────────────────────────
    {
        const rects = filtered.map(c => {
            const r = c.el.getBoundingClientRect();
            const o = c.off || { top: 0, left: 0 };
            return {
                t:    r.top    + o.top,
                l:    r.left   + o.left,
                b:    r.bottom + o.top,
                r:    r.right  + o.left,
                area: r.width  * r.height,
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
                    a.area > b.area * 1.5) { remove.add(i); break; }
            }
        }
        filtered = filtered.filter((_, i) => !remove.has(i));
    }

    // ── 6.4 文本去重（保留面积最小的元素） ────────────────────
    {
        const textMap = new Map();
        for (let i = 0; i < filtered.length; i++) {
            const text = getLabel(filtered[i].el);
            if (!text) continue;
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

    // ── 6.5 空内容兜底过滤 ────────────────────────────────────
    filtered = filtered.filter(c => {
        const tag = c.el.tagName.toUpperCase();
        if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(tag)) return true;
        if (c.el.isContentEditable) return true; // ★ contenteditable 豁免
        return getLabel(c.el).length > 0;
    });

    // ── 6.6 排序：从上到下、从左到右 ─────────────────────────
    filtered.sort((a, b) => {
        const ra = a.el.getBoundingClientRect();
        const rb = b.el.getBoundingClientRect();
        const ay = ra.top  + (a.off?.top  || 0);
        const by_ = rb.top + (b.off?.top  || 0);
        const ax = ra.left + (a.off?.left || 0);
        const bx = rb.left + (b.off?.left || 0);
        return Math.abs(ay - by_) > 10 ? ay - by_ : ax - bx;
    });

    // ═══════════════════════════════════════════════════════════
    //  7. ★ 动态 MAX_LABELS（v5 新增）
    //
    //  策略：L1（输入控件）无上限保留，L2/L3 按视口密度限制。
    //  总上限 = 基础 50 + L1 数量，确保关键输入框永不被截断。
    // ═══════════════════════════════════════════════════════════
    const l1Count   = filtered.filter(c => c.level === 1).length;
    const MAX_LABELS = Math.min(80, 50 + l1Count); // 最多 80，保底 50 + 所有 L1

    // ═══════════════════════════════════════════════════════════
    //  8. 绘制标记
    // ═══════════════════════════════════════════════════════════

    const resultMap = [];
    let somId = startIndex;

    const container = document.createElement('div');
    container.id = '__som_overlay_container';
    container.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    document.body.appendChild(container);
    _overlays.push(container);

    for (const c of filtered) {
        if (resultMap.length >= MAX_LABELS) {
            console.warn('[SoM v5] 达到标记上限:', MAX_LABELS);
            break;
        }

        const el  = c.el;
        const r   = el.getBoundingClientRect();
        const off = c.off || { top: 0, left: 0 };
        const absT = r.top  + off.top;
        const absL = r.left + off.left;
        if (r.width < 3 || r.height < 3) continue;

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

        // resultMap 条目
        const entry = {
            id:   somId,
            tag:  tagLow,
            text: getLabel(el),
            rect: {
                x:      Math.round(absL),
                y:      Math.round(absT),
                width:  Math.round(r.width),
                height: Math.round(r.height),
            },
        };
        const desc = getInputDesc(el);
        if (desc) entry.inputDesc = desc;
        const parentCtx = getParentContext(el);
        if (parentCtx) entry.parentContext = parentCtx;
        if (c.frameIndex !== undefined) {
            entry.frameIndex = c.frameIndex;
            entry.frameSrc   = c.frameSrc || '';
        }

        resultMap.push(entry);
        somId++;
    }

    console.log(`[SoM v5] 标记完成: ${resultMap.length} 个元素 (L1: ${l1Count}, MAX: ${MAX_LABELS})`);
    return { resultMap, nextId: somId };
}
