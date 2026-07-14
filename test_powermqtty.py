import json
import tempfile

import pytest

import powerMQTT


class TestFirstNumber:
    def test_integer(self):
        assert powerMQTT.first_number("150 W") == 150

    def test_float(self):
        assert powerMQTT.first_number("3.14 V") == pytest.approx(3.14)

    def test_negative(self):
        assert powerMQTT.first_number("-5 degrees") == -5

    def test_no_number(self):
        assert powerMQTT.first_number("N/A") is None

    def test_empty_string(self):
        assert powerMQTT.first_number("") is None

    def test_whole_float_returns_int(self):
        result = powerMQTT.first_number("100.0 W")
        assert result == 100
        assert isinstance(result, int)

    def test_decimal_returns_float(self):
        result = powerMQTT.first_number("3.5 A")
        assert result == pytest.approx(3.5)
        assert isinstance(result, float)


class TestAverage:
    def test_basic(self):
        assert powerMQTT.average([10, 20, 30]) == pytest.approx(20.0)

    def test_empty(self):
        assert powerMQTT.average([]) is None

    def test_single_value(self):
        assert powerMQTT.average([42.0]) == pytest.approx(42.0)


class TestParseJsonOutput:
    def test_valid_json(self):
        result = powerMQTT.parse_json_output('{"a": 1}', "test")
        assert result == {"a": 1}

    def test_invalid_json(self):
        with pytest.raises(powerMQTT.QueryError, match="invalid json"):
            powerMQTT.parse_json_output("not json", "test")

    def test_invalid_json_with_host(self):
        with pytest.raises(powerMQTT.QueryError, match="for idrac1"):
            powerMQTT.parse_json_output("bad", "test", host="idrac1")


class TestParseCfgOutput:
    SAMPLE_OUTPUT = (
        "cfgServerActualPowerConsumption=150 W\n"
        "cfgServerPowerLastMinAvg=148 W\n"
        "cfgServerPowerStatus=1\n"
        "cfgServerPeakPowerConsumption=200 W\n"
        "cfgServerPeakPowerConsumptionTimestamp=2024-01-01 00:00:00\n"
    )

    def test_basic_parse(self):
        result = powerMQTT.parse_cfg_output("idrac1", "direct", self.SAMPLE_OUTPUT)
        assert result["host"] == "idrac1"
        assert result["query_mode"] == "direct"
        assert result["actual_watts"] == 150
        assert result["last_min_avg_watts"] == 148
        assert result["power_status"] == 1
        assert result["peak_watts"] == 200
        assert "collected_at" in result

    def test_raw_values_preserved(self):
        result = powerMQTT.parse_cfg_output("idrac1", "direct", self.SAMPLE_OUTPUT)
        assert result["actual_watts_raw"] == "150 W"

    def test_comment_stripping(self):
        output = "#cfgServerActualPowerConsumption=100 W\n"
        result = powerMQTT.parse_cfg_output("idrac1", "direct", output)
        assert result["actual_watts"] == 100

    def test_missing_actual_watts_raises(self):
        with pytest.raises(powerMQTT.QueryError, match="unable to parse actual_watts"):
            powerMQTT.parse_cfg_output("idrac1", "direct", "cfgServerPowerStatus=1\n")

    def test_none_actual_watts_raises(self):
        output = "cfgServerActualPowerConsumption=N/A\n"
        with pytest.raises(powerMQTT.QueryError, match="unable to parse actual_watts"):
            powerMQTT.parse_cfg_output("idrac1", "direct", output)

    def test_empty_output_raises(self):
        with pytest.raises(powerMQTT.QueryError, match="unable to parse actual_watts"):
            powerMQTT.parse_cfg_output("idrac1", "direct", "")


class TestLoadServers:
    def _write_config(self, data):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            return f.name

    def test_valid_config(self):
        path = self._write_config([{"name": "idrac1", "mode": "direct", "ipmi_host": "10.0.0.1"}])
        result = powerMQTT.load_servers(path)
        assert len(result) == 1
        assert result[0]["name"] == "idrac1"
        assert result[0]["mode"] == "direct"
        assert result[0]["ipmi_host"] == "10.0.0.1"

    def test_defaults_applied(self):
        path = self._write_config([{"name": "idrac1"}])
        result = powerMQTT.load_servers(path)
        assert result[0]["mode"] == "direct"
        assert result[0]["ipmi_host"] == "idrac1"

    def test_duplicate_names_rejected(self):
        path = self._write_config([{"name": "idrac1"}, {"name": "idrac1"}])
        with pytest.raises(SystemExit, match="Duplicate server name"):
            powerMQTT.load_servers(path)

    def test_invalid_mode_rejected(self):
        path = self._write_config([{"name": "idrac1", "mode": "driect"}])
        with pytest.raises(SystemExit, match="Invalid mode"):
            powerMQTT.load_servers(path)

    def test_missing_file(self):
        with pytest.raises(SystemExit, match="Config file not found"):
            powerMQTT.load_servers("/nonexistent/servers.json")

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            name = f.name
        with pytest.raises(SystemExit, match="Invalid JSON"):
            powerMQTT.load_servers(name)

    def test_empty_array_rejected(self):
        path = self._write_config([])
        with pytest.raises(SystemExit, match="non-empty JSON array"):
            powerMQTT.load_servers(path)

    def test_missing_name_rejected(self):
        path = self._write_config([{"mode": "direct"}])
        with pytest.raises(SystemExit, match="must be an object with 'name'"):
            powerMQTT.load_servers(path)
