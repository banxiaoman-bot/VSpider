export const formatFailureFixtureRows = (rows = [], limit = 5) => (Array.isArray(rows) ? rows : [])
  .slice(0, limit)
  .map((item) => `${item?.name || 'unknown'} (${item?.count ?? item?.failed_count ?? 0})`)
  .join(', ')

export const buildFailureFixtureBatchReplaySummaryText = ({
  report = {},
  summary = {},
  artifact = {},
  failedChecks = [],
  topPrimaryFailures = [],
  topFailureCategories = [],
  topFailedChecks = [],
  failedItems = [],
} = {}) => {
  const lines = [
    '# Failure Fixture Batch Replay Summary',
    '',
    `Status: ${summary.status || (report.passed ? 'passed' : 'failed')}`,
    `Fixtures: ${report.fixture_count || 0}`,
    `Passed: ${report.passed_count || 0}`,
    `Failed: ${report.failed_count || 0}`,
    `Failed checks: ${(Array.isArray(failedChecks) ? failedChecks : []).length}`,
  ]
  if (summary.recommended_focus) lines.push(`Focus: ${summary.recommended_focus}`)
  const topPrimary = formatFailureFixtureRows(topPrimaryFailures)
  const topCategories = formatFailureFixtureRows(topFailureCategories)
  const topChecks = formatFailureFixtureRows(topFailedChecks)
  if (topPrimary) lines.push(`Top primary failures: ${topPrimary}`)
  if (topCategories) lines.push(`Top failure categories: ${topCategories}`)
  if (topChecks) lines.push(`Top failed checks: ${topChecks}`)
  if (artifact.url) lines.push(`Artifact: ${artifact.url}`)
  const items = Array.isArray(failedItems) ? failedItems : []
  if (items.length) {
    lines.push('', 'Failed fixtures:')
    for (const item of items.slice(0, 8)) {
      const checks = Array.isArray(item?.failed_checks) && item.failed_checks.length
        ? item.failed_checks.slice(0, 3).join(', ')
        : `${item?.failed_check_count || 0} failed checks`
      lines.push(`- ${item?.name || 'fixture'}: ${item?.primary_failure || 'unknown'} · ${checks}`)
    }
  }
  return `${lines.join('\n')}\n`
}
