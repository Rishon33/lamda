# -*- coding: utf-8 -*-
# Copyright 2026 Rishon33. All rights reserved.
#
# Distributed under MIT license.
# See file LICENSE for detail or copy at https://opensource.org/licenses/MIT
"""In-process behaviour test for observe_light.py, run inside the FIRERPA env.

This does not need an MCP restart: it imports the extension module, attaches a
real Device client to the already-running service, and calls the tool method
directly. It measures payload size and latency for each mode.
"""
import importlib.util
import json
import sys
import time

from lamda.client import Device


def load_extension(path):
    spec = importlib.util.spec_from_file_location("observe_light_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from lamda.extensions import BaseMcpExtension
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, BaseMcpExtension) and obj is not BaseMcpExtension:
            return obj
    raise RuntimeError("extension class not found")


def call(ext, **kwargs):
    started = time.time()
    result = ext.observe_device_light(None, **kwargs)
    elapsed = time.time() - started
    text = getattr(result, "text", None)
    if text is None:
        text = str(result)
    return elapsed, text


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/data/local/tmp/observe_light.py"
    cls = load_extension(path)
    # BaseMcpExtension derives from tornado's RequestHandler, so the normal
    # constructor wants the service's application/request pair. Bypass it: the
    # tool method only needs `self.device`.
    ext = cls.__new__(cls)
    ext.device = Device("127.0.0.1", port=65000)

    cases = [
        ("none", dict(hierarchy_mode="none")),
        ("summary", dict(hierarchy_mode="summary", max_nodes=30)),
        ("nodes:taobao", dict(hierarchy_mode="nodes",
                              node_patterns="金币,任务,领取,去完成,浏览",
                              max_nodes=20)),
        ("screenshot q50 scale1.0", dict(hierarchy_mode="none", screenshot=True,
                                         screenshot_quality=50, screenshot_scale=1.0)),
        ("screenshot q50 scale0.5", dict(hierarchy_mode="none", screenshot=True,
                                         screenshot_quality=50, screenshot_scale=0.5)),
    ]

    for label, kwargs in cases:
        try:
            elapsed, text = call(ext, **kwargs)
            payload = json.loads(text)
            payload.pop("ScreenshotBase64", None)
            print("== {0} ==".format(label))
            print("   elapsed: {0:.2f}s".format(elapsed))
            print("   text bytes: {0}".format(len(text)))
            print("   payload: {0}".format(json.dumps(payload, ensure_ascii=False)[:900]))
        except Exception as exc:  # noqa: BLE001
            print("== {0} == FAILED {1}: {2}".format(label, type(exc).__name__, exc))


if __name__ == "__main__":
    main()
