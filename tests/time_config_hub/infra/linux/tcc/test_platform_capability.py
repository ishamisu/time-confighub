# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""Unit tests for :mod:`time_config_hub.infra.linux.tcc.platform_capability`."""

import subprocess
from unittest.mock import MagicMock, mock_open, patch

import pytest

from time_config_hub.infra.linux.tcc import platform_capability as pc
from time_config_hub.infra.linux.tcc.capability_types import (
    CStateInfo,
    MsrAccess,
    PlatformCapability,
)

TccPlatformCapability = pc.TccPlatformCapability

MODULE = "time_config_hub.infra.linux.tcc.platform_capability"


def _completed(stdout="", returncode=0):
    """Build a fake :class:`subprocess.CompletedProcess`-like object."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


class TestToolAvailable:
    def test_tool_present(self):
        with patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cpupower"):
            assert TccPlatformCapability._tool_available("cpupower") is True

    def test_tool_absent(self):
        with patch(f"{MODULE}.shutil.which", return_value=None):
            assert TccPlatformCapability._tool_available("cpupower") is False


class TestProbeGovernors:
    def test_reads_governors(self):
        m = mock_open(read_data="performance powersave  ondemand\n")
        with patch("builtins.open", m):
            assert TccPlatformCapability._probe_governors() == (
                "performance",
                "powersave",
                "ondemand",
            )

    def test_returns_empty_on_oserror(self):
        with patch("builtins.open", side_effect=OSError("no file")):
            assert TccPlatformCapability._probe_governors() == ()


class TestProbeNumCores:
    def test_nproc_success(self):
        with patch(f"{MODULE}.subprocess.run", return_value=_completed("16\n")):
            assert TccPlatformCapability._probe_num_cores() == 16

    def test_falls_back_to_os_cpu_count(self):
        with patch(
            f"{MODULE}.subprocess.run", side_effect=OSError("nproc missing")
        ), patch(f"{MODULE}.os.cpu_count", return_value=4):
            assert TccPlatformCapability._probe_num_cores() == 4

    def test_falls_back_on_bad_value(self):
        with patch(
            f"{MODULE}.subprocess.run", return_value=_completed("not-a-number")
        ), patch(f"{MODULE}.os.cpu_count", return_value=8):
            assert TccPlatformCapability._probe_num_cores() == 8

    def test_fallback_zero_when_cpu_count_none(self):
        with patch(
            f"{MODULE}.subprocess.run", side_effect=subprocess.SubprocessError()
        ), patch(f"{MODULE}.os.cpu_count", return_value=None):
            assert TccPlatformCapability._probe_num_cores() == 0


class TestParseIdleInfo:
    def test_parses_states_and_latencies(self):
        output = (
            "Some analyzing CPU 0:\n"
            "POLL:\n"
            "Flags/Description: CPUIDLE CORE POLL IDLE\n"
            "Latency: 0\n"
            "C1_ACPI:\n"
            "Latency: 1\n"
            "C2_ACPI:\n"
            "Latency: 253\n"
        )
        states = TccPlatformCapability._parse_idle_info(output)
        assert states == (
            CStateInfo(index=0, name="POLL", latency_us=0),
            CStateInfo(index=1, name="C1_ACPI", latency_us=1),
            CStateInfo(index=2, name="C2_ACPI", latency_us=253),
        )

    def test_state_without_latency_keeps_none(self):
        output = "POLL:\nC1_ACPI:\nLatency: 5\n"
        states = TccPlatformCapability._parse_idle_info(output)
        assert states[0] == CStateInfo(index=0, name="POLL", latency_us=None)
        assert states[1] == CStateInfo(index=1, name="C1_ACPI", latency_us=5)

    def test_non_integer_latency_ignored(self):
        output = "POLL:\nLatency: abc\n"
        states = TccPlatformCapability._parse_idle_info(output)
        assert states == (CStateInfo(index=0, name="POLL", latency_us=None),)

    def test_empty_output_returns_empty(self):
        assert TccPlatformCapability._parse_idle_info("") == ()

    def test_latency_before_any_state_is_ignored(self):
        output = "Latency: 99\nPOLL:\n"
        states = TccPlatformCapability._parse_idle_info(output)
        assert states == (CStateInfo(index=0, name="POLL", latency_us=None),)


class TestProbeCStatesForCore:
    def test_success(self):
        with patch(
            f"{MODULE}.subprocess.run",
            return_value=_completed("POLL:\nLatency: 0\n"),
        ):
            states = TccPlatformCapability._probe_c_states_for_core(0)
        assert states == (CStateInfo(index=0, name="POLL", latency_us=0),)

    def test_returns_empty_on_error(self):
        with patch(f"{MODULE}.subprocess.run", side_effect=OSError("boom")):
            assert TccPlatformCapability._probe_c_states_for_core(0) == ()


class TestProbeCStates:
    def test_probes_each_core(self):
        with patch.object(
            TccPlatformCapability,
            "_probe_c_states_for_core",
            side_effect=lambda cid: (CStateInfo(index=0, name="POLL"),),
        ) as mock_probe:
            result = TccPlatformCapability._probe_c_states(3)
        assert set(result.keys()) == {0, 1, 2}
        assert mock_probe.call_count == 3

    def test_omits_cores_with_no_states(self):
        with patch.object(
            TccPlatformCapability,
            "_probe_c_states_for_core",
            side_effect=lambda cid: (
                (CStateInfo(index=0, name="POLL"),) if cid == 0 else ()
            ),
        ):
            result = TccPlatformCapability._probe_c_states(2)
        assert list(result.keys()) == [0]

    def test_zero_cores_probes_single_core(self):
        with patch.object(
            TccPlatformCapability,
            "_probe_c_states_for_core",
            return_value=(CStateInfo(index=0, name="POLL"),),
        ) as mock_probe:
            result = TccPlatformCapability._probe_c_states(0)
        mock_probe.assert_called_once_with(0)
        assert list(result.keys()) == [0]

    def test_all_failed_returns_empty(self):
        with patch.object(
            TccPlatformCapability, "_probe_c_states_for_core", return_value=()
        ):
            assert TccPlatformCapability._probe_c_states(4) == {}


class TestSummarizeCStates:
    def test_empty_returns_none_string(self):
        assert TccPlatformCapability._summarize_c_states({}) == "none"

    def test_groups_cores_with_identical_signatures(self):
        p_states = (
            CStateInfo(index=0, name="POLL", latency_us=None),
            CStateInfo(index=1, name="C1", latency_us=1),
        )
        e_states = (
            CStateInfo(index=0, name="POLL", latency_us=None),
            CStateInfo(index=1, name="C1", latency_us=2),
        )
        c_states = {0: p_states, 1: p_states, 2: e_states}
        summary = TccPlatformCapability._summarize_c_states(c_states)
        # Two distinct signatures -> two groups separated by " | ".
        assert summary.count("|") == 1
        assert "cores [0, 1]" in summary
        assert "cores [2]" in summary
        assert "0:POLL(Noneus)" in summary
        assert "1:C1(1us)" in summary


class TestProbeFreqRange:
    def test_reads_min_max(self):
        # open() is called twice; return min then max (kHz).
        m = mock_open()
        m.side_effect = [
            mock_open(read_data="800000\n").return_value,
            mock_open(read_data="3200000\n").return_value,
        ]
        with patch("builtins.open", m):
            assert TccPlatformCapability._probe_freq_range() == (800, 3200)

    def test_returns_zero_on_error(self):
        with patch("builtins.open", side_effect=OSError("no sysfs")):
            assert TccPlatformCapability._probe_freq_range() == (0, 0)


class TestProbeUncoreRatioRange:
    def test_no_sysfs_dir(self):
        with patch(f"{MODULE}.os.path.isdir", return_value=False):
            assert TccPlatformCapability._probe_uncore_ratio_range() == (0, 0)

    def test_listdir_error(self):
        with patch(f"{MODULE}.os.path.isdir", return_value=True), patch(
            f"{MODULE}.os.listdir", side_effect=OSError("denied")
        ):
            assert TccPlatformCapability._probe_uncore_ratio_range() == (0, 0)

    def test_reads_widest_range_across_domains(self):
        # Two domains: (8..24) and (6..30) -> widest is (6, 30).
        reads = iter(
            ["800000", "2400000", "600000", "3000000"]  # d0 min,max ; d1 min,max
        )

        def fake_open(path, *args, **kwargs):
            return mock_open(read_data=next(reads)).return_value

        with patch(f"{MODULE}.os.path.isdir", return_value=True), patch(
            f"{MODULE}.os.listdir", return_value=["domain0", "domain1"]
        ), patch("builtins.open", side_effect=fake_open):
            assert TccPlatformCapability._probe_uncore_ratio_range() == (6, 30)

    def test_skips_unreadable_domain(self):
        def fake_open(path, *args, **kwargs):
            if "domain_bad" in path:
                raise OSError("unreadable")
            reads = {
                "initial_min_freq_khz": "800000",
                "initial_max_freq_khz": "2400000",
            }
            for key, val in reads.items():
                if key in path:
                    return mock_open(read_data=val).return_value
            raise OSError("unexpected")

        with patch(f"{MODULE}.os.path.isdir", return_value=True), patch(
            f"{MODULE}.os.listdir", return_value=["domain_bad", "domain_good"]
        ), patch("builtins.open", side_effect=fake_open):
            assert TccPlatformCapability._probe_uncore_ratio_range() == (8, 24)

    def test_no_readable_domains_returns_zero(self):
        with patch(f"{MODULE}.os.path.isdir", return_value=True), patch(
            f"{MODULE}.os.listdir", return_value=["domain0"]
        ), patch("builtins.open", side_effect=OSError("denied")):
            assert TccPlatformCapability._probe_uncore_ratio_range() == (0, 0)


class TestRdmsr:
    def test_success_returns_raw_value(self):
        with patch(f"{MODULE}.subprocess.run", return_value=_completed("0000000f\n")):
            assert TccPlatformCapability._rdmsr(0x620) == "0000000f"

    def test_failure_returns_none(self):
        with patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "rdmsr"),
        ):
            assert TccPlatformCapability._rdmsr(0x620) is None

    def test_passes_correct_arguments(self):
        with patch(
            f"{MODULE}.subprocess.run", return_value=_completed("0")
        ) as mock_run:
            TccPlatformCapability._rdmsr(0xC8F)
        args, kwargs = mock_run.call_args
        assert args[0] == ["rdmsr", "-p", "0", "0xc8f"]
        assert kwargs["check"] is True


class TestWriteBack:
    def test_success(self):
        with patch(f"{MODULE}.subprocess.run", return_value=_completed()) as mock_run:
            assert TccPlatformCapability._write_back(0x620, "0f") is True
        args, _ = mock_run.call_args
        # Value must be forced to hex with a 0x prefix.
        assert args[0] == ["wrmsr", "-p", "0", "0x620", "0x0f"]

    def test_failure_returns_false(self):
        with patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "wrmsr"),
        ):
            assert TccPlatformCapability._write_back(0x620, "0f") is False


class TestMsrAccessProbe:
    def test_not_accessible_when_read_fails(self):
        with patch.object(TccPlatformCapability, "_rdmsr", return_value=None):
            assert TccPlatformCapability._msr_access(0x620) is MsrAccess.NOT_ACCESSIBLE

    def test_rw_when_read_and_write_succeed(self):
        with patch.object(
            TccPlatformCapability, "_rdmsr", return_value="0f"
        ), patch.object(TccPlatformCapability, "_write_back", return_value=True):
            assert TccPlatformCapability._msr_access(0x620) is MsrAccess.RW

    def test_ro_when_write_fails(self):
        with patch.object(
            TccPlatformCapability, "_rdmsr", return_value="0f"
        ), patch.object(TccPlatformCapability, "_write_back", return_value=False):
            assert TccPlatformCapability._msr_access(0x620) is MsrAccess.RO

    def test_ro_when_probe_write_disabled(self):
        with patch.object(
            TccPlatformCapability, "_rdmsr", return_value="0f"
        ), patch.object(TccPlatformCapability, "_write_back") as mock_wb:
            result = TccPlatformCapability._msr_access(0xC8E, probe_write=False)
        assert result is MsrAccess.RO
        mock_wb.assert_not_called()


class TestFormatProbeSummary:
    def test_renders_all_sections(self):
        cap = PlatformCapability(
            num_cores=4,
            available_governors=("performance",),
            c_states={0: (CStateInfo(index=0, name="POLL"),)},
            supported_freq_range=(800, 3200),
            supported_uncore_ratio_range=(8, 24),
        )
        text = TccPlatformCapability._format_probe_summary(cap)
        assert "Probed TCC capability" in text
        assert "total cores:         4" in text
        assert "0x620 UNCORE_RATIO" in text
        assert "CLOS count" in text


class TestProbe:
    """End-to-end test of :meth:`probe` with all platform I/O mocked out."""

    def test_probe_assembles_capability(self):
        access_map = {
            pc._MSR_UNCORE_RATIO: MsrAccess.RW,
            pc._MSR_PQR_ASSOC: MsrAccess.RO,
            pc._MSR_QM_EVTSEL: MsrAccess.RO,
            pc._MSR_QM_CTR: MsrAccess.RO,
            pc._MSR_L3_COS0: MsrAccess.RW,
            pc._MSR_L3_COS1: MsrAccess.RW,
        }

        with patch.object(
            TccPlatformCapability, "_probe_num_cores", return_value=8
        ), patch.object(
            TccPlatformCapability,
            "_msr_access",
            side_effect=lambda msr, **kw: access_map[msr],
        ), patch.object(
            TccPlatformCapability, "_tool_available", return_value=True
        ), patch.object(
            TccPlatformCapability,
            "_probe_governors",
            return_value=("performance", "powersave"),
        ), patch.object(
            TccPlatformCapability, "_probe_c_states", return_value={}
        ), patch.object(
            TccPlatformCapability, "_probe_freq_range", return_value=(800, 3200)
        ), patch.object(
            TccPlatformCapability,
            "_probe_uncore_ratio_range",
            return_value=(8, 24),
        ), patch(
            f"{MODULE}.os.geteuid", return_value=0
        ), patch(
            f"{MODULE}.os.path.exists", return_value=True
        ):
            cap = TccPlatformCapability.probe()

        assert isinstance(cap, PlatformCapability)
        assert cap.is_root is True
        assert cap.msr_module_loaded is True
        assert cap.cpupower_available is True
        assert cap.tuna_available is True
        assert cap.num_cores == 8
        assert cap.available_governors == ("performance", "powersave")
        assert cap.supported_freq_range == (800, 3200)
        assert cap.uncore_driver_present is True
        assert cap.supported_uncore_ratio_range == (8, 24)
        assert cap.msr_uncore_ratio_limit is MsrAccess.RW
        # Both COS registers readable -> 2 usable CLOS.
        assert cap.supported_clos_count == 2
        # QM_EVTSEL + QM_CTR both readable -> monitoring available.
        assert cap.resource_monitoring_available is True

    def test_probe_non_root_no_monitoring(self):
        with patch.object(
            TccPlatformCapability, "_probe_num_cores", return_value=1
        ), patch.object(
            TccPlatformCapability,
            "_msr_access",
            return_value=MsrAccess.NOT_ACCESSIBLE,
        ), patch.object(
            TccPlatformCapability, "_tool_available", return_value=False
        ), patch.object(
            TccPlatformCapability, "_probe_governors", return_value=()
        ), patch.object(
            TccPlatformCapability, "_probe_c_states", return_value={}
        ), patch.object(
            TccPlatformCapability, "_probe_freq_range", return_value=(0, 0)
        ), patch.object(
            TccPlatformCapability,
            "_probe_uncore_ratio_range",
            return_value=(0, 0),
        ), patch(
            f"{MODULE}.os.geteuid", return_value=1000
        ), patch(
            f"{MODULE}.os.path.exists", return_value=False
        ):
            cap = TccPlatformCapability.probe()

        assert cap.is_root is False
        assert cap.msr_module_loaded is False
        assert cap.supported_clos_count == 0
        assert cap.resource_monitoring_available is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
