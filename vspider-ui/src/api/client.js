// Centralized API / WebSocket base + fetch wrappers (Phase 1 consolidation).
// Configure at build time via VITE_API_BASE / VITE_WS_BASE.
// Defaults preserve the previously hardcoded host so behavior is unchanged.

const RAW_BASE = (import.meta.env && import.meta.env.VITE_API_BASE) || 'http://localhost:8000'
export const API_BASE = RAW_BASE.replace(/\/+$/, '')

const RAW_WS = (import.meta.env && import.meta.env.VITE_WS_BASE) || API_BASE.replace(/^http/, 'ws')
export const WS_BASE = RAW_WS.replace(/\/+$/, '')

export function apiUrl(path = '') {
  return `${API_BASE}${path}`
}

export function wsUrl(path = '') {
  return `${WS_BASE}${path}`
}

export function apiFetch(path, options) {
  return fetch(apiUrl(path), options)
}
