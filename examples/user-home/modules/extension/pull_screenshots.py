# -*- coding: utf-8 -*-
"""Pull observe_light screenshots off a device and delete them there.

observe_device_light(screenshot_save_path=...) writes JPEGs on the device and
returns only a path, so the image never enters model context. This script moves
those files to the PC (where there is room) and removes them from the device,
which keeps the phone from filling up.

Usage:
  python pull_screenshots.py <host> [remote_dir] [local_dir] [--keep]

    host        e.g. 192.168.0.108
    remote_dir  default /data/local/tmp/observe_light
    local_dir   default ./shots
    --keep      copy but leave the device copies in place

The device path must be readable by the FIRERPA service (uid=2000 shell):
/data/local/tmp/* is always fine; /sdcard/* also works on most devices.
"""
import os
import sys

sys.path.insert(0, r"C:\Users\Risho\.workbuddy\scripts\lamda_env")

from lamda.client import Device  # noqa: E402


def list_remote(device, remote_dir):
    script = "ls -1 {0}/*.jpg 2>/dev/null".format(remote_dir.rstrip("/"))
    result = device.execute_script(script)
    out = result.stdout.decode("utf-8", "ignore") if isinstance(result.stdout, bytes) else result.stdout
    return [line.strip() for line in out.splitlines() if line.strip().endswith(".jpg")]


def main():
    argv = [a for a in sys.argv[1:] if a != "--keep"]
    keep_remote = "--keep" in sys.argv
    host = argv[0] if argv else "192.168.0.108"
    remote_dir = argv[1] if len(argv) > 1 else "/data/local/tmp/observe_light"
    local_dir = argv[2] if len(argv) > 2 else "shots"

    os.makedirs(local_dir, exist_ok=True)
    device = Device(host, port=65000)
    remote_files = list_remote(device, remote_dir)
    if not remote_files:
        print("no screenshots in {0} on {1}".format(remote_dir, host))
        return

    total = 0
    for remote in remote_files:
        name = os.path.basename(remote)
        local = os.path.join(local_dir, name)
        try:
            device.download_file(remote, local)
            size = os.path.getsize(local)
            total += size
            print("pulled {0} ({1} bytes) -> {2}".format(remote, size, local))
            if not keep_remote:
                device.execute_script("rm -f '{0}'".format(remote))
        except Exception as exc:  # noqa: BLE001
            print("FAILED {0}: {1}".format(remote, exc))

    print("pulled {0} file(s), {1} bytes; device copies {2}".format(
        len(remote_files), total, "kept" if keep_remote else "deleted"))


if __name__ == "__main__":
    main()
