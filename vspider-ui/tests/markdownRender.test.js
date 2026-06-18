// Behavior lock for composables/markdownRender.js (方向D · 从 App.vue 抽离)
//
// renderMarkdown was previously trapped inside App.vue <script setup> and thus
// untestable. After extraction these tests pin the contract so future edits to
// the lightweight renderer can't silently regress the Final Answer panel.
import { describe, it, expect } from 'vitest'
import { escapeHtml, renderMarkdown } from '../src/composables/markdownRender.js'

describe('escapeHtml', () => {
  it('escapes the five HTML-sensitive characters', () => {
    expect(escapeHtml(`<a href="x" id='y'>&</a>`)).toBe(
      '&lt;a href=&quot;x&quot; id=&#39;y&#39;&gt;&amp;&lt;/a&gt;',
    )
  })

  it('coerces non-strings', () => {
    expect(escapeHtml(42)).toBe('42')
  })
})

describe('renderMarkdown', () => {
  it('returns empty string for null/empty input', () => {
    expect(renderMarkdown(null)).toBe('')
    expect(renderMarkdown(undefined)).toBe('')
    expect(renderMarkdown('')).toBe('')
  })

  it('renders headings h1/h2/h3', () => {
    expect(renderMarkdown('# T')).toBe('<h1 class="md-h1">T</h1>')
    expect(renderMarkdown('## T')).toBe('<h2 class="md-h2">T</h2>')
    expect(renderMarkdown('### T')).toBe('<h3 class="md-h3">T</h3>')
  })

  it('renders bold and italic', () => {
    expect(renderMarkdown('**b**')).toContain('<strong>b</strong>')
    expect(renderMarkdown('*i*')).toContain('<em>i</em>')
  })

  it('renders inline code with escaping', () => {
    const out = renderMarkdown('use `a<b>` here')
    expect(out).toContain('<code class="md-icode">a&lt;b&gt;</code>')
  })

  it('renders fenced code blocks with language class and escaping', () => {
    const out = renderMarkdown('```js\nlet x = 1 < 2\n```')
    expect(out).toContain('<pre class="md-pre"><code class="md-code lang-js">')
    expect(out).toContain('let x = 1 &lt; 2')
  })

  it('allows http/https/mailto links only, escaping the rest to text', () => {
    expect(renderMarkdown('[ok](https://a.com)')).toContain(
      '<a class="md-a" href="https://a.com" target="_blank" rel="noopener noreferrer">ok</a>',
    )
    // javascript: scheme is not whitelisted -> rendered as escaped plain text
    const evil = renderMarkdown('[x](javascript:alert(1))')
    expect(evil).not.toContain('<a ')
    expect(evil).toContain('[x](javascript:alert(1))')
  })

  it('renders unordered lists', () => {
    const out = renderMarkdown('- a\n- b')
    expect(out).toContain('<ul class="md-ul"><li>a</li><li>b</li></ul>')
  })

  it('wraps plain text blocks in paragraphs with <br/> for soft breaks', () => {
    const out = renderMarkdown('line1\nline2')
    expect(out).toBe('<p class="md-p">line1<br/>line2</p>')
  })

  it('does not let raw HTML escape the sandbox', () => {
    const out = renderMarkdown('<script>alert(1)</script>')
    expect(out).not.toContain('<script>')
    expect(out).toContain('&lt;script&gt;')
  })
})
