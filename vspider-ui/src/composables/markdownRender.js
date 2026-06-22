// ─────────────────────────────────────────────────────────────
//  轻量 Markdown 渲染器（不引入第三方依赖）
//  支持：``` 代码块、`inline 代码`、**粗体**、*斜体*、# / ## / ###
//        标题、- / * 列表、[text](url) 链接、段落（空行分隔）。
//  安全：先把代码块抽成占位符，再把剩余文本整体 escapeHtml，
//        最后才把 Markdown 标记替换为受控的 HTML 标签；
//        链接仅放行 http/https/mailto 协议，其余统一渲染为纯文本。
// ─────────────────────────────────────────────────────────────
export const escapeHtml = (s) =>
  String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

export const renderMarkdown = (src) => {
  if (src == null || src === '') return ''
  let text = String(src).replace(/\r\n/g, '\n')

  // 1. 抽取 ``` 代码块到占位符，避免内部内容被后续替换破坏
  const codeBlocks = []
  text = text.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    const idx = codeBlocks.length
    const langClass = lang ? ` lang-${escapeHtml(lang)}` : ''
    codeBlocks.push(
      `<pre class="md-pre"><code class="md-code${langClass}">${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`,
    )
    return `\u0000CB${idx}\u0000`
  })

  // 2. 抽取行内代码到占位符
  const inlineCodes = []
  text = text.replace(/`([^`\n]+)`/g, (_m, c) => {
    const idx = inlineCodes.length
    inlineCodes.push(`<code class="md-icode">${escapeHtml(c)}</code>`)
    return `\u0000IC${idx}\u0000`
  })

  // 3. 整体 escape HTML
  text = escapeHtml(text)

  // 4. 标题 ### / ## / # （顺序：长前缀先匹配）
  text = text.replace(/^###\s+(.+)$/gm, '<h3 class="md-h3">$1</h3>')
  text = text.replace(/^##\s+(.+)$/gm, '<h2 class="md-h2">$1</h2>')
  text = text.replace(/^#\s+(.+)$/gm, '<h1 class="md-h1">$1</h1>')

  // 5. 粗体、斜体（粗体优先以避免 ** 被 * 抢先吃掉）
  text = text.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>')

  // 6. 链接 [text](url) —— 仅允许 http/https/mailto
  text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, href) => {
    if (!/^(https?:|mailto:)/i.test(href)) return m
    return `<a class="md-a" href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })

  // 7. 无序列表：连续的 - / * 行 → <ul><li>
  text = text.replace(
    /(?:^|\n)((?:[-*]\s+.+(?:\n|$))+)/g,
    (_m, block) => {
      const items = block
        .split('\n')
        .filter((l) => l.trim())
        .map((l) => `<li>${l.replace(/^[-*]\s+/, '')}</li>`)
        .join('')
      return `\n<ul class="md-ul">${items}</ul>\n`
    },
  )

  // 8. 段落：空行分隔的块；已经是 <h*>/<ul>/<pre>/占位符 的块原样保留
  text = text
    .split(/\n{2,}/)
    .map((chunk) => {
      const t = chunk.trim()
      if (!t) return ''
      if (/^<(?:h\d|ul|pre|blockquote)/.test(t)) return t
      if (/^\u0000CB\d+\u0000$/.test(t)) return t
      return `<p class="md-p">${chunk.replace(/\n/g, '<br/>')}</p>`
    })
    .join('\n')

  // 9. 还原代码块占位符
  text = text.replace(/\u0000CB(\d+)\u0000/g, (_m, i) => codeBlocks[+i] || '')
  text = text.replace(/\u0000IC(\d+)\u0000/g, (_m, i) => inlineCodes[+i] || '')

  return text
}
