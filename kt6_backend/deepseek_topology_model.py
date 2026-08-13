"""Deprecated names for the provider-neutral topology model adapter.

New code should import :mod:`kt6_backend.openai_compatible_topology_model`.
The aliases keep older deployments import-compatible without selecting or
special-casing DeepSeek at runtime.
"""

from .openai_compatible_topology_model import (
    OPENAI_COMPATIBLE_TOPOLOGY_PROMPT_VERSION,
    OpenAICompatibleTopologyCall,
    OpenAICompatibleTopologySemanticAdapter,
)

DEEPSEEK_TOPOLOGY_PROMPT_VERSION = OPENAI_COMPATIBLE_TOPOLOGY_PROMPT_VERSION
DeepSeekTopologyCall = OpenAICompatibleTopologyCall
DeepSeekTopologySemanticAdapter = OpenAICompatibleTopologySemanticAdapter

__all__ = [
    "DEEPSEEK_TOPOLOGY_PROMPT_VERSION",
    "DeepSeekTopologyCall",
    "DeepSeekTopologySemanticAdapter",
]
