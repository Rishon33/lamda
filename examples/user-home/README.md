These directory structures are consistent with those on the device; you can view them by running the command `ls -l ~/modules` in the built-in shell.

See Doc: https://device-farm.com/docs/en/model-extension or https://device-farm.com/docs/en/frida-persist

`modules/extension/observe_light.py` is an additional MCP extension that returns a much smaller
payload than the built-in `observeDevice` (no installed-app list, no full hierarchy), for use in
high-frequency polling loops. `observe_light_selftest.py` measures its payload size and latency
for each mode inside the running FIRERPA environment.
