from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
APP_VUE = ROOT / "vspider-ui" / "src" / "App.vue"
UI_ROOT = ROOT / "vspider-ui"


def test_app_submits_merged_authoritative_urls_payload() -> None:
    src = APP_VUE.read_text(encoding="utf-8")

    assert "buildAuthoritativeUrlsPayload," in src
    assert "const normalizedTargetUrl = url.value.trim()" in src
    assert "const normalizedExtraUrls = extraUrls.value.trim()" in src
    assert (
        "const authoritativeUrls = "
        "buildAuthoritativeUrlsPayload(normalizedTargetUrl, normalizedExtraUrls)"
    ) in src
    assert "formData.append('urls', JSON.stringify(authoritativeUrls))" in src
    assert "formData.append('urls', extraUrls.value.trim())" not in src


def test_build_authoritative_urls_payload_order_and_dedupe() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    script = textwrap.dedent(
        """
        import { buildAuthoritativeUrlsPayload, parseUrlList } from './src/composables/useTaskSubmit.js'

        const cases = [
          {
            actual: buildAuthoritativeUrlsPayload(
              'https://a.test',
              'https://b.test\\nhttps://c.test, https://a.test/'
            ),
            expected: ['https://a.test', 'https://b.test', 'https://c.test'],
          },
          {
            actual: buildAuthoritativeUrlsPayload(
              '',
              'https://x.test; https://y.test'
            ),
            expected: ['https://x.test', 'https://y.test'],
          },
          {
            actual: parseUrlList('["https://json-a.test", "https://json-b.test"]'),
            expected: ['https://json-a.test', 'https://json-b.test'],
          },
        ]

        for (const item of cases) {
          if (JSON.stringify(item.actual) !== JSON.stringify(item.expected)) {
            throw new Error(`${JSON.stringify(item.actual)} != ${JSON.stringify(item.expected)}`)
          }
        }
        console.log(JSON.stringify(cases.map((item) => item.actual)))
        """
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=UI_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == [
        ["https://a.test", "https://b.test", "https://c.test"],
        ["https://x.test", "https://y.test"],
        ["https://json-a.test", "https://json-b.test"],
    ]
