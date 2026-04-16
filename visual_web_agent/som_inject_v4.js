/**
 * SoM (Set of Mark) v4
 *
 * 彻底重构版，基于 browser-use / WebVoyager 设计理念:
 *   1. ★ elementFromPoint 点穿透测试 — 过滤透明遮罩及被覆盖元素
 *   2. ★ Shadow DOM 递归穿透 — 覆盖 Web Components 封装的交互控件
 *   3. ★ 语义分级 (AOM 思维) — 原生控件(L1) > ARIA/tabindex(L2) > cursor:pointer(L3)
 *   4. 继承 v3: 跨-doc 几何包含过滤 / 父子容器剔除 / 文本去重 / iframe 注入
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
        } catch (_) { return false; }
        return true;
    }

    /**
     * ★ elementFromPoint 点穿透测试
     *
     * 在元素中心（及角落）多点采样，检查最顶层元素是否为此元素或其后代。
     * 若被透明遮罩 / 模态覆盖，返回 false，从标记集中剔除。
     * 注意：在绘制 overlay 之前运行，不受 SoM 标注层干扰。
     */
    function isTopVisible(el, doc) {
        try {
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            // 采样点：中心 + 左上内缩 + 右下内缩（鲁棒性更高）
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
        const alt = el.getAttribute('alt');
        if (alt?.trim()) return alt.trim().slice(0, 60);
        const tag = el.tagName.toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA') {
            return (el.getAttribute('placeholder') || el.value || el.getAttribute('name') || '').slice(0, 60);
        }
        return (el.innerText || el.textContent || '').trim().slice(0, 60);
    }

    /**
     * 获取元素的详细属性描述，帮助 VLM 区分元素并理解其语义：
     *   - INPUT / TEXTAREA：暴露 type/placeholder/name/id/value（先读后写预检）
     *   - A：暴露 href（让 VLM 知道链接目标，支持 extract_link / save_to_memory 决策）
     */
    function getInputDesc(el) {
        const tag = el.tagName.toUpperCase();

        // ★ <a> 标签：暴露完整 href（截断至 150 字符防止过长），
        //   跳过空链接、锚点、JavaScript 伪协议和 mailto/tel
        if (tag === 'A') {
            // el.href 为绝对化后的 href；getAttribute('href') 为原始值
            const raw  = (el.getAttribute('href') || '').trim();
            const full = (el.href || '').trim();
            // 优先用绝对路径（full），但纯锚点只会得到当前页 URL，改用 raw 判断
            if (!raw || /^(#|javascript:|mailto:|tel:)/i.test(raw)) return '';
            // 使用绝对路径（更易于 VLM 判断是否是外链/API/下载链接）
            const href = (full && !full.endsWith('#')) ? full : raw;
            return `href="${href.slice(0, 150)}"`;
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
            // ★ 暴露当前值，让 VLM 执行"先读后写"表单预检：
            //   只对已有内容的字段提示，避免 value="" 的无效噪音
            const val = (el.value || '').trim();
            if (val) parts.push(`value="${val.slice(0, 50)}"`);
        }
        return parts.join(', ');
    }

    // ═══════════════════════════════════════════════════════════
    //  3. 语义分级 (AOM 思维)
    // ═══════════════════════════════════════════════════════════

    const INTERACT_ROLES = new Set([
        'button', 'checkbox', 'radio', 'tab', 'link',
        'menuitem', 'option', 'switch', 'combobox', 'listbox',
        'spinbutton', 'slider', 'textbox', 'searchbox',
        'treeitem', 'gridcell', 'row',
    ]);

    /**
     * 返回元素交互等级：
     *   1 = 原生表单控件（最高，不受面积/遮挡过滤影响）
     *   2 = button / 有效 a / ARIA 角色 / tabindex 元素
     *   3 = cursor:pointer 小元素（最低，严格条件）
     *   0 = 不标记
     */
    function interactLevel(el) {
        const tag = el.tagName.toUpperCase();

        // L1：原生表单控件
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return 1;

        const r = el.getBoundingClientRect();
        const area = r.width * r.height;

        // L2a：button 标签（过滤掉不合理的超大"按钮"容器）
        if (tag === 'BUTTON') return area < 160000 ? 2 : 0;

        // L2b：有有效 href 的超链接
        if (tag === 'A') {
            const href = el.getAttribute('href') || '';
            if (href && !/^(#|javascript:)/i.test(href)) return 2;
            // 无有效 href 但有 ARIA role 的 <a> 继续走 L2c
        }

        // L2c：ARIA 交互 role
        const role = (el.getAttribute('role') || '').trim().toLowerCase();
        if (INTERACT_ROLES.has(role)) return area < 160000 ? 2 : 0;

        // L2d：label 标签（点击激活关联控件）
        if (tag === 'LABEL') return getLabel(el).length > 0 ? 2 : 0;

        // L2e：tabindex >= 0（明确可键盘聚焦，且有内容）
        const tabIdx = el.getAttribute('tabindex');
        if (tabIdx !== null && tabIdx !== '-1') {
            if (area > 0 && area < 160000 && getLabel(el).length > 0) return 2;
        }

        // L3：cursor:pointer 的小元素（严格条件，最后手段）
        if (area > 40000) return 0; // 超过 ~200×200 不考虑
        try {
            if (window.getComputedStyle(el).cursor === 'pointer') {
                const label = getLabel(el);
                if (label.length < 1) return 0;
                // 排除"文本来自子节点"的容器：无直接文本节点且总文本较长
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

    /**
     * 遍历 root（Document / ShadowRoot）下所有元素，
     * 收集可交互候选，同时递归穿透内嵌 shadowRoot。
     *
     * @param {Document|ShadowRoot} root
     * @param {Document}            doc   — 元素所在的 document（用于 elementFromPoint）
     * @param {{top:number,left:number}} off — iframe 偏移量
     * @param {Array}               results — 输出数组
     */
    function collectFromRoot(root, doc, off, results) {
        try {
            root.querySelectorAll('*').forEach(el => {
                // ★ 先递归穿透 Shadow DOM（无论该元素自身是否可交互）
                if (el.shadowRoot) collectFromRoot(el.shadowRoot, doc, off, results);

                const level = interactLevel(el);
                if (level === 0) return;
                if (!isVisible(el, off)) return;
                results.push({ el, doc, off, level });
            });
        } catch (e) {
            console.warn('[SoM v4] 收集异常:', e);
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  5. 主收集逻辑
    // ═══════════════════════════════════════════════════════════

    let allCandidates = [];

    // 5.1 主页面（含 Shadow DOM）
    collectFromRoot(document, document, { top: 0, left: 0 }, allCandidates);

    // 5.2 同源 iframe（跨域 iframe 由 Python 侧 frame.evaluate() 单独注入）
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
            } catch (_) {
                // 跨域 iframe，跳过（由外部 frame.evaluate 单独处理）
            }
        });
    } catch (_) {}

    // ═══════════════════════════════════════════════════════════
    //  6. 多层过滤管道
    // ═══════════════════════════════════════════════════════════

    let filtered = allCandidates;

    // ── 6.1 ★ elementFromPoint 点穿透测试 ────────────────────
    // 仅对主页面元素（doc === document）检测，因为 iframe 内无法用主 doc 测试。
    // 表单控件 L1 豁免（即使被遮挡也不误删，防止 hidden type input 被干掉）。
    filtered = filtered.filter(c => {
        if (c.doc !== document) return true;
        if (c.level === 1) return true; // 原生表单控件豁免
        return isTopVisible(c.el, document);
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
                if (c.level === 1) { next.push(c); continue; } // 表单控件永远保留
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
    // 主页面大框（如弹窗容器）包住 iframe 内小框时，剔除大框。
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
    //  7. 绘制标记
    // ═══════════════════════════════════════════════════════════

    const resultMap = [];
    let somId = startIndex;
    const MAX_LABELS = 50; // 降低上限，减轻 VLM 认知负担

    const container = document.createElement('div');
    container.id = '__som_overlay_container';
    container.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    document.body.appendChild(container);
    _overlays.push(container);

    for (const c of filtered) {
        if (resultMap.length >= MAX_LABELS) {
            console.warn('[SoM v4] 达到标记上限:', MAX_LABELS);
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
        if (c.frameIndex !== undefined) {
            entry.frameIndex = c.frameIndex;
            entry.frameSrc   = c.frameSrc || '';
        }

        resultMap.push(entry);
        somId++;
    }

    console.log(`[SoM v4] 标记完成: ${resultMap.length} 个元素`);
    return { resultMap, nextId: somId };
}
