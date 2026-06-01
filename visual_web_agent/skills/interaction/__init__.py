"""Interaction-oriented runtime skills."""

from .demoqa_droppable_slider import DemoQADroppableSliderSkill
from .demoqa_slider import DemoQASliderSkill
from .internet_hovers import InternetHoversSkill
from .modal_dialogs import ModalDialogSkill
from .reactrouter_docs import ReactRouterDocsSkill
from .selectorshub_shadow_iframe import SelectorsHubShadowIframeSkill
from .wikipedia_new_tabs import WikipediaNewTabsSkill

__all__ = [
    "DemoQADroppableSliderSkill",
    "DemoQASliderSkill",
    "InternetHoversSkill",
    "ModalDialogSkill",
    "ReactRouterDocsSkill",
    "SelectorsHubShadowIframeSkill",
    "WikipediaNewTabsSkill",
]
