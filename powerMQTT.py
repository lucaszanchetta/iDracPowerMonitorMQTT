#!/usr/bin/env python3

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pexpect


MQTT_HOST = os.environ.get("IDRAC_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("IDRAC_MQTT_PORT", "1883"))
TOPIC_PREFIX = os.environ.get("IDRAC_MQTT_TOPIC_PREFIX", "homelab/idrac")
HOME_ASSISTANT_DISCOVERY_PREFIX = os.environ.get(
    "HOME_ASSISTANT_DISCOVERY_PREFIX", "homeassistant"
)
PUBLISH_HOME_ASSISTANT_DISCOVERY = (
    os.environ.get("IDRAC_PUBLISH_HOME_ASSISTANT_DISCOVERY", "1") != "0"
)
DIRECT_SSH_TIMEOUT_SECONDS = int(os.environ.get("IDRAC_DIRECT_SSH_TIMEOUT_SECONDS", "10"))
INTERACTIVE_SSH_TIMEOUT_SECONDS = int(
    os.environ.get("IDRAC_INTERACTIVE_SSH_TIMEOUT_SECONDS", "30")
)
IPMI_COLLECTION_TIMEOUT_SECONDS = int(
    os.environ.get("IDRAC_IPMI_COLLECTION_TIMEOUT_SECONDS", "15")
)
MQTT_PUBLISH_TIMEOUT_SECONDS = int(
    os.environ.get("IDRAC_MQTT_PUBLISH_TIMEOUT_SECONDS", "10")
)
IDRAC_PROMPT = r"/admin1->\s*"
IPMI_USER = os.environ.get("IDRAC_IPMI_USER", "root")
IPMI_PASSWORD = os.environ.get("IDRAC_IPMI_PASSWORD", "calvin")
AMBIENT_TEMPERATURE_SENSOR = os.environ.get(
    "IDRAC_AMBIENT_TEMPERATURE_SENSOR", "Ambient Temp"
)
DISCOVERY_EXPIRE_AFTER_SECONDS = int(
    os.environ.get("IDRAC_DISCOVERY_EXPIRE_AFTER_SECONDS", "180")
)

SERVERS: list[dict] = []


def load_servers(config_path: str) -> list[dict]:
    path = Path(config_path)
    if not path.exists():
        example = path.with_name("servers.example.json")
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"Copy the example and edit it:\n"
            f"  cp {example} {path}"
        )
    try:
        servers = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(servers, list) or not servers:
        raise SystemExit(f"Config must be a non-empty JSON array: {path}")
    for entry in servers:
        if not isinstance(entry, dict) or "name" not in entry:
            raise SystemExit(f"Each server entry must be an object with 'name': {entry}")
        entry.setdefault("mode", "direct")
        entry.setdefault("ipmi_host", entry["name"])
    return servers

NUMERIC_KEYS = {
    "cfgServerPowerStatus": "power_status",
    "cfgServerActualPowerConsumption": "actual_watts",
    "cfgServerPowerLastMinAvg": "last_min_avg_watts",
    "cfgServerPowerLastHourAvg": "last_hour_avg_watts",
    "cfgServerPowerLastDayAvg": "last_day_avg_watts",
    "cfgServerPowerLastWeekAvg": "last_week_avg_watts",
    "cfgServerPeakPowerConsumption": "peak_watts",
    "cfgServerPowerCapWatts": "power_cap_watts",
    "cfgServerActualAmperageConsumption": "actual_amps",
    "cfgServerPeakAmperage": "peak_amps",
}

RAW_KEYS = {
    "cfgServerPeakPowerConsumptionTimestamp": "peak_power_timestamp",
    "cfgServerPeakAmperageTimeStamp": "peak_amperage_timestamp",
    "cfgServerCumulativePowerConsumption": "cumulative_power_consumption_raw",
    "cfgServerCumulativePowerConsumptionTimeStamp": "cumulative_power_timestamp",
}

SINGLE_HOST_EXTRA_METRICS = (
    "ambient_temp_c",
    "average_input_voltage_v",
    "psu1_input_current_a",
    "psu2_input_current_a",
    "psu1_input_voltage_v",
    "psu2_input_voltage_v",
)
SINGLE_HOST_METRICS = tuple(sorted((*NUMERIC_KEYS.values(), *SINGLE_HOST_EXTRA_METRICS)))


class QueryError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect iDRAC power, temperature, and PSU electrical metrics and publish them to MQTT."
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "servers.json"),
        help="Path to servers JSON config file (default: servers.json next to this script).",
    )
    parser.add_argument(
        "--host",
        help="Query a single iDRAC alias instead of publishing all hosts.",
    )
    parser.add_argument(
        "--metric",
        choices=SINGLE_HOST_METRICS,
        help="With --host, print one parsed metric.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Print only the requested value when used with --host and --metric.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the collected payload instead of publishing to MQTT.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-host collection details.",
    )
    parser.add_argument(
        "--internal-query-ipmi",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--internal-publish-messages",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def server_config(host: str) -> dict:
    for server in SERVERS:
        if server["name"] == host:
            return server
    raise QueryError(f"unknown host {host}")


def script_path() -> str:
    return str(Path(__file__).resolve())


def terminate_process_group(process) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def parse_json_output(stdout: str, label: str, host: str | None = None) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        target = f" for {host}" if host is not None else ""
        raise QueryError(f"{label} returned invalid json{target}: {exc}") from exc


def run_internal_subprocess(
    *,
    extra_args: list[str],
    label: str,
    timeout_seconds: int,
    host: str | None = None,
    stdin_payload: str | None = None,
    config_path: str | None = None,
) -> str:
    command = [sys.executable or "python3", script_path()]
    if config_path is not None:
        command.extend(["--config", config_path])
    if host is not None:
        command.extend(["--host", host])
    command.extend(extra_args)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(stdin_payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        process.communicate()
        target = f" for {host}" if host is not None else ""
        raise QueryError(f"{label} timed out{target} after {timeout_seconds}s") from exc

    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"rc={process.returncode}"
        target = f" for {host}" if host is not None else ""
        raise QueryError(f"{label} failed{target}: {detail}")

    return stdout


def run_ipmi_subprocess(host: str, config_path: str | None = None) -> dict:
    stdout = run_internal_subprocess(
        extra_args=["--internal-query-ipmi"],
        label="ipmi collection",
        timeout_seconds=IPMI_COLLECTION_TIMEOUT_SECONDS,
        host=host,
        config_path=config_path,
    )
    return parse_json_output(stdout, "ipmi collection", host)


def run_direct_racadm(host: str) -> str:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        host,
        "racadm getconfig -g cfgServerPower",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=DIRECT_SSH_TIMEOUT_SECONDS,
    )
    output = result.stdout.strip()
    if result.returncode == 0 and "cfgServerActualPowerConsumption" in output:
        return output
    stderr = result.stderr.strip()
    raise QueryError(
        f"direct query failed for {host}: rc={result.returncode} stderr={stderr or 'empty'}"
    )


def run_interactive_racadm(host: str) -> str:
    if shutil.which("ssh") is None:
        raise QueryError("ssh is not available")

    child = pexpect.spawn(
        "ssh",
        ["-tt", "-o", "BatchMode=yes", host],
        encoding="utf-8",
        timeout=INTERACTIVE_SSH_TIMEOUT_SECONDS,
    )
    try:
        child.expect(IDRAC_PROMPT)
        child.sendline("racadm getconfig -g cfgServerPower")
        child.expect(IDRAC_PROMPT)
        output = child.before.strip()
        child.sendline("exit")
        child.expect(pexpect.EOF)
        if "cfgServerActualPowerConsumption" not in output:
            raise QueryError(f"interactive query returned no power data for {host}")
        return output
    except pexpect.TIMEOUT as exc:
        raise QueryError(f"interactive query timed out for {host}") from exc
    finally:
        if child.isalive():
            child.close(force=True)


def query_power_output(host: str) -> tuple[str, str]:
    mode = server_config(host)["mode"]
    if mode == "interactive":
        return "interactive", run_interactive_racadm(host)

    try:
        return "direct", run_direct_racadm(host)
    except (QueryError, subprocess.TimeoutExpired):
        return "interactive", run_interactive_racadm(host)


def first_number(raw_value: str) -> float | int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", raw_value)
    if not match:
        return None
    value = float(match.group(0))
    if value.is_integer():
        return int(value)
    return value


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def parse_cfg_output(host: str, query_mode: str, output: str) -> dict:
    payload = {
        "host": host,
        "query_mode": query_mode,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    for line in output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        if line.startswith("#"):
            line = line[1:].strip()
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key in NUMERIC_KEYS:
            payload[NUMERIC_KEYS[key]] = first_number(raw_value)
            payload[f"{NUMERIC_KEYS[key]}_raw"] = raw_value
        elif key in RAW_KEYS:
            payload[RAW_KEYS[key]] = raw_value

    if "actual_watts" not in payload:
        raise QueryError(f"unable to parse actual_watts for {host}")
    return payload


def collect_ipmi_metrics(server: dict) -> dict:
    try:
        from pyghmi.ipmi import command as ipmi_command
        import pyghmi.exceptions as ipmi_exceptions
    except ImportError:
        return {"ipmi_metrics_error": "pyghmi is not installed"}

    ipmi = None
    ambient_temp_c = None
    ambient_temp_error = None
    psu_currents = []
    psu_voltages = []

    try:
        ipmi = ipmi_command.Command(
            bmc=server["ipmi_host"],
            userid=IPMI_USER,
            password=IPMI_PASSWORD,
            keepalive=False,
        )
        ipmi.init_sdr()
        for sensor_key in ipmi._sdr.get_sensor_numbers():
            sensor = ipmi._sdr.sensors[sensor_key]
            try:
                response = ipmi.raw_command(
                    command=0x2D,
                    netfn=4,
                    rslun=sensor.sensor_lun,
                    data=(sensor.sensor_number,),
                )
            except Exception:
                continue
            if "error" in response:
                continue

            reading = sensor.decode_sensor_reading(ipmi, response["data"])
            if getattr(reading, "unavailable", 0):
                continue

            sensor_name = getattr(sensor, "sensor_name", "")
            sensor_type = getattr(sensor, "sensor_type", "")
            units = getattr(reading, "units", "")
            value = getattr(reading, "value", None)

            if sensor_type == "Temperature" and sensor_name == AMBIENT_TEMPERATURE_SENSOR:
                if ambient_temp_c is None and value is not None:
                    ambient_temp_c = value
                continue

            if sensor_type == "Current" and sensor_name == "Current" and units == "A":
                if value is not None:
                    psu_currents.append(value)
                continue

            if sensor_type == "Voltage" and sensor_name == "Voltage" and units == "V":
                if value is not None:
                    psu_voltages.append(value)
                continue

        if ambient_temp_c is None:
            ambient_temp_error = f"{AMBIENT_TEMPERATURE_SENSOR} is unavailable"
    except ipmi_exceptions.IpmiException as exc:
        return {"ipmi_metrics_error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ipmi_metrics_error": str(exc)}
    finally:
        if ipmi is not None and getattr(ipmi, "ipmi_session", None) is not None:
            try:
                ipmi.ipmi_session.logout()
            except Exception:  # noqa: BLE001
                pass

    metrics = {}
    if ambient_temp_c is not None:
        metrics["ambient_temp_c"] = ambient_temp_c
        metrics["ambient_temp_sensor"] = AMBIENT_TEMPERATURE_SENSOR
        metrics["ambient_temp_units"] = "°C"
    if ambient_temp_error:
        metrics["ambient_temp_error"] = ambient_temp_error

    if psu_currents:
        metrics["psu_input_currents_a"] = psu_currents
    if len(psu_currents) >= 1:
        metrics["psu1_input_current_a"] = psu_currents[0]
    if len(psu_currents) >= 2:
        metrics["psu2_input_current_a"] = psu_currents[1]

    if psu_voltages:
        metrics["psu_input_voltages_v"] = psu_voltages
        metrics["average_input_voltage_v"] = average(psu_voltages)
    if len(psu_voltages) >= 1:
        metrics["psu1_input_voltage_v"] = psu_voltages[0]
    if len(psu_voltages) >= 2:
        metrics["psu2_input_voltage_v"] = psu_voltages[1]

    return metrics


def collect_host(host: str, config_path: str | None = None) -> dict:
    server = server_config(host)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        power_future = executor.submit(query_power_output, host)
        ipmi_future = executor.submit(run_ipmi_subprocess, host, config_path)
        query_mode, output = power_future.result()
        try:
            ipmi_metrics = ipmi_future.result()
        except Exception as exc:  # noqa: BLE001
            ipmi_metrics = {
                "ipmi_metrics_error": str(exc),
                "ambient_temp_error": str(exc),
            }

    snapshot = parse_cfg_output(host, query_mode, output)
    snapshot["ipmi_host"] = server["ipmi_host"]
    snapshot.update(ipmi_metrics)
    return snapshot


def queue_topic(messages: list[dict], topic: str, payload: str) -> None:
    messages.append({"topic": topic, "payload": payload, "retain": True})


def publish_messages_direct(messages: list[dict]) -> None:
    if not messages:
        return

    try:
        from paho.mqtt import publish as mqtt_publish
    except ImportError:
        for message in messages:
            command = [
                "mosquitto_pub",
                "-h",
                MQTT_HOST,
                "-p",
                str(MQTT_PORT),
                "-r",
                "-t",
                message["topic"],
                "-m",
                message["payload"],
            ]
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=MQTT_PUBLISH_TIMEOUT_SECONDS,
            )
        return

    mqtt_publish.multiple(
        messages,
        hostname=MQTT_HOST,
        port=MQTT_PORT,
        client_id="idrac-power-mqtt",
        keepalive=MQTT_PUBLISH_TIMEOUT_SECONDS,
    )


def publish_messages(messages: list[dict], config_path: str | None = None) -> None:
    payload = json.dumps(messages, separators=(",", ":"))
    run_internal_subprocess(
        extra_args=["--internal-publish-messages"],
        label="mqtt publish",
        timeout_seconds=MQTT_PUBLISH_TIMEOUT_SECONDS,
        stdin_payload=payload,
        config_path=config_path,
    )


def mqtt_publish_is_available() -> bool:
    try:
        from paho.mqtt import publish as mqtt_publish  # noqa: F401
    except ImportError:
        return shutil.which("mosquitto_pub") is not None
    return True


def availability_payload(base_topic: str) -> list[dict]:
    return [
        {
            "topic": f"{base_topic}/status",
            "payload_available": "online",
            "payload_not_available": "error",
        }
    ]


def host_device(host: str) -> dict:
    return {
        "identifiers": [f"dell-idrac6-{host}"],
        "manufacturer": "Dell",
        "model": "iDRAC6",
        "name": f"{host} iDRAC",
    }


def fleet_device() -> dict:
    return {
        "identifiers": ["dell-idrac6-fleet"],
        "manufacturer": "Dell",
        "model": "iDRAC6",
        "name": "iDRAC Fleet",
    }


def ha_sensor_config(
    *,
    name: str,
    unique_id: str,
    state_topic: str,
    device: dict,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = "measurement",
    availability: list[dict] | None = None,
) -> dict:
    payload = {
        "name": name,
        "unique_id": unique_id,
        "state_topic": state_topic,
        "device": device,
        "expire_after": DISCOVERY_EXPIRE_AFTER_SECONDS,
    }
    if unit is not None:
        payload["unit_of_measurement"] = unit
    if device_class is not None:
        payload["device_class"] = device_class
    if state_class is not None:
        payload["state_class"] = state_class
    if availability is not None:
        payload["availability"] = availability
    return payload


def publish_home_assistant_discovery(messages: list[dict]) -> None:
    for server in SERVERS:
        host = server["name"]
        base_topic = f"{TOPIC_PREFIX}/{host}"
        device = host_device(host)
        availability = availability_payload(base_topic)

        configs = {
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_actual_watts/config": ha_sensor_config(
                name=f"{host} Actual Power",
                unique_id=f"{host}_actual_watts",
                state_topic=f"{base_topic}/power/actual_watts",
                unit="W",
                device_class="power",
                availability=availability,
                device=device,
            ),
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_ambient_temp_c/config": ha_sensor_config(
                name=f"{host} Ambient Temperature",
                unique_id=f"{host}_ambient_temp_c",
                state_topic=f"{base_topic}/temperature/ambient_c",
                unit="°C",
                device_class="temperature",
                availability=availability,
                device=device,
            ),
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_psu1_input_current_a/config": ha_sensor_config(
                name=f"{host} PSU 1 Input Current",
                unique_id=f"{host}_psu1_input_current_a",
                state_topic=f"{base_topic}/electrical/psu1_input_current_a",
                unit="A",
                device_class="current",
                availability=availability,
                device=device,
            ),
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_psu2_input_current_a/config": ha_sensor_config(
                name=f"{host} PSU 2 Input Current",
                unique_id=f"{host}_psu2_input_current_a",
                state_topic=f"{base_topic}/electrical/psu2_input_current_a",
                unit="A",
                device_class="current",
                availability=availability,
                device=device,
            ),
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_psu1_input_voltage_v/config": ha_sensor_config(
                name=f"{host} PSU 1 Input Voltage",
                unique_id=f"{host}_psu1_input_voltage_v",
                state_topic=f"{base_topic}/electrical/psu1_input_voltage_v",
                unit="V",
                device_class="voltage",
                availability=availability,
                device=device,
            ),
            f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/{host}_psu2_input_voltage_v/config": ha_sensor_config(
                name=f"{host} PSU 2 Input Voltage",
                unique_id=f"{host}_psu2_input_voltage_v",
                state_topic=f"{base_topic}/electrical/psu2_input_voltage_v",
                unit="V",
                device_class="voltage",
                availability=availability,
                device=device,
            ),
        }

        for topic, payload in configs.items():
            queue_topic(messages, topic, json.dumps(payload, separators=(",", ":")))

    fleet = fleet_device()
    fleet_configs = {
        f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/idrac_total_actual_watts/config": ha_sensor_config(
            name="iDRAC Total Actual Power",
            unique_id="idrac_total_actual_watts",
            state_topic=f"{TOPIC_PREFIX}/total/actual_watts",
            unit="W",
            device_class="power",
            device=fleet,
            availability=None,
        ),
        f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/idrac_fleet_average_ambient_temp_c/config": ha_sensor_config(
            name="iDRAC Fleet Average Ambient Temperature",
            unique_id="idrac_fleet_average_ambient_temp_c",
            state_topic=f"{TOPIC_PREFIX}/fleet/average_ambient_temp_c",
            unit="°C",
            device_class="temperature",
            device=fleet,
            availability=None,
        ),
        f"{HOME_ASSISTANT_DISCOVERY_PREFIX}/sensor/idrac_fleet_average_input_voltage_v/config": ha_sensor_config(
            name="iDRAC Fleet Average Input Voltage",
            unique_id="idrac_fleet_average_input_voltage_v",
            state_topic=f"{TOPIC_PREFIX}/fleet/average_input_voltage_v",
            unit="V",
            device_class="voltage",
            device=fleet,
            availability=None,
        ),
    }
    for topic, payload in fleet_configs.items():
        queue_topic(messages, topic, json.dumps(payload, separators=(",", ":")))


def publish_host_metrics(messages: list[dict], snapshot: dict) -> None:
    host = snapshot["host"]
    base_topic = f"{TOPIC_PREFIX}/{host}"
    queue_topic(messages, f"{base_topic}/status", "online")
    queue_topic(messages, f"{base_topic}/power", json.dumps(snapshot, separators=(",", ":")))

    power_scalar_keys = (
        "actual_watts",
        "last_min_avg_watts",
        "last_hour_avg_watts",
        "last_day_avg_watts",
        "last_week_avg_watts",
        "peak_watts",
        "power_cap_watts",
        "actual_amps",
        "peak_amps",
        "power_status",
    )
    for key in power_scalar_keys:
        if key in snapshot and snapshot[key] is not None:
            queue_topic(messages, f"{base_topic}/power/{key}", str(snapshot[key]))

    temperature_payload = {
        "host": host,
        "collected_at": snapshot["collected_at"],
        "ambient_temp_c": snapshot.get("ambient_temp_c"),
        "ambient_temp_error": snapshot.get("ambient_temp_error"),
    }
    queue_topic(
        messages,
        f"{base_topic}/temperature",
        json.dumps(temperature_payload, separators=(",", ":")),
    )
    if snapshot.get("ambient_temp_c") is not None:
        queue_topic(messages, f"{base_topic}/temperature/ambient_c", str(snapshot["ambient_temp_c"]))

    electrical_payload = {
        "host": host,
        "collected_at": snapshot["collected_at"],
        "psu1_input_current_a": snapshot.get("psu1_input_current_a"),
        "psu2_input_current_a": snapshot.get("psu2_input_current_a"),
        "psu1_input_voltage_v": snapshot.get("psu1_input_voltage_v"),
        "psu2_input_voltage_v": snapshot.get("psu2_input_voltage_v"),
        "average_input_voltage_v": snapshot.get("average_input_voltage_v"),
        "ipmi_metrics_error": snapshot.get("ipmi_metrics_error"),
    }
    queue_topic(
        messages,
        f"{base_topic}/electrical",
        json.dumps(electrical_payload, separators=(",", ":")),
    )
    electrical_scalar_keys = (
        "psu1_input_current_a",
        "psu2_input_current_a",
        "psu1_input_voltage_v",
        "psu2_input_voltage_v",
        "average_input_voltage_v",
    )
    for key in electrical_scalar_keys:
        if key in snapshot and snapshot[key] is not None:
            queue_topic(messages, f"{base_topic}/electrical/{key}", str(snapshot[key]))


def publish_host_error(messages: list[dict], host: str, error_message: str) -> None:
    base_topic = f"{TOPIC_PREFIX}/{host}"
    queue_topic(messages, f"{base_topic}/status", "error")
    queue_topic(
        messages,
        f"{base_topic}/power",
        json.dumps(
            {
                "host": host,
                "status": "error",
                "error": error_message,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        ),
    )


def summarize(results: list[dict], failures: list[dict]) -> dict:
    ambient_values = [item["ambient_temp_c"] for item in results if item.get("ambient_temp_c") is not None]
    average_input_voltage_values = [
        item["average_input_voltage_v"]
        for item in results
        if item.get("average_input_voltage_v") is not None
    ]
    total_actual = sum(item.get("actual_watts", 0) for item in results)
    total_last_min = sum(item.get("last_min_avg_watts", 0) for item in results)

    summary = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "hosts_ok": [item["host"] for item in results],
        "hosts_failed": failures,
        "actual_watts": total_actual,
        "last_min_avg_watts": total_last_min,
        "ambient_temps_c": {
            item["host"]: item["ambient_temp_c"]
            for item in results
            if item.get("ambient_temp_c") is not None
        },
        "average_input_voltages_v": {
            item["host"]: item["average_input_voltage_v"]
            for item in results
            if item.get("average_input_voltage_v") is not None
        },
        "average_ambient_temp_c": average(ambient_values),
        "average_input_voltage_v": average(average_input_voltage_values),
    }
    return summary


def publish_summary(messages: list[dict], summary: dict) -> None:
    queue_topic(messages, f"{TOPIC_PREFIX}/summary", json.dumps(summary, separators=(",", ":")))
    queue_topic(messages, f"{TOPIC_PREFIX}/total/actual_watts", str(summary["actual_watts"]))
    queue_topic(messages, f"{TOPIC_PREFIX}/total/last_min_avg_watts", str(summary["last_min_avg_watts"]))
    if summary.get("average_ambient_temp_c") is not None:
        queue_topic(
            messages,
            f"{TOPIC_PREFIX}/fleet/average_ambient_temp_c",
            str(summary["average_ambient_temp_c"]),
        )
    if summary.get("average_input_voltage_v") is not None:
        queue_topic(
            messages,
            f"{TOPIC_PREFIX}/fleet/average_input_voltage_v",
            str(summary["average_input_voltage_v"]),
        )


def collect_all(verbose: bool, config_path: str | None = None) -> tuple[list[dict], list[dict]]:
    results_by_host = {}
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SERVERS)) as executor:
        future_map = {
            executor.submit(collect_host, server["name"], config_path): server["name"]
            for server in SERVERS
        }
        for future in concurrent.futures.as_completed(future_map):
            host = future_map[future]
            try:
                snapshot = future.result()
                results_by_host[host] = snapshot
                if verbose:
                    temp = snapshot.get("ambient_temp_c")
                    avg_v = snapshot.get("average_input_voltage_v")
                    print(
                        f"{host}: ok via {snapshot['query_mode']} "
                        f"actual={snapshot['actual_watts']} temp={temp} avg_v={avg_v}"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append({"host": host, "error": str(exc)})
                if verbose:
                    print(f"{host}: error {exc}", file=sys.stderr)

    ordered_results = [
        results_by_host[server["name"]]
        for server in SERVERS
        if server["name"] in results_by_host
    ]
    return ordered_results, failures


def print_single_host(snapshot: dict, metric: str | None, plain: bool) -> int:
    if metric:
        value = snapshot.get(metric)
        if value is None:
            print(f"{metric} is unavailable for {snapshot['host']}", file=sys.stderr)
            return 1
        if plain:
            print(value)
        else:
            print(json.dumps({"host": snapshot["host"], metric: value}))
        return 0

    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


def main() -> int:
    global SERVERS
    args = parse_args()

    if not args.internal_publish_messages:
        SERVERS = load_servers(args.config)
        if args.host and args.host not in {s["name"] for s in SERVERS}:
            print(
                f"unknown host '{args.host}', available: {', '.join(s['name'] for s in SERVERS)}",
                file=sys.stderr,
            )
            return 1

    if args.internal_publish_messages:
        if not mqtt_publish_is_available():
            print("neither paho-mqtt nor mosquitto_pub is available", file=sys.stderr)
            return 1
        try:
            messages = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"invalid mqtt message payload: {exc}", file=sys.stderr)
            return 1
        publish_messages_direct(messages)
        return 0

    if args.internal_query_ipmi:
        if not args.host:
            print("--internal-query-ipmi requires --host", file=sys.stderr)
            return 1
        print(json.dumps(collect_ipmi_metrics(server_config(args.host)), separators=(",", ":")))
        return 0

    if args.host:
        try:
            snapshot = collect_host(args.host, config_path=args.config)
        except Exception as exc:  # noqa: BLE001
            print(f"{args.host}: {exc}", file=sys.stderr)
            return 1
        return print_single_host(snapshot, args.metric, args.plain)

    if not args.dry_run and not mqtt_publish_is_available():
        print("neither paho-mqtt nor mosquitto_pub is available", file=sys.stderr)
        return 1

    results, failures = collect_all(args.verbose, config_path=args.config)
    summary = summarize(results, failures)

    if args.dry_run:
        print(json.dumps({"hosts": results, "summary": summary}, indent=2, sort_keys=True))
        return 1 if failures else 0

    mqtt_messages = []
    if PUBLISH_HOME_ASSISTANT_DISCOVERY:
        publish_home_assistant_discovery(mqtt_messages)

    for snapshot in results:
        publish_host_metrics(mqtt_messages, snapshot)
    for failure in failures:
        publish_host_error(mqtt_messages, failure["host"], failure["error"])
    publish_summary(mqtt_messages, summary)
    publish_messages(mqtt_messages, config_path=args.config)

    print(
        json.dumps(
            {
                "published_hosts": [item["host"] for item in results],
                "failed_hosts": failures,
                "summary": summary,
            },
            sort_keys=True,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
