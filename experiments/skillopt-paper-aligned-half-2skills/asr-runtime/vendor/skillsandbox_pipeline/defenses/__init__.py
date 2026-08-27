"""Defense filters for SKILL.md prompt-injection preprocessing."""

from .legacy.opi_filter import OPIFilter
from .open_prompt_injection.datasentinel import OPIDataSentinelFilter
from .open_prompt_injection.pipeline import OPIPipelineFilter
from .open_prompt_injection.promptlocate import OPIPromptLocateFilter
from .prompt_armor.gpt_detector import PromptArmorFilter
from .struq.canonicalizer import StruQLiteFilter
from .struq.fixed import StruQFixedFilter

try:
    from .mcp_guard.adapter import MCPGuardFilter
except ImportError:
    MCPGuardFilter = None

try:
    from .agent_scan.adapter import AgentScanFilter
except ImportError:
    AgentScanFilter = None

try:
    from .SkillJect.adapter import SkillJectFilter
except ImportError:
    SkillJectFilter = None

try:
    from .PurpleLlama.adapter import LlamaFirewallFilter
except ImportError:
    LlamaFirewallFilter = None


def create_filter(name: str):
    """Create a filter instance by CLI-facing name."""
    normalized = name.strip().lower()
    if normalized in {"datasentinel_fixed", "datasentinel", "opi_datasentinel"}:
        return OPIDataSentinelFilter(method_name=normalized)
    if normalized == "opi_promptlocate":
        return OPIPromptLocateFilter(method_name=normalized)
    if normalized == "opi_pipeline":
        return OPIPipelineFilter(method_name=normalized)
    if normalized in {"prompt_armor", "prompt_armor_gpt"}:
        return PromptArmorFilter(method_name=normalized)
    if normalized in {"struq_fixed", "struq"}:
        return StruQFixedFilter(method_name=normalized)
    if normalized in {"opi_rules_only", "opi_detect_drop", "opi_detect_locate"}:
        return OPIFilter(mode=normalized)
    if normalized in {"struq_lite_strict", "struq_lite_relaxed"}:
        return StruQLiteFilter(mode=normalized)
    if normalized == "mcp_guard":
        if MCPGuardFilter is None:
            raise ValueError("mcp_guard module not installed")
        return MCPGuardFilter(method_name=normalized)
    if normalized == "agent_scan":
        if AgentScanFilter is None:
            raise ValueError("agent_scan module not installed")
        return AgentScanFilter(method_name=normalized)
    if normalized in {"skillject", "skillject_scan"}:
        if SkillJectFilter is None:
            raise ValueError("SkillJect module not installed")
        return SkillJectFilter(method_name=normalized)
    if normalized in {"llama_firewall", "llamafirewall", "prompt_guard"}:
        if LlamaFirewallFilter is None:
            raise ValueError("PurpleLlama module not installed")
        return LlamaFirewallFilter(method_name=normalized)
    raise ValueError(f"Unknown filter: {name}")


__all__ = [
    "OPIDataSentinelFilter",
    "OPIPipelineFilter",
    "OPIPromptLocateFilter",
    "PromptArmorFilter",
    "OPIFilter",
    "StruQFixedFilter",
    "StruQLiteFilter",
    "MCPGuardFilter",
    "AgentScanFilter",
    "SkillJectFilter",
    "LlamaFirewallFilter",
    "create_filter",
]
