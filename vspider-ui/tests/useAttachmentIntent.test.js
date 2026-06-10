import { describe, it, expect } from 'vitest'
import {
  ATTACHMENT_INTENT_AUTO,
  ATTACHMENT_INTENT_OPTIONS,
  isValidAttachmentIntent,
  appendAttachmentIntentToFormData,
} from '../src/composables/useAttachmentIntent.js'

describe('useAttachmentIntent', () => {
  it('offers auto as the first (default) option', () => {
    expect(ATTACHMENT_INTENT_AUTO).toBe('auto')
    expect(ATTACHMENT_INTENT_OPTIONS[0].value).toBe('auto')
    expect(ATTACHMENT_INTENT_OPTIONS[0].label).toBeTruthy()
  })

  it('covers the four user-overridable intents from input_contract.v1', () => {
    const values = ATTACHMENT_INTENT_OPTIONS.map((o) => o.value)
    expect(values).toContain('batch_rows')
    expect(values).toContain('upload_to_page')
    expect(values).toContain('prompt_context')
    expect(values).toContain('media_source')
    // "unknown" is an inference result, never a user choice.
    expect(values).not.toContain('unknown')
  })

  it('every option carries a non-empty label', () => {
    for (const option of ATTACHMENT_INTENT_OPTIONS) {
      expect(typeof option.label).toBe('string')
      expect(option.label.length).toBeGreaterThan(0)
    }
  })

  it('validates intent values', () => {
    expect(isValidAttachmentIntent('batch_rows')).toBe(true)
    expect(isValidAttachmentIntent('upload_to_page')).toBe(true)
    expect(isValidAttachmentIntent('prompt_context')).toBe(true)
    expect(isValidAttachmentIntent('media_source')).toBe(true)
    expect(isValidAttachmentIntent('auto')).toBe(false)
    expect(isValidAttachmentIntent('unknown')).toBe(false)
    expect(isValidAttachmentIntent('')).toBe(false)
    expect(isValidAttachmentIntent('bogus')).toBe(false)
  })

  it('does not append attachment_intent for auto', () => {
    const fd = new FormData()
    const appended = appendAttachmentIntentToFormData(fd, 'auto')
    expect(appended).toBe(false)
    expect(fd.has('attachment_intent')).toBe(false)
  })

  it('does not append attachment_intent for invalid values', () => {
    const fd = new FormData()
    const appended = appendAttachmentIntentToFormData(fd, 'bogus')
    expect(appended).toBe(false)
    expect(fd.has('attachment_intent')).toBe(false)
  })

  it('appends a valid user override', () => {
    const fd = new FormData()
    const appended = appendAttachmentIntentToFormData(fd, 'upload_to_page')
    expect(appended).toBe(true)
    expect(fd.get('attachment_intent')).toBe('upload_to_page')
  })
})
