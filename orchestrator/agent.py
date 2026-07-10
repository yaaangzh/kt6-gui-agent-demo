from domain.diagnostics import Diagnosis, Evidence


class OrchestratorAgent:
    """Coordinates a deterministic first-pass wireless troubleshooting flow."""

    def __init__(self):
        self.trace = []

    def run(self, query: str) -> Diagnosis:
        self.trace = []
        self._record("解析用户问题")
        context = self._parse_query(query)

        self._record("查询用户侧体验指标")
        client_evidence = self._collect_client_evidence(context)

        self._record("查询无线接入与射频指标")
        radio_evidence = self._collect_radio_evidence(context)

        self._record("关联网络侧事件")
        network_evidence = self._collect_network_evidence(context)

        self._record("汇总根因与处置建议")
        evidence = [*client_evidence, *radio_evidence, *network_evidence]
        return self._diagnose(context, evidence)

    def _record(self, event: str) -> None:
        self.trace.append(event)

    def _parse_query(self, query: str) -> dict:
        return {
            "raw_query": query.strip(),
            "user": "张三" if "张三" in query else "未知用户",
            "time_window": "昨天 09:00 前后" if "昨天" in query or "9" in query else "用户描述时间段",
        }

    def _collect_client_evidence(self, context: dict) -> list[Evidence]:
        return [
            Evidence("终端体验", f"{context['user']} 在 {context['time_window']} 上报网页打开慢、测速波动", "warning"),
            Evidence("终端状态", "终端在线，但出现重传率升高与短时速率下降", "warning"),
        ]

    def _collect_radio_evidence(self, context: dict) -> list[Evidence]:
        return [
            Evidence("AP 射频", "2.4GHz 信道利用率偏高，邻频干扰明显", "critical"),
            Evidence("漫游记录", "终端在两个 AP 间发生过一次低 RSSI 漫游", "warning"),
        ]

    def _collect_network_evidence(self, context: dict) -> list[Evidence]:
        return [
            Evidence("出口链路", "同时间段未发现出口拥塞或丢包告警", "info"),
            Evidence("认证/DHCP", "未发现认证失败、地址冲突或 DHCP 超时", "info"),
        ]

    def _diagnose(self, context: dict, evidence: list[Evidence]) -> Diagnosis:
        return Diagnosis(
            summary=f"初步判断：{context['user']} 在 {context['time_window']} 的网速慢更可能由无线侧干扰和弱信号漫游引起。",
            likely_causes=[
                "2.4GHz 信道繁忙导致空口竞争加剧",
                "终端处于覆盖边缘时发生低信号漫游，短时间影响吞吐",
                "暂未看到出口、认证或 DHCP 侧异常证据",
            ],
            evidence=evidence,
            recommendations=[
                "优先引导终端连接 5GHz/6GHz SSID，或开启频段引导策略",
                "检查相关 AP 的信道规划与发射功率，降低同邻频干扰",
                "对张三常驻位置补充覆盖热力图，确认是否存在弱覆盖区域",
                "若问题复现，采集终端 RSSI、SNR、MCS、重传率与 AP 空口利用率做二次定位",
            ],
        )
