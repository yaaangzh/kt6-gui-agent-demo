from __future__ import annotations

import threading
import time
from typing import Any

from .agent import DiagnosisAgent, IntentAgent
from .memory import SQLiteMemoryStore
from .models import TASK_STATES, Task
from .playbook_loader import PlaybookLoader
from .router import PlaybookRouter
from .tool_registry import ToolRegistry
from .tools import MockBusinessTools


class KT6Runtime:
    def __init__(
        self,
        tools: MockBusinessTools,
        playbooks: PlaybookLoader,
        event_delay: float = 0.45,
        memory: SQLiteMemoryStore | None = None,
    ):
        self.intent_agent = IntentAgent()
        self.diagnosis_agent = DiagnosisAgent()
        self.tools = ToolRegistry(tools)
        self.playbooks = playbooks
        self.router = PlaybookRouter(playbooks)
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()
        self.event_delay = event_delay
        self.memory = memory

    def create_task(self, query: str) -> Task:
        task = Task(query=query)
        with self.lock:
            self.tasks[task.task_id] = task
        self._persist_task(task)
        threading.Thread(target=self._run_diagnosis, args=(task.task_id,), daemon=True).start()
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self.lock:
            return self.tasks.get(task_id)

    def get_events(self, task_id: str, since: int = 0) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        if not task:
            return []
        with self.lock:
            return [event.to_dict() for event in task.events if event.id > since]

    def execute_action(self, task_id: str, action: str, payload: dict[str, Any]) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        intent = task.context.get("intent", {})
        playbook_id = intent.get("playbook_id", "user_experience_assurance")
        playbook = self.playbooks.load(playbook_id)
        action_spec = playbook.actions.get(action)
        if not action_spec or task.state != action_spec["allowed_state"]:
            return False
        scene_validation = self._validate_scene_for_action(task)
        if scene_validation and not scene_validation["valid"]:
            self._start_replan(task, scene_validation)
            return True
        threading.Thread(target=self._run_action_playbook, args=(task_id, action_spec, payload), daemon=True).start()
        return True

    def _emit(self, task: Task, event_type: str, **payload: Any) -> None:
        with self.lock:
            event = task.append_event(event_type, payload)
        if self.memory:
            self.memory.save_event(event)
            self.memory.save_task(task)

    def _set_state(self, task: Task, state: str) -> None:
        if state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {state}")
        with self.lock:
            task.state = state
            if self.memory:
                self.memory.save_task(task)
        self._emit(task, "runtime_state", runtime_state=state)

    def _persist_task(self, task: Task) -> None:
        if self.memory:
            self.memory.save_task(task)

    def _record_perception(self, task: Task, topology: dict[str, Any]) -> None:
        perception_meta = topology.get("perception_meta", {})
        focus = topology.get("focus", {})
        task.context["topology"] = topology
        task.context["ui_perception"] = topology["ui_perception"]
        task.context["perception_meta"] = perception_meta
        task.context["scene_ref"] = {
            "scene_key": perception_meta.get("scene_key"),
            "revision": perception_meta.get("scene_revision", 0),
            "target_ids": focus.get("target_ids", []),
        }
        self._persist_task(task)

    def _validate_scene_for_action(self, task: Task) -> dict[str, Any] | None:
        scene_ref = task.context.get("scene_ref")
        if not scene_ref or not scene_ref.get("scene_key"):
            return None
        validation = self.tools.call("topology.validate_scene", scene_ref=scene_ref)
        topology = validation["topology"]
        current_meta = topology["perception_meta"]
        previous_revision = scene_ref.get("revision", 0)
        current_revision = current_meta["scene_revision"]

        if validation["valid"]:
            self._record_perception(task, topology)
            if current_revision != previous_revision:
                action = "重新绑定目标坐标后继续执行" if validation.get("rebased") else "变化与当前目标无关，继续执行"
                self._emit(
                    task,
                    "topology_changed",
                    topology=topology,
                    perception_meta=current_meta,
                    topology_changes=validation["changes"],
                    action_allowed=True,
                    message=f"检测到拓扑版本变化：{validation['changes']['summary']}；{action}。",
                    gui_action=f"Topology Sync：{validation['changes']['summary']}，{action}",
                )
        return validation

    def _start_replan(self, task: Task, validation: dict[str, Any]) -> None:
        topology = validation["topology"]
        self._record_perception(task, topology)
        self._set_state(task, "replanning")
        self._emit(
            task,
            "topology_changed",
            topology=topology,
            perception_meta=topology["perception_meta"],
            topology_changes=validation["changes"],
            action_allowed=False,
            invalidate_solutions=True,
            message=(
                f"执行前检测到目标拓扑变化：{validation['changes']['summary']}。"
                "旧方案已失效，Runtime 正在重新感知和生成方案。"
            ),
            scene={"phase": "Topology Changed", "headline": "目标拓扑已变化，正在重新分析", "progress": 12},
            gui_action="Topology Sync：目标拓扑变化，撤销旧方案并重新分析",
        )
        threading.Thread(target=self._run_diagnosis, args=(task.task_id,), daemon=True).start()

    def _checkpoint(self, task: Task, step_id: str) -> str | None:
        if not self.memory:
            return None
        checkpoint_id = self.memory.save_checkpoint(task, step_id)
        task.context["last_checkpoint_id"] = checkpoint_id
        self._persist_task(task)
        return checkpoint_id

    def _remember_completion(self, task: Task) -> None:
        if not self.memory:
            return
        entities = task.context.get("entities", {})
        associated_device = task.context.get("associated_device", {})
        root_cause = task.context.get("root_cause", {})
        recovery = task.context.get("recovery", {})
        self.memory.remember(
            scope="business_incident",
            subject=f"{entities.get('user', 'unknown')}:{associated_device.get('ap_id', 'unknown')}",
            kind="wireless_user_experience_resolution",
            payload={
                "task_id": task.task_id,
                "user": entities.get("user"),
                "time_range": entities.get("time_range"),
                "symptom": entities.get("symptom"),
                "ap_id": associated_device.get("ap_id"),
                "root_cause": root_cause.get("root_cause"),
                "root_cause_text": root_cause.get("root_cause_text"),
                "recovery": recovery,
            },
        )

    def _pause(self) -> None:
        if self.event_delay > 0:
            time.sleep(self.event_delay)

    def _run_diagnosis(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        try:
            intent = self.intent_agent.parse(task.query)
            route = self.router.route(task.query, intent)
            intent["playbook_id"] = route.playbook.scenario_id
            intent["route"] = {
                "confidence": route.confidence,
                "reason": route.reason,
            }
            task.context["intent"] = intent
            task.context["entities"] = intent["entities"]
            playbook = route.playbook
            task.context["playbook"] = {"scenario_id": playbook.scenario_id, "name": playbook.name}
            task.context["route_decision"] = route.to_dict()
            task.context["playbook_steps"] = [step for step in playbook.steps if step["id"] != "create_context"]
            self._persist_task(task)

            self._emit(task, "chat", role="user", title="用户", message=task.query)
            missing_slots = self._missing_required_slots(playbook.required_slots, intent)
            if missing_slots:
                task.context["missing_slots"] = missing_slots
                self._persist_task(task)
                self._set_state(task, "waiting_input")
                self._emit(
                    task,
                    "clarification",
                    title="需要补充信息",
                    message=self._clarification_message(missing_slots),
                    missing_slots=missing_slots,
                    playbook=task.context["playbook"],
                    route_decision=task.context.get("route_decision"),
                    scene={"phase": "Input Required", "headline": self._clarification_message(missing_slots), "progress": 3},
                    gui_action=f"缺少必要输入：{', '.join(item['label'] for item in missing_slots)}",
                    step_labels=[item["name"] for item in task.context.get("playbook_steps", [])],
                )
                return
            for step in playbook.steps:
                self._execute_diagnosis_step(task, step)
        except Exception as exc:
            self._set_state(task, "failed")
            self._emit(task, "runtime", title="Runtime", message=f"任务执行失败：{exc}")

    def _missing_required_slots(self, required_slots: list[str], intent: dict[str, Any]) -> list[dict[str, str]]:
        entities = intent.get("entities", {})
        labels = {
            "user": "用户姓名或账号",
            "ap_id": "AP 编号",
            "time_range": "故障时间",
            "symptom": "故障现象",
        }
        missing = []
        for slot in required_slots:
            value = entities.get(slot)
            if value is None or value == "" or str(value).startswith("未知"):
                missing.append({"slot": slot, "label": labels.get(slot, slot)})
        return missing

    def _clarification_message(self, missing_slots: list[dict[str, str]]) -> str:
        labels = "、".join(item["label"] for item in missing_slots)
        return f"当前输入缺少{labels}，请补充后重新提交。"

    def _execute_diagnosis_step(self, task: Task, step: dict[str, Any]) -> None:
        step_id = step["id"]
        self._set_state(task, step["state"])

        if step_id == "create_context":
            self._emit(
                task,
                "runtime",
                title="Runtime",
                message=f"创建任务，写入 Conversation / Task / Business Context，并加载“{task.context['playbook']['name']}”Playbook。",
                playbook=task.context["playbook"],
                route_decision=task.context.get("route_decision"),
                scene={"phase": "Step 0 / Runtime Planning", "headline": "创建任务上下文，准备驱动左侧 GUI", "progress": 5},
                gui_action="创建任务上下文，准备驱动左侧 GUI",
                step_labels=[item["name"] for item in task.context.get("playbook_steps", [])],
            )
            self._pause()
            return

        if step_id == "locate_ap_topology":
            entities = task.context["entities"]
            topology = self.tools.call(step["tool"], ap_id=entities["ap_id"])
            self._record_perception(task, topology)
            self._emit(
                task,
                "ui",
                view="experience",
                topology=topology,
                perception_meta=topology["perception_meta"],
                topology_changes=topology["topology_changes"],
                actions=step["ui_actions"],
                metrics={"focus": f"{entities['ap_name']} / {entities['ap_id']}", "ap": entities["ap_name"], "experience": "离线"},
                scene={"phase": "Step 1 / GUI Navigate", "headline": f"左侧定位 {entities['ap_name']} 所在站点1 / 1F 拓扑", "progress": 18},
                gui_action=f"左侧跳转：定位 {entities['ap_name']} 所在网络拓扑",
                step={"index": 1, "status": "running"},
            )
            intent = task.context["intent"]
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message=f"识别意图：{intent['scenario']}。\n已抽取实体：AP={entities['ap_name']}，时间={entities['time_range']}，症状={entities['symptom']}。\n左侧正在定位 AP 所在拓扑。",
            )
            self._pause()
            self._emit(task, "step", index=1, status="done")
            return

        if step_id == "locate_user_topology":
            entities = task.context["entities"]
            topology = self.tools.call(step["tool"], user=entities["user"])
            self._record_perception(task, topology)
            self._emit(
                task,
                "ui",
                view="experience",
                topology=topology,
                perception_meta=topology["perception_meta"],
                topology_changes=topology["topology_changes"],
                actions=step["ui_actions"],
                metrics={"focus": "user_zhangsan", "user": entities["user"], "experience": "定位中"},
                scene={"phase": "Step 1 / GUI Navigate", "headline": "左侧立即跳转张三所在站点1 / 1F 网络拓扑", "progress": 18},
                gui_action="左侧跳转：站点1 / 1F 张三所在网络拓扑",
                step={"index": 1, "status": "running"},
            )
            intent = task.context["intent"]
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message=f"识别意图：{intent['scenario']}。\n已抽取实体：用户={entities['user']}，时间={entities['time_range']}，症状={entities['symptom']}。\n左侧正在跳转张三所在网络拓扑。",
            )
            self._emit(
                task,
                "runtime",
                title="UI Perception",
                message=(
                    f"界面感知缓存：{topology['perception_meta']['cache_status'].upper()}，"
                    f"Scene revision={topology['perception_meta']['scene_revision']}，"
                    f"耗时={topology['perception_meta']['perception_ms']}ms。\n"
                    f"识别模式：{topology['ui_perception']['mode']}；"
                    f"对象数：{topology['ui_perception']['object_count']}；"
                    f"选择原因：{topology['perception_decision']['reason']}；"
                    "业务对象绑定：张三 -> user_zhangsan，AP1 -> ap_001。"
                ),
            )
            self._pause()
            self._emit(task, "step", index=1, status="done")
            return

        if step_id == "analyze_user_and_ap":
            entities = task.context["entities"]
            user_exp = self.tools.call("experience.query_user_metrics", user=entities["user"], time_range=entities["time_range"])
            associated_device = self.tools.call("wireless.query_associated_ap", user=entities["user"], time_range=entities["time_range"])
            task.context["user_experience"] = user_exp
            task.context["associated_device"] = associated_device
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                metrics={
                    "focus": "AP1 / ap_001",
                    "ap": associated_device["ap_name"],
                    "channel": f"{associated_device['band']} CH{associated_device['channel']}",
                    "experience": "劣化",
                },
                scene={"phase": "Step 2 / UI Perception", "headline": "识别拓扑对象：张三接入 AP1，并绑定业务对象 ap_001", "progress": 36},
                gui_action="UI Perception：识别张三接入链路并绑定 AP1",
                step={"index": 2, "status": "running"},
            )
            self._emit(
                task,
                "runtime",
                title="UI Perception + Grounding",
                message="左侧拓扑识别完成：张三接入 AP1。\n已将拓扑节点 AP1 绑定到业务对象 ap_001，并读取用户体验指标。",
            )
            self._pause()
            self._emit(task, "step", index=2, status="done")
            return

        if step_id == "infer_root_cause":
            entities = task.context["entities"]
            associated_device = task.context["associated_device"]
            radio_metrics = self.tools.call("radio.query_metrics", ap_id=associated_device["ap_id"])
            negative_checks = self.tools.call("network.query_negative_checks", user=entities["user"], time_range=entities["time_range"])
            root_cause = self.diagnosis_agent.infer_root_cause(
                task.context["user_experience"],
                associated_device,
                radio_metrics,
                negative_checks,
            )
            solutions = self.diagnosis_agent.recommend_solutions(root_cause)
            task.context["radio_metrics"] = radio_metrics
            task.context["negative_checks"] = negative_checks
            task.context["root_cause"] = root_cause
            task.context["solutions"] = solutions
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                metrics={"neighbor": "6 个同信道邻居", "experience": "吞吐低 / 重传高"},
                scene={"phase": "Step 3 / Root Cause", "headline": "联动射频指标：AP1 同频邻居干扰被高亮", "progress": 56},
                gui_action="Topology Sync：高亮 AP1 与同频邻居干扰关系",
                step={"index": 3, "status": "running"},
            )
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message="STEP 1 用户指标分析完成。\nSTEP 2 关联设备问题分析完成。\nSTEP 3 问题原因分析完成。\n\n证据链：\n- "
                + "\n- ".join(root_cause["evidence"])
                + f"\n\n判断根因：{root_cause['root_cause_text']} 引起用户体验劣化。",
            )
            self._pause()
            self._emit(task, "step", index=3, status="done")
            return

        if step_id == "analyze_ap_status":
            entities = task.context["entities"]
            ap_status = self.tools.call("wireless.query_ap_status", ap_id=entities["ap_id"], time_range=entities["time_range"])
            switch_port = self.tools.call("wireless.query_switch_port", ap_id=entities["ap_id"])
            task.context["ap_status"] = ap_status
            task.context["switch_port"] = switch_port
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                metrics={
                    "focus": f"{ap_status['ap_name']} / {ap_status['ap_id']}",
                    "ap": ap_status["ap_name"],
                    "channel": f"{switch_port['switch_name']} {switch_port['port']}",
                    "experience": "AP 离线",
                },
                scene={"phase": "Step 2 / Device Check", "headline": "读取 AP 心跳、交换机端口与 PoE 状态", "progress": 38},
                gui_action="设备状态分析：查询 AP 心跳和交换机端口",
                step={"index": 2, "status": "running"},
            )
            self._emit(
                task,
                "runtime",
                title="AP Status",
                message=f"{ap_status['ap_name']} 当前 {ap_status['status']}，最后心跳 {ap_status['last_seen']}；端口 {switch_port['port']} PoE 状态 {switch_port['poe_status']}。",
            )
            self._pause()
            self._emit(task, "step", index=2, status="done")
            return

        if step_id == "infer_ap_offline_root_cause":
            root_cause = self.diagnosis_agent.infer_ap_offline_root_cause(
                task.context["ap_status"],
                task.context["switch_port"],
            )
            solutions = self.diagnosis_agent.recommend_ap_recovery_solutions(root_cause)
            task.context["root_cause"] = root_cause
            task.context["solutions"] = solutions
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                metrics={"neighbor": "PoE fault", "experience": "AP 离线"},
                scene={"phase": "Step 3 / Root Cause", "headline": "判断 AP 离线根因为交换机端口 PoE 异常", "progress": 58},
                gui_action="Root Cause：高亮 AP3 PoE 异常",
                step={"index": 3, "status": "running"},
            )
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message="STEP 1 AP 拓扑定位完成。\nSTEP 2 AP 状态与交换机端口分析完成。\nSTEP 3 AP 离线原因分析完成。\n\n证据链：\n- "
                + "\n- ".join(root_cause["evidence"])
                + f"\n\n判断根因：{root_cause['root_cause_text']}。",
            )
            self._pause()
            self._emit(task, "step", index=3, status="done")
            return

        if step_id == "recommend_ap_recovery":
            self._emit(
                task,
                "solutions",
                title="Copilot",
                message="针对 AP 离线问题推荐以下恢复方案：",
                solutions=task.context["solutions"],
                scene={"phase": "Step 4 / Waiting User", "headline": "右侧等待用户选择恢复方案，左侧保持 AP3 故障态", "progress": 70},
                gui_action="右侧生成 AP 恢复方案，左侧保持 AP3 故障高亮",
                step={"index": 4, "status": "running"},
            )
            return

        if step_id == "recommend_solutions":
            self._emit(
                task,
                "solutions",
                title="Copilot",
                message="针对该问题 AI 为您推荐以下两种解决方案：",
                solutions=task.context["solutions"],
                scene={"phase": "Step 4 / Waiting User", "headline": "右侧等待用户选择方案，左侧保持 AP1 根因态", "progress": 68},
                gui_action="右侧生成方案卡片，左侧保持 AP1 根因高亮",
                step={"index": 4, "status": "running"},
            )

    def _run_action_playbook(self, task_id: str, action_spec: dict[str, Any], payload: dict[str, Any]) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        try:
            for resource in action_spec.get("resource_locks", []):
                task.locks.add(resource)
            self._persist_task(task)
            playbook = self.playbooks.load(action_spec["playbook"])
            for step in playbook.steps:
                self._execute_action_step(task, step, payload)
        except Exception as exc:
            task.locks.clear()
            self._set_state(task, "failed")
            self._emit(task, "runtime", title="Runtime", message=f"动作执行失败：{exc}")

    def _execute_action_step(self, task: Task, step: dict[str, Any], payload: dict[str, Any]) -> None:
        step_id = step["id"]
        if step_id not in {"complete", "complete_ap_recovery"}:
            self._set_state(task, step["state"])

        if step_id == "confirm_and_lock":
            associated_device = task.context["associated_device"]
            self._emit(task, "chat", role="user", title="用户", message="一键执行方案1：射频调优")
            checkpoint_id = self._checkpoint(task, step_id)
            self._emit(
                task,
                "runtime",
                title="Runtime",
                message=(
                    "恢复 waiting_user 任务，校验方案为 high-risk。\n"
                    "触发人在环确认，获取 AP1 / 站点1/1F 射频配置资源锁，并建立 checkpoint"
                    f"{'：' + checkpoint_id if checkpoint_id else '。'}"
                ),
                scene={"phase": "Step 4 / HITL Confirm", "headline": "确认高风险操作并锁定 AP1 射频配置资源", "progress": 72},
                gui_action="HITL：确认高风险射频调优并锁定 AP1 资源",
            )
            self._pause()
            return

        if step_id == "confirm_and_lock_ap_recovery":
            entities = task.context["entities"]
            switch_port = task.context["switch_port"]
            self._emit(task, "chat", role="user", title="用户", message="确认执行方案1：重启 PoE 端口")
            checkpoint_id = self._checkpoint(task, step_id)
            self._emit(
                task,
                "runtime",
                title="Runtime",
                message=(
                    f"恢复 waiting_user 任务，校验方案为 medium-risk。\n"
                    f"触发人在环确认，锁定 {switch_port['switch_name']} {switch_port['port']}，并建立 checkpoint"
                    f"{'：' + checkpoint_id if checkpoint_id else '。'}"
                ),
                scene={"phase": "Step 4 / HITL Confirm", "headline": f"确认重启 {entities['ap_name']} 所在交换机 PoE 端口", "progress": 74},
                gui_action=f"HITL：确认重启 {switch_port['switch_name']} {switch_port['port']} PoE",
            )
            self._pause()
            return

        if step_id == "enter_optimization_view":
            self._emit(
                task,
                "ui",
                view=step["view"],
                actions=step["ui_actions"],
                scene={"phase": "Step 4 / Execute", "headline": "左侧跳转 AP1 射频调优执行节点", "progress": 78},
                gui_action="左侧跳转：AP1 射频调优执行节点",
            )
            self._emit(task, "chat", role="assistant", title="Copilot", message="正在进入射频调优执行视图，并定位 AP1。")
            self._pause()
            return

        if step_id == "enter_ap_recovery_view":
            entities = task.context["entities"]
            self._emit(
                task,
                "ui",
                view=step["view"],
                actions=step["ui_actions"],
                scene={"phase": "Step 4 / Execute", "headline": f"左侧跳转 {entities['ap_name']} PoE 恢复执行节点", "progress": 80},
                gui_action=f"左侧跳转：{entities['ap_name']} PoE 恢复执行节点",
            )
            self._emit(task, "chat", role="assistant", title="Copilot", message=f"正在进入 {entities['ap_name']} 恢复执行视图，并准备重启 PoE 端口。")
            self._pause()
            return

        if step_id == "generate_strategy":
            associated_device = task.context["associated_device"]
            strategy = self.tools.call(step["tool"], ap_id=associated_device["ap_id"])
            task.context["strategy"] = strategy
            self._persist_task(task)
            self._emit(
                task,
                "runtime",
                title="Topology Sync",
                message="左侧已切换至 AP1 调优执行节点：策略生成中。",
                scene={"phase": "Step 4 / Strategy", "headline": "AP1 调优策略生成中，左侧进度同步", "progress": 84},
                gui_action="执行进度同步：策略生成中",
                ui_actions=step["ui_actions"],
            )
            self._pause()
            return

        if step_id == "dispatch_strategy":
            strategy = task.context["strategy"]
            dispatch = self.tools.call(step["tool"], strategy_id=strategy["strategy_id"])
            task.context["dispatch"] = dispatch
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                scene={"phase": "Step 4 / Dispatch", "headline": "策略已生成，正在下发到站点1 / 1F AP", "progress": 90},
                gui_action="执行进度同步：策略已生成，开始下发",
            )
            self._emit(task, "chat", role="assistant", title="Copilot", message="射频调优策略已生成，目标为降低 AP1 的同频干扰并改善张三吞吐与重传率。")
            self._pause()
            return

        if step_id == "restart_poe_port":
            switch_port = task.context["switch_port"]
            entities = task.context["entities"]
            poe_action = self.tools.call(
                step["tool"],
                switch_name=switch_port["switch_name"],
                port=switch_port["port"],
                ap_id=entities["ap_id"],
            )
            task.context["poe_action"] = poe_action
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                actions=step["ui_actions"],
                scene={"phase": "Step 4 / PoE Restart", "headline": "PoE 端口重启指令已下发，等待 AP 心跳恢复", "progress": 88},
                gui_action="执行进度同步：PoE 重启指令已下发",
            )
            self._emit(task, "chat", role="assistant", title="Copilot", message=poe_action["message"])
            self._pause()
            return

        if step_id == "verify_recovery":
            user = task.context["entities"]["user"]
            recovery = self.tools.call(step["tool"], user=user)
            task.context["recovery"] = recovery
            self._persist_task(task)
            self._emit(
                task,
                "runtime",
                title="Runtime",
                message="策略下发完成，正在监听生效状态并重新校验用户体验指标。",
                scene={"phase": "Step 5 / Verify", "headline": "策略下发完成，正在校验张三体验恢复", "progress": 96},
                gui_action="执行进度同步：策略生效校验中",
                ui_actions=step["ui_actions"],
            )
            self._pause()
            return

        if step_id == "verify_ap_online":
            entities = task.context["entities"]
            ap_recovery = self.tools.call(step["tool"], ap_id=entities["ap_id"])
            task.context["ap_recovery"] = ap_recovery
            self._persist_task(task)
            self._emit(
                task,
                "runtime",
                title="Runtime",
                message="PoE 重启完成，正在监听 AP 心跳并校验在线状态。",
                scene={"phase": "Step 5 / Verify", "headline": f"{entities['ap_name']} 心跳恢复，正在校验在线状态", "progress": 96},
                gui_action="执行进度同步：AP 心跳恢复校验中",
                ui_actions=step["ui_actions"],
            )
            self._pause()
            return

        if step_id == "complete":
            task.locks.clear()
            recovery = task.context["recovery"]
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                view=step["view"],
                actions=step["ui_actions"],
                metrics={"focus": "AP1 / ap_001", "neighbor": "恢复正常", "experience": "正常"},
                scene={"phase": "Step 5 / Completed", "headline": "AP1 状态恢复正常，张三体验校验通过", "progress": 100},
                gui_action="左侧完成态：AP1 恢复正常，清除同频干扰关系",
                step={"index": 4, "status": "done"},
            )
            self._emit(task, "step", index=5, status="done")
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message=f"站点1/1F AP 射频调优已完成。\n\n已重新校验用户张三的体验指标：\n- {recovery['summary']}\n\n用户张三体验恢复正常。",
            )
            self._remember_completion(task)
            self._set_state(task, "completed")

        if step_id == "complete_ap_recovery":
            task.locks.clear()
            entities = task.context["entities"]
            ap_recovery = task.context["ap_recovery"]
            self._persist_task(task)
            self._emit(
                task,
                "ui",
                view=step["view"],
                actions=step["ui_actions"],
                metrics={"focus": f"{entities['ap_name']} / {entities['ap_id']}", "neighbor": "PoE 正常", "experience": "AP 在线"},
                scene={"phase": "Step 5 / Completed", "headline": f"{entities['ap_name']} 已恢复在线，心跳校验通过", "progress": 100},
                gui_action=f"左侧完成态：{entities['ap_name']} 恢复在线，PoE 状态正常",
                step={"index": 4, "status": "done"},
            )
            self._emit(task, "step", index=5, status="done")
            self._emit(
                task,
                "chat",
                role="assistant",
                title="Copilot",
                message=f"{entities['ap_name']} PoE 端口恢复操作已完成。\n\n校验结果：\n- {ap_recovery['summary']}\n\n{entities['ap_name']} 已恢复在线。",
            )
            self._set_state(task, "completed")
