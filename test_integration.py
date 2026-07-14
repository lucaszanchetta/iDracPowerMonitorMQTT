import json
import tempfile
from unittest import mock

import pytest

import powerMQTT

SAMPLE_RACADM_OUTPUT = (
    "cfgServerActualPowerConsumption=150 W\n"
    "cfgServerPowerLastMinAvg=148 W\n"
    "cfgServerPowerLastHourAvg=145 W\n"
    "cfgServerPowerLastDayAvg=140 W\n"
    "cfgServerPowerLastWeekAvg=135 W\n"
    "cfgServerPowerStatus=1\n"
    "cfgServerPeakPowerConsumption=200 W\n"
    "cfgServerPeakPowerConsumptionTimestamp=2024-01-01 00:00:00\n"
    "cfgServerPowerCapWatts=300 W\n"
    "cfgServerActualAmperageConsumption=0.8 A\n"
    "cfgServerPeakAmperage=1.2 A\n"
    "cfgServerPeakAmperageTimeStamp=2024-01-01 00:00:00\n"
    "cfgServerCumulativePowerConsumption=100 kWh\n"
    "cfgServerCumulativePowerConsumptionTimeStamp=2024-01-01 00:00:00\n"
)

SAMPLE_IPMI_METRICS = {
    "ambient_temp_c": 25.0,
    "ambient_temp_sensor": "Ambient Temp",
    "ambient_temp_units": "\u00b0C",
    "psu_input_currents_a": [0.8, 1.2],
    "psu1_input_current_a": 0.8,
    "psu2_input_current_a": 1.2,
    "psu_input_voltages_v": [230.0, 229.0],
    "average_input_voltage_v": 229.5,
    "psu1_input_voltage_v": 230.0,
    "psu2_input_voltage_v": 229.0,
}


def _write_servers_config(servers):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(servers, f)
        return f.name


class TestCollectHostEndToEnd:
    """Test collect_host with mocked subprocess queries."""

    def test_collect_host_success(self):
        servers = [{"name": "idrac1", "mode": "direct", "ipmi_host": "idrac1"}]
        servers_by_name = {s["name"]: s for s in servers}

        with (
            mock.patch(
                "powerMQTT.query_power_output", return_value=("direct", SAMPLE_RACADM_OUTPUT)
            ),
            mock.patch("powerMQTT.run_ipmi_subprocess", return_value=SAMPLE_IPMI_METRICS),
        ):
            snapshot = powerMQTT.collect_host("idrac1", servers_by_name=servers_by_name)

        assert snapshot["host"] == "idrac1"
        assert snapshot["query_mode"] == "direct"
        assert snapshot["actual_watts"] == 150
        assert snapshot["last_min_avg_watts"] == 148
        assert snapshot["power_status"] == 1
        assert snapshot["ambient_temp_c"] == 25.0
        assert snapshot["psu1_input_current_a"] == 0.8
        assert snapshot["psu2_input_current_a"] == 1.2
        assert snapshot["average_input_voltage_v"] == pytest.approx(229.5)
        assert "collected_at" in snapshot

    def test_collect_host_ipmi_failure_graceful(self):
        servers = [{"name": "idrac1", "mode": "direct", "ipmi_host": "idrac1"}]
        servers_by_name = {s["name"]: s for s in servers}

        with (
            mock.patch(
                "powerMQTT.query_power_output", return_value=("direct", SAMPLE_RACADM_OUTPUT)
            ),
            mock.patch("powerMQTT.run_ipmi_subprocess", side_effect=RuntimeError("IPMI down")),
        ):
            snapshot = powerMQTT.collect_host("idrac1", servers_by_name=servers_by_name)

        assert snapshot["actual_watts"] == 150
        assert snapshot.get("ipmi_metrics_error") == "IPMI down"
        assert snapshot.get("ambient_temp_c") is None

    def test_collect_host_unknown_raises(self):
        servers_by_name = {}
        with pytest.raises(powerMQTT.QueryError, match="unknown host"):
            powerMQTT.collect_host("nonexistent", servers_by_name=servers_by_name)


class TestCollectAllEndToEnd:
    """Test collect_all with mocked collect_host."""

    def test_collect_all_success(self):
        servers = [{"name": "idrac1"}, {"name": "idrac2"}, {"name": "idrac3"}]
        servers_by_name = {s["name"]: s for s in servers}

        def mock_collect_host(host, config_path=None, servers_by_name=None):
            return {
                "host": host,
                "query_mode": "direct",
                "actual_watts": 150,
                "ambient_temp_c": 25.0,
                "average_input_voltage_v": 230.0,
                "collected_at": "2024-01-01T00:00:00+00:00",
            }

        with mock.patch("powerMQTT.collect_host", side_effect=mock_collect_host):
            results, failures = powerMQTT.collect_all(
                False, servers=servers, servers_by_name=servers_by_name
            )

        assert len(results) == 3
        assert len(failures) == 0
        assert results[0]["host"] == "idrac1"
        assert results[1]["host"] == "idrac2"
        assert results[2]["host"] == "idrac3"

    def test_collect_all_mixed_results(self):
        servers = [{"name": "idrac1"}, {"name": "idrac2"}]
        servers_by_name = {s["name"]: s for s in servers}

        calls = []

        def mock_collect_host(host, config_path=None, servers_by_name=None):
            calls.append(host)
            if host == "idrac1":
                raise RuntimeError("timeout")
            return {
                "host": host,
                "query_mode": "direct",
                "actual_watts": 150,
                "ambient_temp_c": 25.0,
                "average_input_voltage_v": None,
                "collected_at": "2024-01-01T00:00:00+00:00",
            }

        with mock.patch("powerMQTT.collect_host", side_effect=mock_collect_host):
            results, failures = powerMQTT.collect_all(
                False, servers=servers, servers_by_name=servers_by_name
            )

        assert len(results) == 1
        assert len(failures) == 1
        assert results[0]["host"] == "idrac2"
        assert failures[0]["host"] == "idrac1"
        assert "timeout" in failures[0]["error"]

    def test_collect_all_shutdown_early(self):
        servers = [{"name": f"idrac{i}"} for i in range(1, 6)]
        servers_by_name = {s["name"]: s for s in servers}

        powerMQTT._shutdown_event.set()
        try:
            results, failures = powerMQTT.collect_all(
                False, servers=servers, servers_by_name=servers_by_name
            )

            assert len(results) == 0
            assert len(failures) == 0
        finally:
            powerMQTT._shutdown_event.clear()


class TestSummarize:
    """Test fleet summary aggregation."""

    def test_summarize_basic(self):
        results = [
            {
                "host": "idrac1",
                "actual_watts": 100,
                "last_min_avg_watts": 95,
                "ambient_temp_c": 25.0,
                "average_input_voltage_v": 230.0,
            },
            {
                "host": "idrac2",
                "actual_watts": 200,
                "last_min_avg_watts": 195,
                "ambient_temp_c": 28.0,
                "average_input_voltage_v": 229.0,
            },
        ]
        failures = []

        summary = powerMQTT.summarize(results, failures)

        assert summary["actual_watts"] == 300
        assert summary["last_min_avg_watts"] == 290
        assert summary["hosts_ok"] == ["idrac1", "idrac2"]
        assert summary["hosts_failed"] == []
        assert summary["average_ambient_temp_c"] == pytest.approx(26.5)
        assert summary["average_input_voltage_v"] == pytest.approx(229.5)

    def test_summarize_with_failures(self):
        results = [
            {
                "host": "idrac1",
                "actual_watts": 100,
                "last_min_avg_watts": 95,
                "ambient_temp_c": 25.0,
                "average_input_voltage_v": None,
            },
        ]
        failures = [{"host": "idrac2", "error": "timeout"}]

        summary = powerMQTT.summarize(results, failures)

        assert summary["hosts_ok"] == ["idrac1"]
        assert summary["hosts_failed"] == failures
        assert summary["average_input_voltage_v"] is None


class TestPublishHostMetrics:
    """Test MQTT message generation for host metrics."""

    def test_publishes_all_scalar_keys(self):
        snapshot = {
            "host": "idrac1",
            "collected_at": "2024-01-01T00:00:00+00:00",
            "actual_watts": 150,
            "last_min_avg_watts": 148,
            "last_hour_avg_watts": 145,
            "last_day_avg_watts": 140,
            "last_week_avg_watts": 135,
            "peak_watts": 200,
            "power_cap_watts": 300,
            "actual_amps": 0.8,
            "peak_amps": 1.2,
            "power_status": 1,
            "ambient_temp_c": 25.0,
            "psu1_input_current_a": 0.8,
            "psu2_input_current_a": 1.2,
            "psu1_input_voltage_v": 230.0,
            "psu2_input_voltage_v": 229.0,
            "average_input_voltage_v": 229.5,
        }

        messages = []
        powerMQTT.publish_host_metrics(messages, snapshot)

        topics = {m["topic"] for m in messages}
        assert "homelab/idrac/idrac1/status" in topics
        assert "homelab/idrac/idrac1/power" in topics
        assert "homelab/idrac/idrac1/power/actual_watts" in topics
        assert "homelab/idrac/idrac1/temperature/ambient_c" in topics
        assert "homelab/idrac/idrac1/electrical/psu1_input_current_a" in topics
        assert "homelab/idrac/idrac1/electrical/psu2_input_current_a" in topics
        assert "homelab/idrac/idrac1/electrical/average_input_voltage_v" in topics

    def test_skips_none_values(self):
        snapshot = {
            "host": "idrac1",
            "collected_at": "2024-01-01T00:00:00+00:00",
            "actual_watts": 150,
            "ambient_temp_c": None,
            "psu1_input_current_a": None,
            "psu2_input_current_a": None,
        }

        messages = []
        powerMQTT.publish_host_metrics(messages, snapshot)

        scalar_topics = {m["topic"] for m in messages if m["topic"].endswith("/actual_watts")}
        assert "homelab/idrac/idrac1/power/actual_watts" in scalar_topics

        temp_scalar = any(
            m["topic"] == "homelab/idrac/idrac1/temperature/ambient_c" for m in messages
        )
        assert not temp_scalar


class TestPublishHostError:
    """Test MQTT message generation for host errors."""

    def test_error_payload(self):
        messages = []
        powerMQTT.publish_host_error(messages, "idrac1", "connection refused")

        status_msg = next(m for m in messages if m["topic"] == "homelab/idrac/idrac1/status")
        assert status_msg["payload"] == "error"

        power_msg = next(m for m in messages if m["topic"] == "homelab/idrac/idrac1/power")
        payload = json.loads(power_msg["payload"])
        assert payload["host"] == "idrac1"
        assert payload["status"] == "error"
        assert payload["error"] == "connection refused"
        assert "expire_after_seconds" in payload
        assert "collected_at" in payload


class TestPublishSummary:
    """Test fleet summary MQTT message generation."""

    def test_summary_payload(self):
        summary = {
            "collected_at": "2024-01-01T00:00:00+00:00",
            "hosts_ok": ["idrac1", "idrac2"],
            "hosts_failed": [],
            "actual_watts": 300,
            "last_min_avg_watts": 290,
            "average_ambient_temp_c": 26.5,
            "average_input_voltage_v": 229.5,
        }

        messages = []
        powerMQTT.publish_summary(messages, summary)

        topics = {m["topic"] for m in messages}
        assert "homelab/idrac/fleet/status" in topics
        assert "homelab/idrac/total/actual_watts" in topics
        assert "homelab/idrac/fleet/average_ambient_temp_c" in topics
        assert "homelab/idrac/fleet/average_input_voltage_v" in topics

    def test_summary_empty_hosts_sets_error(self):
        summary = {
            "collected_at": "2024-01-01T00:00:00+00:00",
            "hosts_ok": [],
            "hosts_failed": [{"host": "idrac1"}],
            "actual_watts": 0,
            "last_min_avg_watts": 0,
            "average_ambient_temp_c": None,
            "average_input_voltage_v": None,
        }

        messages = []
        powerMQTT.publish_summary(messages, summary)

        status_msg = next(m for m in messages if m["topic"] == "homelab/idrac/fleet/status")
        assert status_msg["payload"] == "error"


class TestHomeAssistantDiscovery:
    """Test HA discovery config generation."""

    def test_discovery_configs_generated(self):
        servers = [{"name": "idrac1"}, {"name": "idrac2"}]

        messages = []
        powerMQTT.publish_home_assistant_discovery(messages, servers)

        assert len(messages) > 0
        config_topics = {m["topic"] for m in messages}
        assert any("idrac1_actual_watts" in t for t in config_topics)
        assert any("idrac2_actual_watts" in t for t in config_topics)
        assert any("idrac_fleet_average_ambient_temp_c" in t for t in config_topics)

    def test_discovery_each_config_has_expire_after(self):
        servers = [{"name": "idrac1"}]

        messages = []
        powerMQTT.publish_home_assistant_discovery(messages, servers)

        for msg in messages:
            if "/config" in msg["topic"]:
                payload = json.loads(msg["payload"])
                assert "expire_after" in payload


class TestVersion:
    """Test version flag."""

    def test_version_string_exists(self):
        assert isinstance(powerMQTT.__version__, str)
        assert "." in powerMQTT.__version__


class TestMqttAuth:
    """Test MQTT auth params are passed through."""

    def test_mosquitto_pub_auth_args(self, monkeypatch):
        monkeypatch.setenv("IDRAC_MQTT_USERNAME", "testuser")
        monkeypatch.setenv("IDRAC_MQTT_PASSWORD", "testpass")

        import importlib

        importlib.reload(powerMQTT)

        assert powerMQTT.MQTT_USERNAME == "testuser"
        assert powerMQTT.MQTT_PASSWORD == "testpass"
