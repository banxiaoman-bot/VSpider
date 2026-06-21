"""VSpider Actions package — split from monolith actions.py"""

from ._base import (
    ActionContext,
    ActionHandler,
    ActionRegistry,
    UnknownActionError,
    _click_locator_with_js_fallback,
    _is_navigation_context_destroyed,
    _resolve_env_placeholders,
    _scroll_largest_container,
)

try:
    from ..browser_env import ActionExecutionError
except ImportError:
    from browser_env import ActionExecutionError  # type: ignore[no-redef]

# Handler modules — importing triggers @ActionRegistry.register
from .navigation import (
    DoneHandler, WaitHandler, CloseTabHandler,
    GotoHandler, ScrollHandler, SmoothScrollHandler,
)
from .find_and_form import (
    FindTextHandler, FormSetHandler,
    _parse_form_set_payload, _click_visible_text_option,
    _form_set_bound_control_v2, _form_set_with_frames,
    _FORM_SET_NOT_FOUND_REASONS,
)
from .click_and_type import (
    ClickHandler, ClickNewTabHandler, FetchLinkContentHandler,
    TypeHandler, HoverHandler,
)
from .keys_select_drag import (
    PressKeyHandler, SelectHandler,
    DragAndDropHandler, RemoveElementHandler,
)
from .page_ops import (
    ClickPointHandler, NextPageHandler,
    DismissConsentHandler,
    ClickTextHandler, HoverAndClickHandler,
)
from .row_and_tree import (
    RowActionHandler, ExtractRowHandler, TreeCheckHandler,
    SetPromptResponseHandler, SwitchTabHandler,
)
from .data_io import (
    ExtractLinkHandler, DownloadImageHandler,
    UploadHandler, SaveToMemoryHandler,
    ChatExtractHandler, ChatSubmitHandler,
    _persist_extracted_link,
)

# Out-of-package capability handlers — decorator side-effects
try:
    from .. import page_to_markdown_action as _page_to_markdown_action  # noqa: F401
except ImportError:
    import page_to_markdown_action as _page_to_markdown_action  # type: ignore  # noqa: F401
try:
    from .. import resume_run_action as _resume_run_action  # noqa: F401
except ImportError:
    import resume_run_action as _resume_run_action  # type: ignore  # noqa: F401
try:
    from .. import vscroll_capture_action as _vscroll_capture_action  # noqa: F401
except ImportError:
    import vscroll_capture_action as _vscroll_capture_action  # type: ignore  # noqa: F401
try:
    from .. import snapshot_actions as _snapshot_actions  # noqa: F401
except ImportError:
    import snapshot_actions as _snapshot_actions  # type: ignore  # noqa: F401
try:
    from .. import search_nav_action as _search_nav_action  # noqa: F401
except ImportError:
    import search_nav_action as _search_nav_action  # type: ignore  # noqa: F401

__all__ = [
    "ActionContext", "ActionHandler", "ActionRegistry", "UnknownActionError", "ActionExecutionError",
    "_click_locator_with_js_fallback", "_is_navigation_context_destroyed",
    "_resolve_env_placeholders", "_scroll_largest_container",
    "DoneHandler", "WaitHandler", "CloseTabHandler",
    "GotoHandler", "ScrollHandler", "SmoothScrollHandler",
    "FindTextHandler", "FormSetHandler",
    "_parse_form_set_payload", "_click_visible_text_option",
    "_form_set_bound_control_v2", "_form_set_with_frames", "_FORM_SET_NOT_FOUND_REASONS",
    "ClickHandler", "ClickNewTabHandler", "FetchLinkContentHandler",
    "TypeHandler", "HoverHandler",
    "PressKeyHandler", "SelectHandler",
    "DragAndDropHandler", "RemoveElementHandler",
    "ClickPointHandler", "NextPageHandler",
    "DismissConsentHandler",
    "ClickTextHandler", "HoverAndClickHandler",
    "RowActionHandler", "ExtractRowHandler", "TreeCheckHandler",
    "SetPromptResponseHandler", "SwitchTabHandler",
    "ExtractLinkHandler", "DownloadImageHandler",
    "UploadHandler", "SaveToMemoryHandler",
    "ChatExtractHandler", "ChatSubmitHandler",
    "_persist_extracted_link",
]
