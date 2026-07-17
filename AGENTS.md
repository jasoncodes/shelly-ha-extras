# Project instructions

## Live smoke tests

Read-only live checks are permitted with `devices discover` and `devices info`.
Do not upload firmware, reboot devices, or mutate schedules without explicit
authorization for the target device.

When testing the container, use host networking as shown in
`compose.example.yml` and a local `config.toml` based on
`config.example.toml`. If bridge networking is used instead, attach the
container to the external Docker network named `iot`.
