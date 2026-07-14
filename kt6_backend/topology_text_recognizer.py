from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


class TopologyTextRecognizer:
    """Conservatively turn a structured Unicode topology text into a Scene Graph.

    The recognizer deliberately understands only evidence that is explicit in the
    supplied text: a small set of diagram fan-out patterns and the device-detail
    table.  Narrative architecture descriptions are retained as source material,
    but never used to manufacture devices or relations.
    """

    MAX_INPUT_CHARS = 200_000
    MAX_LINES = 4_000
    MAX_DISPLAY_CELLS = 1_000_000
    recognizer_id = "kt6.topology_text_recognizer"
    recognizer_version = "1.0.0"

    DEVICE_ID_PATTERN = re.compile(
        r"(?<![A-Za-z0-9])(?P<prefix>[A-Za-z][A-Za-z0-9]{0,15})[-_](?P<number>\d{2,8})(?![A-Za-z0-9])"
    )
    MARKER_PATTERN = re.compile(r"\s*[（(]\s*([^()（）]+?)\s*[)）]")
    UNCERTAINTY_PATTERN = re.compile(r"可能|疑似|异常或|识别失败|暗示|未知|不确定|\?")

    PREFIX_TYPES = {
        "GW": "gateway",
        "CORE": "core",
        "AGG": "aggregation",
        "ACC": "access_switch",
        "AP": "ap",
    }
    ROLE_TYPES = {
        "出口网关": "gateway",
        "核心交换机": "core",
        "汇聚/防火墙": "aggregation_firewall",
        "接入交换机": "access_switch",
        "无线AP": "ap",
    }

    def recognize(self, text: str, source_ref: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str):
            return self._rejected_scene(
                text="",
                source_ref=source_ref,
                code="invalid_input_type",
                message="topology text must be a string",
            )
        if len(text) > self.MAX_INPUT_CHARS:
            return self._rejected_scene(
                text=text[: self.MAX_INPUT_CHARS],
                source_ref=source_ref,
                code="input_too_large",
                message=f"input exceeds {self.MAX_INPUT_CHARS} characters",
            )

        normalized = self._normalize_text(text)
        lines = normalized.split("\n") if normalized else []
        display_cells = sum(self._display_width(line) for line in lines)
        if len(lines) > self.MAX_LINES or display_cells > self.MAX_DISPLAY_CELLS:
            return self._rejected_scene(
                text=normalized,
                source_ref=source_ref,
                code="input_grid_too_large",
                message=(
                    f"input exceeds {self.MAX_LINES} lines or "
                    f"{self.MAX_DISPLAY_CELLS} display cells"
                ),
            )

        source_id = "topology_text_input"
        content_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        evidence: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        visual_groups: list[dict[str, Any]] = []
        nodes: dict[str, dict[str, Any]] = {}
        relations: dict[tuple[str, str, str], dict[str, Any]] = {}

        sections = self._section_ranges(lines)
        sources = self._sources(
            source_id=source_id,
            source_ref=source_ref,
            content_sha256=content_sha256,
            lines=lines,
            sections=sections,
        )

        if self._contains_unsafe_formatting(normalized):
            issues.append(
                self._issue(
                    "unsafe_unicode_formatting",
                    "error",
                    "input contains bidirectional or zero-width formatting characters",
                )
            )

        diagram_end = sections.get("device_table", (len(lines), len(lines)))[0]
        diagram_occurrences = self._parse_diagram_nodes(
            lines,
            diagram_end,
            nodes,
            evidence,
            source_id,
            issues,
        )
        self._parse_visual_groups(
            lines,
            diagram_end,
            visual_groups,
            evidence,
            source_id,
        )
        diagram_relation_count = self._parse_diagram_relations(
            lines,
            diagram_end,
            diagram_occurrences,
            nodes,
            relations,
            evidence,
            source_id,
            issues,
        )

        table_result = self._parse_device_table(
            lines,
            sections,
            nodes,
            relations,
            evidence,
            source_id,
            issues,
        )
        self._parse_special_markers(
            lines,
            sections,
            nodes,
            evidence,
            source_id,
            issues,
        )

        if not normalized:
            issues.append(self._issue("empty_input", "error", "topology text is empty"))
        if not nodes:
            issues.append(self._issue("no_devices", "error", "no strict device IDs were recognized"))
        if diagram_relation_count == 0:
            issues.append(
                self._issue(
                    "no_explicit_diagram_relations",
                    "error",
                    "no complete diagram connector hierarchy was recognized",
                )
            )
        if not table_result["header_found"]:
            issues.append(
                self._issue(
                    "missing_device_table",
                    "error",
                    "the 设备详情 table header is missing or unsupported",
                )
            )
        elif not table_result["closed"]:
            issues.append(
                self._issue(
                    "incomplete_device_table",
                    "error",
                    "the 设备详情 table is not closed; input may be truncated",
                )
            )

        for relation in relations.values():
            if relation["source"] not in nodes or relation["target"] not in nodes:
                issues.append(
                    self._issue(
                        "dangling_relation_endpoint",
                        "error",
                        "relation endpoint is not a recognized device",
                        relation_id=relation["relation_id"],
                    )
                )

        graph_metrics = self._graph_metrics(nodes, relations)
        evidence_by_id = {item["evidence_id"]: item for item in evidence}
        for business_id in graph_metrics["isolated_business_ids"]:
            node = nodes[business_id]
            evidence_kinds = {
                evidence_by_id[evidence_id]["kind"]
                for evidence_id in node["evidence_ids"]
                if evidence_id in evidence_by_id
            }
            if node["attributes"].get("independent_access"):
                issues.append(
                    self._issue(
                        "unknown_parent",
                        "warning",
                        "independent device is explicit, but its parent or attachment point is unknown",
                        business_id=business_id,
                    )
                )
            elif "table_device_row" in evidence_kinds and "diagram_device_id" not in evidence_kinds:
                issues.append(
                    self._issue(
                        "table_only_node_no_edge",
                        "warning",
                        "device is explicit in the table but has no asserted topology relation",
                        business_id=business_id,
                    )
                )

        elements, bindings = self._finalize_nodes(nodes, evidence)
        ordered_relations = sorted(
            relations.values(),
            key=lambda item: (item["source"], item["target"], item["type"]),
        )
        errors = [issue for issue in issues if issue["severity"] == "error"]
        usable_for_analysis = bool(elements and ordered_relations and not errors)

        metrics = {
            "input_char_count": len(normalized),
            "line_count": len(lines),
            "display_cell_count": display_cells,
            "node_count": len(elements),
            "relation_count": len(ordered_relations),
            "diagram_relation_count": diagram_relation_count,
            "table_relation_count": table_result["relation_count"],
            "visual_group_count": len(visual_groups),
            "evidence_count": len(evidence),
            "issue_count": len(issues),
            "error_count": len(errors),
            "main_component_nodes": graph_metrics["main_component_nodes"],
            "observed_isolated_nodes": graph_metrics["observed_isolated_nodes"],
            "connected_components": graph_metrics["connected_components"],
            "undirected_cycle_rank": graph_metrics["undirected_cycle_rank"],
        }
        return {
            "mode": "topology_text_recognizer",
            "scene_type": "text_topology",
            "object_count": len(elements),
            "elements": elements,
            "business_object_bindings": bindings,
            "relations": ordered_relations,
            "co_channel_relations": [],
            "relation_count": len(ordered_relations),
            "coordinate_space": {
                "type": "text_grid",
                "width": max((self._display_width(line) for line in lines), default=0),
                "height": len(lines),
                "unit": "display_cell",
                "synthetic": True,
                "actionable_grounding": False,
            },
            "sources": sources,
            "visual_groups": visual_groups,
            "issues": issues,
            "metrics": metrics,
            "evidence": evidence,
            "usable_for_analysis": usable_for_analysis,
            "usable_for_actions": False,
            "actionable_grounding": False,
            "requires_vision_model": False,
            "diagnostics": {
                "structural_complete": usable_for_analysis,
                "table_header_found": table_result["header_found"],
                "table_closed": table_result["closed"],
                "narrative_relations_enabled": False,
            },
            "limitations": [
                "仅解析明确的 Unicode 层级连线和设备详情表，不从架构说明补全关系",
                "下方AP仅表示下游归属，不代表直连、PoE 或物理链路",
                "text-grid 坐标仅用于证据定位，不能作为 GUI 点击或设备操作依据",
            ],
        }

    def _normalize_text(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        non_empty = [line for line in lines if line.strip()]
        if non_empty:
            common_indent = min(len(line) - len(line.lstrip(" ")) for line in non_empty)
            if common_indent:
                lines = [line[common_indent:] if line.strip() else "" for line in lines]
        return "\n".join(line.rstrip() for line in lines)

    def _contains_unsafe_formatting(self, text: str) -> bool:
        return any(
            unicodedata.category(char) == "Cf"
            for char in text
        )

    def _section_ranges(self, lines: list[str]) -> dict[str, tuple[int, int]]:
        headings: dict[str, int] = {}
        names = {
            "设备详情": "device_table",
            "特殊标记设备": "special_markers",
            "架构特点": "architecture_notes",
        }
        for index, line in enumerate(lines):
            normalized = re.sub(r"\s+", "", line)
            for heading, key in names.items():
                if normalized == heading:
                    headings[key] = index
        ordered = sorted((index, key) for key, index in headings.items())
        ranges: dict[str, tuple[int, int]] = {}
        for position, (start, key) in enumerate(ordered):
            end = ordered[position + 1][0] if position + 1 < len(ordered) else len(lines)
            ranges[key] = (start, end)
        return ranges

    def _sources(
        self,
        *,
        source_id: str,
        source_ref: str | None,
        content_sha256: str,
        lines: list[str],
        sections: dict[str, tuple[int, int]],
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = [
            {
                "source_id": source_id,
                "kind": "unicode_plain_text",
                "source_ref": source_ref,
                "sha256": content_sha256,
                "line_start": 1 if lines else 0,
                "line_end": len(lines),
            }
        ]
        diagram_end = sections.get("device_table", (len(lines), len(lines)))[0]
        if diagram_end:
            sources.append(
                {
                    "source_id": "diagram",
                    "kind": "unicode_topology_diagram",
                    "line_start": 1,
                    "line_end": diagram_end,
                }
            )
        for key, kind in (
            ("device_table", "device_detail_table"),
            ("special_markers", "special_device_notes"),
            ("architecture_notes", "narrative_architecture_notes"),
        ):
            if key in sections:
                start, end = sections[key]
                sources.append(
                    {
                        "source_id": key,
                        "kind": kind,
                        "line_start": start + 1,
                        "line_end": end,
                    }
                )
        return sources

    def _parse_diagram_nodes(
        self,
        lines: list[str],
        diagram_end: int,
        nodes: dict[str, dict[str, Any]],
        evidence: list[dict[str, Any]],
        source_id: str,
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        occurrences = []
        for row, line in enumerate(lines[:diagram_end]):
            for match in self.DEVICE_ID_PATTERN.finditer(line):
                raw_id = match.group(0)
                business_id = self._canonical_id(match)
                column = self._display_width(line[: match.start()])
                width = self._display_width(raw_id)
                evidence_id = self._add_evidence(
                    evidence,
                    kind="diagram_device_id",
                    text=raw_id,
                    source_id="diagram" if diagram_end else source_id,
                    row=row,
                    column=column,
                    width=width,
                )
                type_hint = self.PREFIX_TYPES.get(match.group("prefix").upper(), "device")
                self._add_node(
                    nodes,
                    business_id,
                    raw_id,
                    type_hint,
                    30,
                    evidence_id,
                    [column, row, width, 1],
                    100,
                    {},
                    issues,
                )
                occurrences.append(
                    {
                        "business_id": business_id,
                        "raw_id": raw_id,
                        "prefix": match.group("prefix").upper(),
                        "row": row,
                        "column": column,
                        "width": width,
                        "evidence_id": evidence_id,
                    }
                )
        return occurrences

    def _parse_visual_groups(
        self,
        lines: list[str],
        diagram_end: int,
        visual_groups: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        source_id: str,
    ) -> None:
        trunk_index = 0
        ap_group_index = 0
        for row, line in enumerate(lines[:diagram_end]):
            for match in re.finditer(r"主干线\s*[（(]?\s*Trunk\s*[)）]?", line, re.IGNORECASE):
                trunk_index += 1
                column = self._display_width(line[: match.start()])
                width = self._display_width(match.group(0))
                evidence_id = self._add_evidence(
                    evidence,
                    kind="visual_relation_group",
                    text=match.group(0),
                    source_id="diagram" if diagram_end else source_id,
                    row=row,
                    column=column,
                    width=width,
                )
                visual_groups.append(
                    {
                        "group_id": f"visual_group_trunk_{trunk_index:03d}",
                        "kind": "relation_label",
                        "label": match.group(0),
                        "is_device": False,
                        "bbox": [column, row, width, 1],
                        "evidence_ids": [evidence_id],
                    }
                )
            for match in re.finditer(r"AP群", line, re.IGNORECASE):
                ap_group_index += 1
                column = self._display_width(line[: match.start()])
                width = self._display_width(match.group(0))
                evidence_id = self._add_evidence(
                    evidence,
                    kind="visual_generic_group",
                    text=match.group(0),
                    source_id="diagram" if diagram_end else source_id,
                    row=row,
                    column=column,
                    width=width,
                )
                visual_groups.append(
                    {
                        "group_id": f"visual_group_ap_group_{ap_group_index:03d}",
                        "kind": "generic_device_group",
                        "label": match.group(0),
                        "is_device": False,
                        "bbox": [column, row, width, 1],
                        "evidence_ids": [evidence_id],
                    }
                )

    def _parse_diagram_relations(
        self,
        lines: list[str],
        diagram_end: int,
        occurrences: list[dict[str, Any]],
        nodes: dict[str, dict[str, Any]],
        relations: dict[tuple[str, str, str], dict[str, Any]],
        evidence: list[dict[str, Any]],
        source_id: str,
        issues: list[dict[str, Any]],
    ) -> int:
        count = 0
        gateways = [item for item in occurrences if item["prefix"] == "GW"]
        cores = [item for item in occurrences if item["prefix"] == "CORE"]
        if len(gateways) == 1 and len(cores) == 1 and gateways[0]["row"] < cores[0]["row"]:
            upstream = gateways[0]
            downstream = cores[0]
            segment = lines[upstream["row"] : downstream["row"] + 1]
            if any("▼" in line for line in segment) and any("│" in line for line in segment):
                evidence_id = self._add_relation_evidence(
                    evidence,
                    "diagram_connector_path",
                    upstream,
                    downstream,
                    source_id="diagram" if diagram_end else source_id,
                )
                if self._add_relation(
                    relations,
                    upstream["business_id"],
                    downstream["business_id"],
                    "topology_link",
                    evidence_id,
                    "diagram_connector",
                    {"layout_direction": "downstream", "directness": "diagram_explicit"},
                ):
                    count += 1
            else:
                issues.append(
                    self._issue(
                        "incomplete_gateway_core_connector",
                        "error",
                        "GW and CORE are present but the explicit down connector is incomplete",
                    )
                )

        trunk_rows = [
            row
            for row, line in enumerate(lines[:diagram_end])
            if re.search(r"主干线\s*[（(]?\s*Trunk", line, re.IGNORECASE)
        ]
        if len(cores) == 1 and len(trunk_rows) == 1:
            trunk_row = trunk_rows[0]
            access_rows: list[tuple[int, list[dict[str, Any]]]] = []
            for row in range(trunk_row + 1, diagram_end):
                row_access = [
                    item
                    for item in occurrences
                    if item["row"] == row and item["prefix"] == "ACC"
                ]
                if row_access:
                    access_rows.append((row, row_access))
            if access_rows:
                access_row, access_nodes = access_rows[0]
                arrow_count = sum(line.count("▼") for line in lines[trunk_row + 1 : access_row + 1])
                has_branch = any(
                    "┼" in line or line.count("┬") >= 2
                    for line in lines[trunk_row + 1 : access_row + 1]
                )
                if arrow_count == len(access_nodes) and has_branch and access_nodes:
                    core = cores[0]
                    for access in sorted(access_nodes, key=lambda item: item["column"]):
                        evidence_id = self._add_relation_evidence(
                            evidence,
                            "diagram_trunk_fanout",
                            core,
                            access,
                            source_id="diagram",
                        )
                        if self._add_relation(
                            relations,
                            core["business_id"],
                            access["business_id"],
                            "trunk",
                            evidence_id,
                            "diagram_connector",
                            {
                                "layout_direction": "downstream",
                                "directness": "diagram_explicit",
                                "visual_group": "主干线 (Trunk)",
                            },
                        ):
                            count += 1
                else:
                    issues.append(
                        self._issue(
                            "incomplete_trunk_fanout",
                            "error",
                            "Trunk fan-out requires one ACC row and an equal number of explicit down arrows",
                            arrow_count=arrow_count,
                            access_count=len(access_nodes),
                        )
                    )
            else:
                issues.append(
                    self._issue(
                        "missing_trunk_targets",
                        "error",
                        "Trunk is present but no downstream ACC row was recognized",
                    )
                )
        return count

    def _parse_device_table(
        self,
        lines: list[str],
        sections: dict[str, tuple[int, int]],
        nodes: dict[str, dict[str, Any]],
        relations: dict[tuple[str, str, str], dict[str, Any]],
        evidence: list[dict[str, Any]],
        source_id: str,
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = {"header_found": False, "closed": False, "relation_count": 0}
        if "device_table" not in sections:
            return result
        start, end = sections["device_table"]
        header_seen = False
        for row in range(start + 1, end):
            line = lines[row]
            stripped = line.strip()
            if stripped.startswith("└") and "┘" in stripped:
                result["closed"] = True
            if "│" not in line:
                continue
            cells_with_offsets = self._table_cells(line)
            if len(cells_with_offsets) < 4:
                continue
            cells = [cell[0].strip() for cell in cells_with_offsets[:4]]
            normalized_headers = [re.sub(r"\s+", "", cell) for cell in cells]
            if normalized_headers == ["设备", "型号", "角色", "下方AP"]:
                header_seen = True
                result["header_found"] = True
                continue
            if not header_seen:
                continue
            parent_match = self.DEVICE_ID_PATTERN.search(cells[0])
            if not parent_match:
                continue
            raw_parent = parent_match.group(0)
            parent_id = self._canonical_id(parent_match)
            parent_column = self._display_width(line[: line.find(raw_parent)])
            parent_evidence = self._add_evidence(
                evidence,
                kind="table_device_row",
                text=" | ".join(cells),
                source_id="device_table",
                row=row,
                column=parent_column,
                width=self._display_width(raw_parent),
            )
            role = cells[2]
            attributes: dict[str, Any] = {}
            if cells[1] and cells[1] != "-":
                attributes["model"] = cells[1]
            if role and role != "-":
                attributes["role"] = role
            if "独立接入" in cells[3]:
                attributes["independent_access"] = True
            self._add_node(
                nodes,
                parent_id,
                raw_parent,
                self.ROLE_TYPES.get(role, self.PREFIX_TYPES.get(parent_match.group("prefix").upper(), "device")),
                100,
                parent_evidence,
                [parent_column, row, self._display_width(raw_parent), 1],
                50,
                attributes,
                issues,
            )

            downstream_text = cells[3]
            downstream_start = cells_with_offsets[3][1]
            for child_match in self.DEVICE_ID_PATTERN.finditer(downstream_text):
                raw_child = child_match.group(0)
                child_id = self._canonical_id(child_match)
                child_column = self._display_width(line[:downstream_start]) + self._display_width(
                    downstream_text[: child_match.start()]
                )
                marker_match = self.MARKER_PATTERN.match(downstream_text, child_match.end())
                marker = marker_match.group(1).strip() if marker_match else ""
                child_evidence = self._add_evidence(
                    evidence,
                    kind="table_downstream_device",
                    text=raw_child + (f" ({marker})" if marker else ""),
                    source_id="device_table",
                    row=row,
                    column=child_column,
                    width=self._display_width(raw_child),
                )
                child_attributes: dict[str, Any] = {"declared_category": "下方AP"}
                if marker:
                    child_attributes["inline_marker"] = marker
                self._add_node(
                    nodes,
                    child_id,
                    raw_child,
                    "ap",
                    70,
                    child_evidence,
                    [child_column, row, self._display_width(raw_child), 1],
                    40,
                    child_attributes,
                    issues,
                )
                if self._add_relation(
                    relations,
                    parent_id,
                    child_id,
                    "downstream",
                    child_evidence,
                    "device_detail_table",
                    {"directness": "unknown", "declared_column": "下方AP"},
                ):
                    result["relation_count"] += 1
        return result

    def _parse_special_markers(
        self,
        lines: list[str],
        sections: dict[str, tuple[int, int]],
        nodes: dict[str, dict[str, Any]],
        evidence: list[dict[str, Any]],
        source_id: str,
        issues: list[dict[str, Any]],
    ) -> None:
        if "special_markers" not in sections:
            return
        start, end = sections["special_markers"]
        for row in range(start + 1, end):
            line = lines[row]
            for match in self.DEVICE_ID_PATTERN.finditer(line):
                raw_id = match.group(0)
                business_id = self._canonical_id(match)
                marker_match = self.MARKER_PATTERN.match(line, match.end())
                marker = marker_match.group(1).strip() if marker_match else ""
                note_parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
                note = note_parts[1].strip() if len(note_parts) == 2 else line.strip(" -")
                column = self._display_width(line[: match.start()])
                evidence_id = self._add_evidence(
                    evidence,
                    kind="special_device_note",
                    text=line.strip(),
                    source_id="special_markers",
                    row=row,
                    column=column,
                    width=self._display_width(raw_id),
                )
                if business_id not in nodes:
                    self._add_node(
                        nodes,
                        business_id,
                        raw_id,
                        self.PREFIX_TYPES.get(match.group("prefix").upper(), "device"),
                        20,
                        evidence_id,
                        [column, row, self._display_width(raw_id), 1],
                        10,
                        {},
                        issues,
                    )
                else:
                    nodes[business_id]["evidence_ids"].append(evidence_id)
                node = nodes[business_id]
                if marker:
                    node["attributes"].setdefault("special_markers", [])
                    if marker not in node["attributes"]["special_markers"]:
                        node["attributes"]["special_markers"].append(marker)
                node["attributes"].setdefault("special_notes", [])
                if note not in node["attributes"]["special_notes"]:
                    node["attributes"]["special_notes"].append(note)
                node["attributes"]["classification_uncertain"] = True
                if marker.upper() in {"LSW", "ONU"} or marker == "?":
                    node["attributes"]["type_candidates"] = [
                        node["type"],
                        "unknown" if marker == "?" else f"marker:{marker}",
                    ]
                    node["attributes"]["classification_status"] = "conflicted"
                    node["type"] = "unknown_device"
                    node["type_priority"] = 110
                node["usable_for_actions"] = False
                issues.append(
                    self._issue(
                        "uncertain_special_device",
                        "warning",
                        "special marker is retained as uncertain evidence and not promoted to a structural fact",
                        business_id=business_id,
                        marker=marker or None,
                        hedged=bool(self.UNCERTAINTY_PATTERN.search(line)),
                        evidence_ids=[evidence_id],
                    )
                )

    def _add_node(
        self,
        nodes: dict[str, dict[str, Any]],
        business_id: str,
        raw_id: str,
        type_hint: str,
        type_priority: int,
        evidence_id: str,
        bbox: list[int],
        bbox_priority: int,
        attributes: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        node = nodes.setdefault(
            business_id,
            {
                "business_id": business_id,
                "display_id": raw_id.upper().replace("_", "-"),
                "type": type_hint,
                "type_priority": type_priority,
                "attributes": {},
                "evidence_ids": [],
                "bbox": bbox,
                "bbox_priority": bbox_priority,
                "usable_for_actions": False,
            },
        )
        if evidence_id not in node["evidence_ids"]:
            node["evidence_ids"].append(evidence_id)
        if type_priority > node["type_priority"]:
            node["type"] = type_hint
            node["type_priority"] = type_priority
        elif type_priority == node["type_priority"] and node["type"] != type_hint:
            issues.append(
                self._issue(
                    "conflicting_device_type",
                    "error",
                    "equally authoritative sources disagree on the device type",
                    business_id=business_id,
                    types=sorted({node["type"], type_hint}),
                )
            )
        if bbox_priority > node["bbox_priority"]:
            node["bbox"] = bbox
            node["bbox_priority"] = bbox_priority
        for key, value in attributes.items():
            if key not in node["attributes"]:
                node["attributes"][key] = value
                continue
            existing = node["attributes"][key]
            if existing == value:
                continue
            if key == "model" and (str(existing).startswith(str(value)) or str(value).startswith(str(existing))):
                longer = max((str(existing), str(value)), key=len)
                shorter = min((str(existing), str(value)), key=len)
                node["attributes"]["model"] = longer
                node["attributes"].setdefault("model_aliases", [])
                if shorter not in node["attributes"]["model_aliases"]:
                    node["attributes"]["model_aliases"].append(shorter)
                continue
            issues.append(
                self._issue(
                    "conflicting_device_attribute",
                    "error",
                    "equally identified device has conflicting explicit attributes",
                    business_id=business_id,
                    attribute=key,
                    values=[existing, value],
                )
            )

    def _add_relation(
        self,
        relations: dict[tuple[str, str, str], dict[str, Any]],
        source: str,
        target: str,
        relation_type: str,
        evidence_id: str,
        evidence_source: str,
        attributes: dict[str, Any],
    ) -> bool:
        key = (source, target, relation_type)
        if key in relations:
            if evidence_id not in relations[key]["evidence_ids"]:
                relations[key]["evidence_ids"].append(evidence_id)
            return False
        relation_id = f"text_relation_{source}_{target}_{relation_type}"
        relations[key] = {
            "relation_id": relation_id,
            "source": source,
            "target": target,
            "type": relation_type,
            "attributes": attributes,
            "confidence": 0.99 if evidence_source == "diagram_connector" else 0.96,
            "evidence_source": evidence_source,
            "evidence_ids": [evidence_id],
            "usable_for_actions": False,
        }
        return True

    def _finalize_nodes(
        self,
        nodes: dict[str, dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        evidence_by_id = {item["evidence_id"]: item for item in evidence}
        elements = []
        bindings: dict[str, dict[str, Any]] = {}
        for business_id in sorted(nodes):
            node = nodes[business_id]
            bbox = node["bbox"]
            evidence_kinds = {
                evidence_by_id[evidence_id]["kind"]
                for evidence_id in node["evidence_ids"]
                if evidence_id in evidence_by_id
            }
            confidence = 0.99 if {"diagram_device_id", "table_device_row"} <= evidence_kinds else 0.96
            if evidence_kinds == {"diagram_device_id"}:
                confidence = 0.9
            element_id = f"text_node_{business_id}"
            attributes = {
                "display_id": node["display_id"],
                **node["attributes"],
            }
            element = {
                "element_id": element_id,
                "business_id": business_id,
                "type": node["type"],
                "label": node["display_id"],
                "bbox": bbox,
                "center": [round(bbox[0] + bbox[2] / 2, 2), round(bbox[1] + bbox[3] / 2, 2)],
                "attributes": attributes,
                "confidence": confidence,
                "evidence_ids": list(node["evidence_ids"]),
                "usable_for_actions": False,
                "actionable_grounding": False,
            }
            elements.append(element)
            first_evidence = evidence_by_id[node["evidence_ids"][0]]
            bindings[business_id] = {
                "element_id": element_id,
                "text_ref": (
                    f"text:{first_evidence['span']['line_start']}:"
                    f"{first_evidence['span']['column_start']}"
                ),
                "confidence": confidence,
                "method": "topology_text_recognizer",
                "evidence_ids": list(node["evidence_ids"]),
                "usable_for_actions": False,
                "actionable_grounding": False,
            }
        return elements, bindings

    def _add_evidence(
        self,
        evidence: list[dict[str, Any]],
        *,
        kind: str,
        text: str,
        source_id: str,
        row: int,
        column: int,
        width: int,
    ) -> str:
        evidence_id = f"evidence_{len(evidence) + 1:04d}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "source_id": source_id,
                "text": text,
                "span": {
                    "line_start": row + 1,
                    "line_end": row + 1,
                    "column_start": column + 1,
                    "column_end": column + max(width, 1),
                },
            }
        )
        return evidence_id

    def _add_relation_evidence(
        self,
        evidence: list[dict[str, Any]],
        kind: str,
        upstream: dict[str, Any],
        downstream: dict[str, Any],
        *,
        source_id: str,
    ) -> str:
        start_row = min(upstream["row"], downstream["row"])
        end_row = max(upstream["row"], downstream["row"])
        start_column = min(upstream["column"], downstream["column"])
        end_column = max(
            upstream["column"] + upstream["width"],
            downstream["column"] + downstream["width"],
        )
        evidence_id = f"evidence_{len(evidence) + 1:04d}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "source_id": source_id,
                "text": f"{upstream['raw_id']} -> {downstream['raw_id']}",
                "derived_from_explicit_layout": True,
                "span": {
                    "line_start": start_row + 1,
                    "line_end": end_row + 1,
                    "column_start": start_column + 1,
                    "column_end": end_column,
                },
            }
        )
        return evidence_id

    def _table_cells(self, line: str) -> list[tuple[str, int]]:
        delimiters = [index for index, char in enumerate(line) if char == "│"]
        cells = []
        for left, right in zip(delimiters, delimiters[1:]):
            cells.append((line[left + 1 : right], left + 1))
        return cells

    def _canonical_id(self, match: re.Match[str]) -> str:
        prefix = unicodedata.normalize("NFKC", match.group("prefix")).casefold()
        return f"{prefix}_{match.group('number')}"

    def _display_width(self, text: str) -> int:
        width = 0
        for char in text:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        return width

    def _graph_metrics(
        self,
        nodes: dict[str, dict[str, Any]],
        relations: dict[tuple[str, str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        adjacency = {business_id: set() for business_id in nodes}
        undirected_edges: set[tuple[str, str]] = set()
        for relation in relations.values():
            source = relation["source"]
            target = relation["target"]
            if source not in adjacency or target not in adjacency:
                continue
            adjacency[source].add(target)
            adjacency[target].add(source)
            undirected_edges.add(tuple(sorted((source, target))))

        component_sizes = []
        unseen = set(adjacency)
        while unseen:
            start = min(unseen)
            stack = [start]
            unseen.remove(start)
            size = 0
            while stack:
                current = stack.pop()
                size += 1
                neighbors = adjacency[current] & unseen
                unseen.difference_update(neighbors)
                stack.extend(sorted(neighbors, reverse=True))
            component_sizes.append(size)

        component_count = len(component_sizes)
        cycle_rank = max(0, len(undirected_edges) - len(nodes) + component_count)
        isolated = sorted(business_id for business_id, neighbors in adjacency.items() if not neighbors)
        return {
            "main_component_nodes": max(component_sizes, default=0),
            "observed_isolated_nodes": len(isolated),
            "isolated_business_ids": isolated,
            "connected_components": component_count,
            "undirected_cycle_rank": cycle_rank,
        }

    def _issue(self, code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
        issue = {"code": code, "severity": severity, "message": message}
        issue.update(details)
        return issue

    def _rejected_scene(
        self,
        *,
        text: str,
        source_ref: str | None,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        normalized = self._normalize_text(text)
        lines = normalized.split("\n") if normalized else []
        content_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        issue = self._issue(code, "error", message)
        return {
            "mode": "topology_text_recognizer",
            "scene_type": "text_topology",
            "object_count": 0,
            "elements": [],
            "business_object_bindings": {},
            "relations": [],
            "co_channel_relations": [],
            "relation_count": 0,
            "coordinate_space": {
                "type": "text_grid",
                "width": 0,
                "height": 0,
                "unit": "display_cell",
                "synthetic": True,
                "actionable_grounding": False,
            },
            "sources": [
                {
                    "source_id": "topology_text_input",
                    "kind": "unicode_plain_text",
                    "source_ref": source_ref,
                    "sha256": content_sha256,
                    "line_start": 1 if lines else 0,
                    "line_end": len(lines),
                }
            ],
            "visual_groups": [],
            "issues": [issue],
            "metrics": {
                "input_char_count": len(normalized),
                "line_count": len(lines),
                "display_cell_count": 0,
                "node_count": 0,
                "relation_count": 0,
                "diagram_relation_count": 0,
                "table_relation_count": 0,
                "visual_group_count": 0,
                "evidence_count": 0,
                "issue_count": 1,
                "error_count": 1,
                "main_component_nodes": 0,
                "observed_isolated_nodes": 0,
                "connected_components": 0,
                "undirected_cycle_rank": 0,
            },
            "evidence": [],
            "usable_for_analysis": False,
            "usable_for_actions": False,
            "actionable_grounding": False,
            "requires_vision_model": False,
            "diagnostics": {
                "structural_complete": False,
                "table_header_found": False,
                "table_closed": False,
                "narrative_relations_enabled": False,
            },
            "limitations": ["input rejected before topology recognition"],
        }
