from dataclasses import dataclass
from typing import Iterable, List


@dataclass(frozen=True)
class Evidence:
    source: str
    detail: str
    severity: str = "info"


@dataclass(frozen=True)
class Diagnosis:
    summary: str
    likely_causes: List[str]
    evidence: List[Evidence]
    recommendations: List[str]

    def to_text(self) -> str:
        lines = [self.summary, ""]
        lines.append("可能原因:")
        lines.extend(f"- {item}" for item in self.likely_causes)
        lines.append("")
        lines.append("证据:")
        lines.extend(f"- [{item.severity}] {item.source}: {item.detail}" for item in self.evidence)
        lines.append("")
        lines.append("建议动作:")
        lines.extend(f"- {item}" for item in self.recommendations)
        return "\n".join(lines)


def format_trace(events: Iterable[str]) -> str:
    return "\n".join(f"{index}. {event}" for index, event in enumerate(events, start=1))
