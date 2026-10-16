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

Screenshots: inline base64 is expensive (a q50 shot is ~255 KB -> ~340 KB of
base64 text, which is a lot of model context per poll). Pass
`screenshot_save_path` instead and the JPEG is written to a file on the device
and the response carries only `ScreenshotPath` + `ScreenshotBytes`. The
directory is pruned automatically (keep-newest count and max age), so shots
cannot accumulate and fill the device. Pull the file to the PC with
`extensions/pull_screenshots.py`, then delete it on the device.
"""

import base64
import json
import os
import time
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
    version = "1.1"

    @mcp("tool", description=(
        "Lightweight device observation. Returns CapturedAt, CurrentPackage "
        "and optionally a bounded, filtered set of UI nodes. Never enumerates "
        "installed apps and never returns the full hierarchy. Use this for "
        "polling; use observeDevice when you need the complete state. For "
        "screenshots prefer screenshot_save_path (written on the device, "
        "auto-pruned) over inline base64: it keeps the response tiny."
    ))
    def observe_device_light(
        self,
        ctx,
        include_installed_apps: Annotated[bool, "Accepted for call compatibility. The installed app list is never returned."] = False,
        hierarchy_mode: Annotated[str, "summary | nodes | none. 'none' skips the hierarchy dump entirely."] = "summary",
        node_patterns: Annotated[str, "Comma-separated substrings matched against text and content-desc."] = "",
        resource_ids: Annotated[str, "Comma-separated exact resource-id values to keep."] = "",
        max_nodes: Annotated[int, "Maximum nodes to return. 0 disables node output."] = 30,
        screenshot: Annotated[bool, "Include a screenshot. Implied when screenshot_save_path is set."] = False,
        screenshot_save_path: Annotated[str, "Device path for the JPEG. Trailing '/' or an existing dir auto-names shot_<ts>.jpg; '{ts}' expands to a ms timestamp. When set, base64 is omitted."] = "",
        screenshot_inline: Annotated[bool, "With screenshot_save_path, also inline base64 (rarely wanted)."] = False,
        screenshot_keep: Annotated[int, "When saving, keep at most this many shots in the directory; older ones are deleted. 0 disables the count limit."] = 5,
        screenshot_max_age: Annotated[int, "When saving, delete shots older than this many seconds. 0 disables the age limit."] = 600,
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

        save_path = str(screenshot_save_path).strip()
        if screenshot or save_path:
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
                if save_path:
                    # Write on the device and return only the path: a JPEG is
                    # far too large to inline into model context on every poll.
                    try:
                        keep = int(screenshot_keep)
                    except (TypeError, ValueError):
                        keep = 5
                    try:
                        max_age = int(screenshot_max_age)
                    except (TypeError, ValueError):
                        max_age = 600
                    saved = self._write_screenshot(save_path, image_data,
                                                   keep=keep, max_age=max_age)
                    payload["ScreenshotPath"] = saved["path"]
                    payload["ScreenshotPruned"] = saved["pruned"]
                    payload["ScreenshotDirCount"] = saved["count"]
                    payload["ScreenshotBase64Included"] = False
                    if screenshot_inline:
                        payload["ScreenshotBase64"] = base64.b64encode(image_data).decode("ascii")
                        payload["ScreenshotBase64Included"] = True
                else:
                    payload["ScreenshotBase64"] = base64.b64encode(image_data).decode("ascii")
                    payload["ScreenshotBase64Included"] = True
            except Exception as exc:  # noqa: BLE001
                payload["ScreenshotError"] = "{0}: {1}".format(type(exc).__name__, exc)

        return TextContent(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _write_screenshot(self, path, image_data, keep=5, max_age=600):
        """Write JPEG bytes to a device path, then prune the directory.

        Accepts a file path, a directory (trailing '/' or an existing dir), or
        a path containing '{ts}'; the last two get a timestamped filename.

        Retention matters: these shots accumulate at ~250 KB each and the
        device is not the storage owner. After every write the containing
        directory is trimmed to the newest `keep` *.jpg files and anything
        older than `max_age` seconds is removed. PC-side
        pull_screenshots.py then copies files off and deletes them.
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = path.replace("{ts}", stamp)
        if path.endswith("/") or os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            path = os.path.join(path, "shot_{0}.jpg".format(stamp))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fd:
            fd.write(image_data)
        pruned, count = self._prune_shots(parent, keep=keep, max_age=max_age)
        return {"path": path, "pruned": pruned, "count": count}

    def _prune_shots(self, directory, keep=5, max_age=600):
        """Trim `directory` to the newest `keep` jpg files / `max_age` seconds."""
        if not directory or not os.path.isdir(directory):
            return 0, 0
        shots = []
        try:
            for name in os.listdir(directory):
                if not name.lower().endswith((".jpg", ".jpeg")):
                    continue
                full = os.path.join(directory, name)
                try:
                    shots.append((os.path.getmtime(full), full))
                except OSError:
                    continue
        except OSError:
            return 0, 0
        shots.sort(reverse=True)
        now = time.time()
        pruned = 0
        keep = max(0, int(keep))
        max_age = max(0, int(max_age))
        for index, (mtime, full) in enumerate(shots):
            too_many = keep > 0 and index >= keep
            too_old = max_age > 0 and (now - mtime) > max_age
            if not (too_many or too_old):
                continue
            try:
                os.remove(full)
                pruned += 1
            except OSError:
                continue
        return pruned, len(shots) - pruned

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
