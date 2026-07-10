from __future__ import annotations

import re
from typing import Any


class IntentAgent:
    def parse(self, query: str) -> dict[str, Any]:
        ap_match = re.search(r"AP\s*([0-9]+)", query, flags=re.IGNORECASE)
        if "离线" in query or "掉线" in query:
            ap_name = f"AP{ap_match.group(1)}" if ap_match else "未知AP"
            return {
                "intent": "diagnose_ap_offline",
                "scenario": "AP 离线排障",
                "preferred_playbook_id": "ap_offline_diagnosis",
                "entities": {
                    "ap_name": ap_name,
                    "ap_id": f"ap_{int(ap_match.group(1)):03d}" if ap_match else None,
                    "time_range": "昨晚" if "昨晚" in query else "用户描述时间",
                    "symptom": "AP离线",
                },
                "task_goal": "分析 AP 离线原因并给出恢复建议",
            }
        return {
            "intent": "diagnose_user_experience",
            "scenario": "用户体验保障",
            "preferred_playbook_id": "user_experience_assurance",
            "entities": {
                "user": "张三" if "张三" in query else None,
                "time_range": "昨天上午9:00" if "昨天" in query or "9" in query else "用户描述时间",
                "symptom": "网速慢" if "网速" in query else None,
            },
            "task_goal": "分析用户体验劣化原因并给出可执行优化建议",
        }


class DiagnosisAgent:
    def infer_root_cause(
        self,
        user_experience: dict[str, Any],
        associated_device: dict[str, Any],
        radio_metrics: dict[str, Any],
        negative_checks: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = [
            f"{user_experience['user']} 在 {user_experience['time_range']} 接入 {associated_device['ap_name']}",
            f"{associated_device['ap_name']} 工作在 {associated_device['band']} 信道 {associated_device['channel']}",
            f"同信道邻居 AP 数量为 {radio_metrics['co_channel_neighbor_count']}",
            f"信道利用率 {radio_metrics['channel_utilization']}",
            f"用户吞吐 {user_experience['throughput']}，重传率 {user_experience['retransmission_rate']}",
            f"出口、认证、DHCP 检查结果：{negative_checks['summary']}",
        ]
        return {
            "root_cause": "co_channel_interference",
            "root_cause_text": "AP1 同频邻居干扰",
            "confidence": 0.86,
            "affected_object": {
                "type": "ap",
                "id": associated_device["ap_id"],
                "name": associated_device["ap_name"],
            },
            "evidence": evidence,
            "reasoning_summary": "张三在故障时间段接入 AP1，用户侧表现为吞吐下降和重传率升高；AP1 同信道邻居数量高且信道利用率高，同时出口、认证、DHCP 未见异常，因此将 AP1 同频邻居干扰判定为主因。",
        }

    def recommend_solutions(self, root_cause: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "solution_id": "rf_optimization",
                "name": "射频调优",
                "execution_mode": "one_click",
                "risk_level": "high-risk",
                "description": "系统自动分析站点1/1F AP 射频调优策略，并适时自动下发。",
            },
            {
                "solution_id": "channel_set_optimization",
                "name": "优化信道集配置",
                "execution_mode": "manual",
                "risk_level": "medium-risk",
                "description": "在 5G 调优信道集中增加 149、153、157、161、165。",
            },
        ]

    def infer_ap_offline_root_cause(
        self,
        ap_status: dict[str, Any],
        switch_port: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = [
            f"{ap_status['ap_name']} 当前状态为 {ap_status['status']}",
            f"最后心跳时间：{ap_status['last_seen']}",
            f"接入交换机端口：{switch_port['switch_name']} {switch_port['port']}",
            f"端口状态：{switch_port['link_status']}，PoE 状态：{switch_port['poe_status']}",
        ]
        return {
            "root_cause": "poe_power_loss",
            "root_cause_text": "交换机端口 PoE 供电异常导致 AP 离线",
            "confidence": 0.82,
            "affected_object": {
                "type": "ap",
                "id": ap_status["ap_id"],
                "name": ap_status["ap_name"],
            },
            "evidence": evidence,
            "reasoning_summary": "AP 无心跳且有线端口链路存在异常，PoE 状态异常，优先判定为交换机端口供电问题。",
        }

    def recommend_ap_recovery_solutions(self, root_cause: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "solution_id": "restart_poe_port",
                "name": "重启 PoE 端口",
                "execution_mode": "manual_confirm",
                "risk_level": "medium-risk",
                "description": "确认现场无施工风险后，重启 AP 所在交换机端口 PoE 供电。",
            },
            {
                "solution_id": "dispatch_field_check",
                "name": "派单现场检查",
                "execution_mode": "manual",
                "risk_level": "low-risk",
                "description": "检查 AP 网线、供电模块和交换机端口状态。",
            },
        ]
