from __future__ import annotations

from pathlib import Path


SOM_JS = Path("visual_web_agent/som_inject_v6.js")


def _source() -> str:
    return SOM_JS.read_text(encoding="utf-8")


def test_som_icon_buttons_do_not_require_cursor_pointer() -> None:
    js = _source()
    assert "composerIconCandidate" in js
    # The OR-chain must accept icon-shaped buttons even without
    # cursor:pointer. The set of acceptance signals grew over time
    # (composer → composer + toolbar → composer + toolbar + explicit name),
    # so assert each token is present in the same expression rather than
    # pinning the literal order.
    assert "_isComposerIconCandidate" in js
    # Locate the OR-chain that ungates SoM marking.
    or_chains = [
        line for line in js.splitlines()
        if "s.cursor === 'pointer'" in line
        and "composerIconCandidate" in line
    ]
    assert or_chains, "no SoM ungate OR-chain found combining cursor:pointer + composerIconCandidate"
    target_line = or_chains[0]
    # Every load-bearing token in the chain
    for token in (
        "s.cursor === 'pointer'",
        "composerIconCandidate",
        "explicitIconName",
    ):
        assert token in target_line, f"missing {token!r} from chain: {target_line!r}"


def test_som_rightmost_composer_icon_is_named_send() -> None:
    js = _source()
    assert "_isRightmostComposerIcon" in js
    assert "return '[send]'" in js
    assert "cx >= er.left + er.width * 0.72" in js


def test_som_icon_semantics_cover_chat_controls() -> None:
    js = _source()
    for marker in (
        "paper[-_ ]?plane",
        "arrow[-_ ]?up",
        "麦克风",
        "附件",
        "code",
    ):
        assert marker in js
