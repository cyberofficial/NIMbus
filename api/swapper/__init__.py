"""Model swapper and NIM server type swapper module.

Provides:
- Dynamic model swapping via <modelswap:model> tags
- Dynamic NIM server type switching via <nimserver:stream|buffer> tags
- Adaptive rate limit reset via <nimrpm:reset> tag
- Effort level control via <nimeffort:level> tags
"""

from api.swapper.manager import ModelSwapManager
from api.swapper.nimserver import NimServerManager
from api.swapper.parser import (
    extract_modelswap_tag,
    extract_nimeffort_tag,
    extract_nimserver_tag,
    get_modelswap_model,
    get_nimserver_type,
    is_modelswap_clear_tag,
    is_modelswap_message,
    is_nimeffort_tag,
    is_nimhelp_tag,
    is_nimserver_clear_tag,
    is_nimserver_message,
    is_nimrpm_reset_tag,
)
from api.swapper.validator import resolve_model_name, validate_and_test_model

__all__ = [
    "ModelSwapManager",
    "NimServerManager",
    "extract_modelswap_tag",
    "extract_nimeffort_tag",
    "extract_nimserver_tag",
    "get_modelswap_model",
    "get_nimserver_type",
    "is_modelswap_clear_tag",
    "is_modelswap_message",
    "is_nimeffort_tag",
    "is_nimhelp_tag",
    "is_nimserver_clear_tag",
    "is_nimserver_message",
    "is_nimrpm_reset_tag",
    "resolve_model_name",
    "validate_and_test_model",
]