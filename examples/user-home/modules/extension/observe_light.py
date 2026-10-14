# -*- coding: utf-8 -*-
# Based on examples/user-home/modules/extension/example_mcp_extension.py
# Copyright 2025 rev1si0n (lamda.devel@gmail.com). All rights reserved.
# Modifications Copyright 2026 Rishon33. All rights reserved.
#
# Distributed under MIT license.
# See file LICENSE for detail or copy at https://opensource.org/licenses/MIT
"""FIRERPA MCP extension: lightweight device observation for polling loops.

Deploy: copy to the device's ~/modules/extension/ directory, then reload the
FIRERPA service. It registers its own MCP route, so the built-in /mcp/ tool
set is untouched.

Why this exists: the built-in observeDevice always returns the installed app
list (up to 128 entries) plus the full UI hierarchy plus device/vision
metadata, about 8 KB of text per call. That is fine for a one-off check and
wasteful in a polling loop.

observe_device_light never enumerates installed apps and never returns the
full hierarchy. It returns CapturedAt, CurrentPackage and, when asked, a
bounded set of nodes filtered by text/description/resource-id.
"""

import base64
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO

try:
    from PIL import Image
except ImportError:  # pragma: no cover - device may lack Pillow
    Image = None

from lamda.client import Bound
from lamda.extensions import BaseMcpExtension
from lamda.mcp import mcp, Annotated, TextContent


class ObserveLightMcpExtension(BaseMcpExtension):
    """Cheap stand-in for observeDevice in high-frequency polling."""

    route = "/observe-light/mcp/"
    name = "observe-light"
    version = "1.0"

    @mcp("tool", description=(
        "Lightweight device observation. Returns CapturedAt, CurrentPackage "
        "and optionally a bounded, filtered set of UI nodes. Never enumerates "
        "installed apps and never returns the full hierarchy. Use this for "
        "polling; use observeDevice when you need the complete state."
    ))
    def observe_device_light(
        self,
        ctx,
        include_installed_apps: Annotated[bool, "Accepted for call compatibility. The installed app list is never returned."] = False,
        hierarchy_mode: Annotated[str, "summary | nodes | none. 'none' skips the hierarchy dump entirely."] = "summary",
        node_patterns: Annotated[str, "Comma-separated substrings matched against text and content-desc."] = "",
        resource_ids: Annotated[str, "Comma-separated exact resource-id values to keep."] = "",
        max_nodes: Annotated[int, "Maximum nodes to return. 0 disables node output."] = 30,
        screenshot: Annotated[bool, "Include a JPEG screenshot as base64."] = False,
        screenshot_quality: Annotated[int, "JPEG quality when screenshot is enabled."] = 50,
        screenshot_scale: Annotated[float, "Optional downscale factor for the screenshot."] = 1.0,
        screenshot_bound: Annotated[str, "Optional crop region as top,left,right,bottom."] = "",
        display_id: Annotated[int, "Display to observe. 0 is the main display."] = 0,
    ):
        mode = str(hierarchy_mode).strip().lower()
        if mode not in ("summary", "nodes", "none"):
            raise ValueError("hierarchy_mode must be summary, nodes or none")

        try:
            max_nodes = int(max_nodes)
        except (TypeError, ValueError):
            max_nodes = 30
        max_nodes = max(0, min(max_nodes, 200))

        try:
            display_id = int(display_id)
        except (TypeError, ValueError):
            display_id = 0
        target = self.device if display_id == 0 else self.device.get_virtual_display_by_id(display_id)

        payload = {
            "CapturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "DisplayId": display_id,
            "CurrentPackage": self._current_package(target),
            "InstalledAppsIncluded": False,
            "Nodes": [],
            "NodeCount": 0,
            "Truncated": False,
        }

        if mode != "none" and max_nodes > 0:
            nodes = []
            error = None
            try:
                xml_data = target.dump_window_hierarchy(compressed=True).getvalue()
                root = ET.fromstring(xml_data)
                self._collect_nodes(root, mode, self._split_csv(str(node_patterns).lower()),
                                    self._split_csv(resource_ids), nodes)
            except Exception as exc:  # noqa: BLE001 - report the reason instead of failing the call
                error = "{0}: {1}".format(type(exc).__name__, exc)
            payload["Nodes"] = nodes[:max_nodes]
            payload["NodeCount"] = len(payload["Nodes"])
            payload["Truncated"] = len(nodes) > len(payload["Nodes"])
            if error:
                payload["HierarchyError"] = error

        if screenshot:
            try:
                bound = self._parse_bound(screenshot_bound)
                image_data = target.screenshot(int(screenshot_quality), bound=bound).getvalue()
                scale = float(screenshot_scale)
                if scale != 1.0:
                    if Image is None:
                        raise ValueError("screenshot_scale needs Pillow on the device")
                    with Image.open(BytesIO(image_data)) as src:
                        size = (max(1, int(src.width * scale)), max(1, int(src.height * scale)))
                        buf = BytesIO()
                        src.convert("RGB").resize(size).save(buf, format="JPEG",
                                                             quality=int(screenshot_quality))
                        image_data = buf.getvalue()
                payload["ScreenshotFormat"] = "jpeg"
                payload["ScreenshotBytes"] = len(image_data)
                payload["ScreenshotBase64"] = base64.b64encode(image_data).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                payload["ScreenshotError"] = "{0}: {1}".format(type(exc).__name__, exc)

        return TextContent(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _current_package(self, target):
        try:
            package = getattr(target.device_info(), "currentPackageName", None)
            if package:
                return package
        except Exception:  # noqa: BLE001
            pass
        try:
            return target.current_application().packageName or ""
        except Exception:  # noqa: BLE001
            return ""

    def _split_csv(self, value):
        return [item.strip() for item in str(value).split(",") if item.strip()]

    def _parse_bound(self, value):
        parts = self._split_csv(value)
        if not parts:
            return None
        if len(parts) != 4:
            raise ValueError("screenshot_bound must be top,left,right,bottom")
        top, left, right, bottom = (int(item) for item in parts)
        return Bound(top=top, left=left, right=right, bottom=bottom)

    def _collect_nodes(self, element, mode, patterns, wanted_ids, output):
        if element.tag != "node":
            for child in element:
                self._collect_nodes(child, mode, patterns, wanted_ids, output)
            return

        attrs = element.attrib
        if attrs.get("package") == "com.android.systemui":
            return

        text = attrs.get("text", "") or ""
        description = attrs.get("content-desc", "") or ""
        resource_id = attrs.get("resource-id", "") or ""

        # Decide whether this node itself is worth emitting. Never return early:
        # layout containers carry no text and their children do.
        emit = True
        if mode == "nodes":
            haystack = (text + "\n" + description).lower()
            emit = any(pattern in haystack for pattern in patterns) or resource_id in wanted_ids
        elif not text and not description and not resource_id:
            emit = False

        if emit:
            node = {
                "Text": text,
                "Description": description,
                "ResourceId": resource_id,
                "Class": attrs.get("class", "") or "",
                "Bounds": attrs.get("bounds", "") or "",
            }
            if attrs.get("clickable") == "true":
                node["Clickable"] = True
            output.append(node)

        for child in element:
            self._collect_nodes(child, mode, patterns, wanted_ids, output)
