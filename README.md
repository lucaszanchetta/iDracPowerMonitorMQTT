# iDracPowerMonitorMQTT

Collect power, temperature, and PSU electrical metrics from Dell iDRAC6 servers
and publish them to an MQTT broker. Includes Home Assistant MQTT auto-discovery
for zero-config sensor setup.

## Features

- Reads power draw (instantaneous, averages, peaks, caps) via RACADM over SSH
- Reads ambient temperature, PSU current, and PSU voltage via IPMI
- Publishes per-host and fleet-aggregate MQTT topics
- Generates Home Assistant MQTT discovery payloads automatically
- Supports direct and interactive (legacy firmware) SSH modes
- Configurable via `servers.json` and environment variables
- Built-in timeouts and error isolation per host

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/lucaszanchetta/iDracPowerMonitorMQTT.git
cd iDracPowerMonitorMQTT

# 2. Create your server config
cp servers.example.json servers.json
# Edit servers.json with your iDRAC hostnames and IPs

# 3. Install dependencies
pip3 install paho-mqtt pyghmi pexpect

# 4. Run a dry run
python3 powerMQTT.py --dry-run --verbose

# 5. Publish to MQTT
python3 powerMQTT.py
```

## Configuration

### Server config (`servers.json`)

Copy `servers.example.json` to `servers.json` and edit it:

```json
[
  {"name": "idrac1", "mode": "direct", "ipmi_host": "192.168.1.119"},
  {"name": "idrac2", "mode": "direct", "ipmi_host": "192.168.1.120"},
  {"name": "idrac3", "mode": "interactive", "ipmi_host": "192.168.1.122"}
]
```

Each entry requires:

| Field | Description |
|-------|-------------|
| `name` | SSH alias from `~/.ssh/config` (used for RACADM and MQTT topics) |
| `mode` | `direct` (default) for normal SSH, `interactive` for legacy firmware requiring a RAC shell |
| `ipmi_host` | IP or hostname for IPMI queries (defaults to `name` if omitted) |

Use `--config /path/to/servers.json` to specify a custom config path.

### SSH setup

SSH aliases must be configured in `~/.ssh/config` and key-based authentication
must be enabled on each iDRAC. Example:

```ssh-config
Host idrac1
    HostName 192.168.1.119
    User root
    IdentityFile ~/.ssh/id_rsa
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IDRAC_MQTT_HOST` | `127.0.0.1` | MQTT broker address |
| `IDRAC_MQTT_PORT` | `1883` | MQTT broker port |
| `IDRAC_MQTT_TOPIC_PREFIX` | `homelab/idrac` | MQTT topic prefix |
| `IDRAC_DIRECT_SSH_TIMEOUT_SECONDS` | `10` | Timeout for direct RACADM SSH calls |
| `IDRAC_INTERACTIVE_SSH_TIMEOUT_SECONDS` | `30` | Timeout for interactive RAC shell SSH calls |
| `IDRAC_IPMI_COLLECTION_TIMEOUT_SECONDS` | `15` | Timeout for IPMI sensor collection |
| `IDRAC_MQTT_PUBLISH_TIMEOUT_SECONDS` | `10` | Timeout for MQTT publish |
| `IDRAC_IPMI_USER` | `root` | IPMI username |
| `IDRAC_IPMI_PASSWORD` | `calvin` | IPMI password |
| `IDRAC_AMBIENT_TEMPERATURE_SENSOR` | `Ambient Temp` | IPMI sensor name for ambient temperature |
| `IDRAC_PUBLISH_HOME_ASSISTANT_DISCOVERY` | `1` | Set to `0` to disable HA discovery |
| `HOME_ASSISTANT_DISCOVERY_PREFIX` | `homeassistant` | HA discovery MQTT prefix |
| `IDRAC_DISCOVERY_EXPIRE_AFTER_SECONDS` | `180` | HA sensor expiry time |

## Topics

### Per host

| Topic | Content |
|-------|---------|
| `homelab/idrac/<host>/status` | `online` or `error` |
| `homelab/idrac/<host>/power` | Full power JSON payload |
| `homelab/idrac/<host>/power/actual_watts` | Instantaneous power draw |
| `homelab/idrac/<host>/power/last_min_avg_watts` | 1-minute rolling average |
| `homelab/idrac/<host>/temperature` | Temperature JSON payload |
| `homelab/idrac/<host>/temperature/ambient_c` | Ambient temperature |
| `homelab/idrac/<host>/electrical` | Electrical JSON payload |
| `homelab/idrac/<host>/electrical/psu1_input_current_a` | PSU 1 input current |
| `homelab/idrac/<host>/electrical/psu2_input_current_a` | PSU 2 input current |
| `homelab/idrac/<host>/electrical/psu1_input_voltage_v` | PSU 1 input voltage |
| `homelab/idrac/<host>/electrical/psu2_input_voltage_v` | PSU 2 input voltage |
| `homelab/idrac/<host>/electrical/average_input_voltage_v` | Average PSU voltage |

### Fleet

| Topic | Content |
|-------|---------|
| `homelab/idrac/summary` | Fleet summary JSON |
| `homelab/idrac/total/actual_watts` | Total fleet power draw |
| `homelab/idrac/total/last_min_avg_watts` | Total fleet 1-min average |
| `homelab/idrac/fleet/average_ambient_temp_c` | Fleet average ambient temp |
| `homelab/idrac/fleet/average_input_voltage_v` | Fleet average input voltage |

### Home Assistant discovery

Discovery configs are published under `homeassistant/sensor/` for each host
and the fleet. All topics are retained.

## CLI usage

```bash
# Full run — collect from all hosts and publish to MQTT
python3 powerMQTT.py

# Dry run — print payload without publishing
python3 powerMQTT.py --dry-run --verbose

# Single host
python3 powerMQTT.py --host idrac3

# Single metric (plain value for scripting)
python3 powerMQTT.py --host idrac3 --metric psu1_input_voltage_v --plain

# Custom config path
python3 powerMQTT.py --config /etc/idrac/servers.json

# Compatibility helper for the old shell entry point
./powerConsumption.sh idrac1
```

## Scheduling

Example cron entry (every minute, with lock to prevent overlap):

```cron
* * * * * /usr/bin/flock -n /tmp/idrac-power-mqtt.lock /usr/bin/timeout 55s /usr/bin/python3 /path/to/powerMQTT.py >> /path/to/powerMQTT.log 2>&1
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `paho-mqtt` | MQTT publishing (optional — falls back to `mosquitto_pub` CLI) |
| `pyghmi` | IPMI sensor queries for temperature and PSU data |
| `pexpect` | Interactive SSH sessions for legacy iDRAC firmware |
| `ssh` | System SSH client |
| `mosquitto_pub` | Fallback MQTT CLI if `paho-mqtt` is not installed |

Install Python dependencies:

```bash
pip3 install paho-mqtt pyghmi pexpect
```

## Safety

This tool communicates only with iDRAC management controllers via SSH and IPMI.
It does not flash firmware or interact with the host OS.

## License

This project is licensed under the GNU General Public License v3.0.
See [LICENSE](LICENSE) for details.
