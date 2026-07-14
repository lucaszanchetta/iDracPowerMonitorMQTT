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
- SIGHUP handler registered; config is re-read from disk on every invocation (single-shot run model — see Scheduling)
- Docker container and systemd timer deployment options
- MQTT authentication (username/password) and configurable QoS

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/lucaszanchetta/iDracPowerMonitorMQTT.git
cd iDracPowerMonitorMQTT

# 2. Create your server config
cp servers.example.json servers.json
# Edit servers.json with your iDRAC hostnames and IPs

# 3. Install dependencies
pip3 install -r requirements.txt

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
| `IDRAC_MQTT_QOS` | `1` | MQTT QoS level |
| `IDRAC_MQTT_USERNAME` | *(empty)* | MQTT authentication username (omit for anonymous) |
| `IDRAC_MQTT_PASSWORD` | *(empty)* | MQTT authentication password (omit for anonymous) |
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
| `IDRAC_SSH_CONNECT_TIMEOUT_SECONDS` | `5` | SSH connection timeout (fail fast on unreachable hosts) |
| `IDRAC_DEVICE_MODEL` | `iDRAC` | Device model name shown in Home Assistant |

> **Hot-reload:** A `SIGHUP` handler is registered, but the script runs as a single-shot invocation — each run re-reads `servers.json` fresh from disk. `SIGHUP` has no additional effect in the current single-shot model; for persistent-daemon hot-reload, wrap the script in a supervisor that re-executes it on `SIGHUP`.

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
| `homelab/idrac/fleet/status` | `online` or `error` (fleet availability) |
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

# Quiet mode — suppress JSON summary (for cron)
python3 powerMQTT.py --quiet

# Show version
python3 powerMQTT.py --version

# Compatibility helper for the old shell entry point
./powerConsumption.sh idrac1
```

## Scheduling

Example cron entry (every minute, with lock to prevent overlap):

```cron
* * * * * /usr/bin/flock -n /tmp/idrac-power-mqtt.lock /usr/bin/timeout 55s /usr/bin/python3 /path/to/powerMQTT.py >> /path/to/powerMQTT.log 2>&1
```

## Docker

A `Dockerfile` is provided for containerized deployments.

### Build

```bash
docker build -t idrac-power-mqtt .
```

### Run

Mount your `servers.json` inside the container and set environment variables:

```bash
docker run --rm \
  -v /path/to/servers.json:/config/servers.json:ro \
  -e IDRAC_MQTT_HOST=mosquitto \
  -e IDRAC_MQTT_USERNAME=user \
  -e IDRAC_MQTT_PASSWORD=pass \
  -e IDRAC_MQTT_QOS=1 \
  idrac-power-mqtt
```

The default `CMD` is `--config /config/servers.json`. The image is based on
`python:3.10-slim` and includes `mosquitto-clients` (fallback MQTT publisher)
and `openssh-client` for SSH access to iDRACs. All environment variables from
the [table above](#environment-variables) apply.

## Systemd

The repository includes systemd unit files for running the collector on a timer.

### Files

| File | Purpose |
|------|---------|
| `systemd/idrac-power-mqtt.service` | Oneshot service unit |
| `systemd/idrac-power-mqtt.timer` | Minute-based timer |
| `systemd/env.example` | Environment file template |

### Installation

```bash
# Install the script to the path expected by the service unit
sudo install -m 0755 powerMQTT.py /usr/local/bin/powerMQTT.py

# Copy unit files
sudo cp systemd/idrac-power-mqtt.service systemd/idrac-power-mqtt.timer /etc/systemd/system/

# Create config directory and populate
sudo mkdir -p /etc/idrac-power-mqtt
sudo cp systemd/env.example /etc/idrac-power-mqtt/env
sudo cp servers.json /etc/idrac-power-mqtt/servers.json

# Edit the environment file with your MQTT and iDRAC settings
sudo $EDITOR /etc/idrac-power-mqtt/env
sudo $EDITOR /etc/idrac-power-mqtt/servers.json

# Reload systemd and enable the timer
sudo systemctl daemon-reload
sudo systemctl enable --now idrac-power-mqtt.timer
```

### Logs

Output is captured via `journald`:

```bash
journalctl -u idrac-power-mqtt.service --since "5 minutes ago"
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
pip3 install -r requirements.txt
```

The old-style manual install also works:

```bash
pip3 install paho-mqtt pyghmi pexpect
```

## Safety

This tool communicates only with iDRAC management controllers via SSH and IPMI.
It does not flash firmware or interact with the host OS.

## License

This project is licensed under the GNU General Public License v3.0.
See [LICENSE](LICENSE) for details.
