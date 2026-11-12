# Configuration

The config file is searched in this order: `$AMBIENCE_RGB_CONFIG`; the user's config directory
(`~/.config/ambience-rgb/config.toml` on Linux, `%APPDATA%\ambience-rgb\config.toml` on Windows); then
`config.example.toml` in the repository. One file serves both platforms. The daemon reads it when it starts.

| Key | Meaning |
|---|---|
| `socket.transport` | `auto` (the default) is the unix socket on Linux and UDP on Windows. `unix` forces the socket, and is refused on Windows, which has no unix datagram sockets; `udp` forces UDP. |
| `socket.path` | The unix socket, read only for that transport. Must equal MatterLights' `RGB_PUBLISH_SOCKET`. Environment variables are expanded -- `$XDG_RUNTIME_DIR` with a working fallback when root runs it at shutdown -- and any other unset variable is an error rather than a directory named `$`. A relative path resolves from the config file's directory. |
| `socket.udp_port` | The UDP port, always on `127.0.0.1`; read only for that transport. Default `45731`, below Windows' dynamic range (49152 and up), where Hyper-V and WSL reserve ports at boot. matterlights then publishes to `udp://127.0.0.1:<port>`. |
| `openrgb.host` / `openrgb.port` | SDK server. Keep the host on the loopback. |
| `openrgb.restart_command` | Run this when the server wedges beyond reconnecting. A list applies on every platform. A table picks by platform -- `[openrgb.restart_command]` with `linux = [...]` and `windows = [...]` -- and a platform without an entry only logs. Empty = log only. A plain string is refused: it would run as a tuple of its characters. |
| `rates.usb_hz` / `rates.smbus_hz` | Update rate **per bus**. |
| `commander.enabled` | Drive the AIO at all. |
| `commander.pump_group` / `commander.fan_group` | `near` or `far`. Not `excluded`: the device is written whole, so use `enabled = false`. |
| `katana_ha.*` | The soundbar through Home Assistant — see below. |
| `katana.enabled` | Removed with the old USB driver: `true` is refused with a pointer to `[katana_ha]`; a leftover section with `enabled = false` is ignored. |
| `devices[].match` | Case-insensitive **substring** of the OpenRGB controller name. |
| `devices[].group` | `near`, `far`, or `excluded`. |
| `devices[].bus` | `usb` or `smbus`, selecting which rate applies. |
| `devices[].zones` | Per-zone group overrides, e.g. `{ "IO Cover" = "near" }`. |
| `devices[].static` | A fixed colour (`"#rrggbb"` or `[r, g, b]`) set once per connection instead of synced. |
| `devices[].channel_order` | The order to send the device's channels in: a permutation of `RGB`, any case. `GRB` sends our green where red would go. Default `RGB`; anything that is not a permutation is refused. |
| `devices[].zone_channel_order` | Per-zone overrides of `channel_order`, e.g. `{ "IO Cover" = "GRB" }`. Zone names match case-insensitively. |
| `devices[].openrgb_rg_swap` | Zones whose red/green swap OpenRGB is made to do, e.g. `["IO Cover", "PCB"]`, by OpenRGB's exact zone names. The installers write them into OpenRGB's `ASRock Polychrome USB` / `RGSwap` settings -- the one per-zone swap OpenRGB exposes -- with OpenRGB stopped. A list, not a table: OpenRGB swaps a zone when the board's own table *or* the setting says so, so a setting can force the swap on and never off. Empty, the default, leaves OpenRGB's settings alone. It pairs with `zone_channel_order`: see *Red and green are swapped on some zones*. |
| `colour.saturation` | Push each channel away from the mean of the three. `1.0` disables it. |
| `colour.gamma` | Spread the channels apart. Raises saturation; does **not** dim a colour already at peak. `1.0` disables it. |
| `colour.group_cohesion` | Pull the near and far group colours toward each other. `0.0` keeps the engine's intent, `1.0` is one colour everywhere. |
| `spread.bias` | How hard each LED is pulled back to its group's single colour. `1.0` = flat (original behaviour), `0.0` = raw palette gradient. |

`match` is a case-insensitive substring on purpose: OpenRGB renames devices between versions, and an exact name would
silently stop matching. Keep `smbus_hz` well below `usb_hz` — SMBus is shared with the sensors fan control reads, and
the OpenRGB server acknowledges a packet before the device has applied it, so the bus load cannot be measured from here.

## `[katana_ha]` — the soundbar through Home Assistant

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Post the soundbar's colour to HA. Off unless set, so a config that predates it never posts anywhere |
| `group` | `"far"` | `near` or `far`. Not `excluded`: the light bar is one colour, so use `enabled = false` |
| `url` | — | HA's base URL, required when enabled, e.g. `http://homeassistant.local:8123`. `http://` or `https://` |
| `rate_hz` | `10.0` | The most posts per second -- a ceiling, not a rate. HA applies its own ceiling as well (the integration's screen-sync rate option) |
| `keepalive_seconds` | `5.0` | Re-post an unchanged colour this often; must stay under HA's 10 s hand-over, and a config that does not is refused |
| `timeout_seconds` | `2.0` | HTTP timeout for one post; also how long a stop waits for its black to go out |
| `token_file` | `.ha-token` in the checkout | Where the token is read from, if `$AMBIENCE_RGB_HA_TOKEN` is unset. `~` is expanded |

The token is read from `$AMBIENCE_RGB_HA_TOKEN`, else `token_file`. Colours are posted to
`POST <url>/api/sb_katana/stream` (`{"rgb": [r, g, b]}`) on a worker thread, so a slow Home Assistant never stalls the
other devices. Home Assistant answers `accepted: false` while its *Screen sync* switch is off — not an error.
