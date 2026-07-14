"""Tests for daemon mode and SIGHUP reload functionality."""

import threading
import time
from unittest.mock import MagicMock, patch

import powerMQTT


class TestDaemonMode:
    """Tests for the daemon loop (_run_daemon_loop) and related logic."""

    def setup_method(self):
        """Clear events before each test."""
        powerMQTT._shutdown_event.clear()
        powerMQTT._reload_config_event.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_args(self, **overrides):
        """Create a minimal argparse.Namespace for full collection (no --host)."""
        args = MagicMock()
        args.config = "/fake/config.json"
        args.dry_run = True
        args.quiet = True
        args.verbose = False
        args.host = None
        args.metric = None
        args.plain = False
        args.internal_publish_messages = False
        args.internal_query_ipmi = False
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def _servers(self):
        """Return a single-server config and name index."""
        servers = [{"name": "idrac1", "mode": "direct"}]
        by_name = {"idrac1": servers[0]}
        return servers, by_name

    # ------------------------------------------------------------------
    # Daemon mode — SIGHUP reload
    # ------------------------------------------------------------------

    def test_sighup_reloads_config(self):
        """Daemon mode: _reload_config_event triggers load_servers inside the loop."""
        args = self._make_args()
        servers, by_name = self._servers()

        call_count = [0]

        def collect_side(verbose, config_path=None, servers=None, servers_by_name=None):
            call_count[0] += 1
            if call_count[0] >= 2:
                powerMQTT._shutdown_event.set()
            return [], []

        powerMQTT._reload_config_event.set()

        with (
            patch("powerMQTT.load_servers", return_value=servers) as mock_load,
            patch("powerMQTT.collect_all", side_effect=collect_side),
            patch("powerMQTT.mqtt_publish_is_available", return_value=True),
        ):
            powerMQTT._run_daemon_loop(args, servers, by_name, interval=0.01)

        # load_servers called once for the reload at loop top
        assert mock_load.call_count == 1
        # Two full cycles completed before shutdown
        assert call_count[0] == 2

    def test_sighup_after_first_cycle_still_reloads(self):
        """Daemon mode: setting _reload_config_event after the first cycle triggers a reload."""
        args = self._make_args()
        servers, by_name = self._servers()

        call_count = [0]

        def collect_side(verbose, config_path=None, servers=None, servers_by_name=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # After the first cycle, set the reload event so the next
                # iteration picks it up.
                powerMQTT._reload_config_event.set()
            if call_count[0] >= 3:
                powerMQTT._shutdown_event.set()
            return [], []

        with (
            patch("powerMQTT.load_servers", return_value=servers) as mock_load,
            patch("powerMQTT.collect_all", side_effect=collect_side),
            patch("powerMQTT.mqtt_publish_is_available", return_value=True),
        ):
            powerMQTT._run_daemon_loop(args, servers, by_name, interval=0.01)

        # load_servers called once for the reload after first cycle
        assert mock_load.call_count == 1
        # Three cycles completed (1 → set reload, 2 → reload + collect, 3 → shutdown)
        assert call_count[0] == 3

    # ------------------------------------------------------------------
    # Daemon mode — SIGTERM clean exit
    # ------------------------------------------------------------------

    def test_sigterm_during_sleep_exits_immediately(self):
        """SIGTERM while the loop is sleeping causes immediate clean exit."""
        args = self._make_args()
        servers, by_name = self._servers()

        collect_count = [0]

        def collect_side(verbose, config_path=None, servers=None, servers_by_name=None):
            collect_count[0] += 1
            return [], []

        result = []

        with (
            patch("powerMQTT.collect_all", side_effect=collect_side),
            patch("powerMQTT.mqtt_publish_is_available", return_value=True),
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    powerMQTT._run_daemon_loop(args, servers, by_name, interval=60)
                )
            )
            thread.start()

            # Let one cycle complete, then simulate SIGTERM during sleep
            time.sleep(0.1)
            powerMQTT._shutdown_event.set()

            thread.join(timeout=5)
            assert not thread.is_alive(), "Daemon loop did not exit after SIGTERM"

        assert result == [0], f"Expected exit code 0, got {result}"
        # One cycle completed, then SIGTERM interrupted the sleep
        assert collect_count[0] == 1

    def test_sigterm_wakes_immediately_no_additional_cycles(self):
        """SIGTERM during sleep does not start an extra collection cycle."""
        args = self._make_args()
        servers, by_name = self._servers()

        collect_count = [0]

        def collect_side(verbose, config_path=None, servers=None, servers_by_name=None):
            collect_count[0] += 1
            return [], []

        result = []

        with (
            patch("powerMQTT.collect_all", side_effect=collect_side),
            patch("powerMQTT.mqtt_publish_is_available", return_value=True),
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    powerMQTT._run_daemon_loop(args, servers, by_name, interval=60)
                )
            )
            thread.start()
            time.sleep(0.1)
            powerMQTT._shutdown_event.set()
            time.sleep(0.2)

            thread.join(timeout=5)

        # Exactly one collection — the loop does NOT start a new one after wake
        assert collect_count[0] == 1

    # ------------------------------------------------------------------
    # --host forces single-shot
    # ------------------------------------------------------------------

    def test_host_forces_single_shot(self):
        """--host with interval > 0 runs once and prints a stderr note."""
        servers, _ = self._servers()

        with (
            patch("powerMQTT.COLLECTION_INTERVAL_SECONDS", 60),
            patch("powerMQTT.load_servers", return_value=servers),
            patch("powerMQTT.collect_host") as mock_collect_host,
        ):
            mock_collect_host.return_value = {
                "host": "idrac1",
                "actual_watts": 150,
                "query_mode": "direct",
                "collected_at": "now",
            }
            with patch("powerMQTT.parse_args") as mock_parse:
                mock_parse.return_value = self._make_args(host="idrac1")
                result = powerMQTT.main()

        assert result == 0
        assert mock_collect_host.call_count == 1

    # ------------------------------------------------------------------
    # Default single-shot unchanged
    # ------------------------------------------------------------------

    def test_default_single_shot(self):
        """Default (interval=0) runs once — load_servers and collect_all called once."""
        servers, _ = self._servers()

        with (
            patch("powerMQTT.COLLECTION_INTERVAL_SECONDS", 0),
            patch("powerMQTT.load_servers", return_value=servers) as mock_load,
            patch("powerMQTT.collect_all", return_value=([], [])) as mock_collect,
            patch("powerMQTT.mqtt_publish_is_available", return_value=True),
            patch("powerMQTT.parse_args") as mock_parse,
        ):
            mock_parse.return_value = self._make_args()
            result = powerMQTT.main()

        assert result == 0
        assert mock_load.call_count == 1
        assert mock_collect.call_count == 1

    def test_single_shot_internal_subprocess_passthrough(self):
        """Internal subprocess shortcuts (--internal-query-ipmi) still work in single-shot."""
        servers, _ = self._servers()

        # Test --internal-query-ipmi with --host
        with (
            patch("powerMQTT.COLLECTION_INTERVAL_SECONDS", 0),
            patch("powerMQTT.load_servers", return_value=servers),
            patch("powerMQTT.collect_ipmi_metrics") as mock_ipmi,
            patch("powerMQTT.parse_args") as mock_parse,
        ):
            mock_ipmi.return_value = {"ambient_temp_c": 25.0}
            mock_parse.return_value = self._make_args(
                internal_query_ipmi=True,
                host="idrac1",
            )
            result = powerMQTT.main()

        assert result == 0
        mock_ipmi.assert_called_once()
