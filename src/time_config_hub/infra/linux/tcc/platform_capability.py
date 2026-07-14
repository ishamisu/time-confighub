# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
TCC Platform Capability Discovery

Probes the platform to determine what capabilities are available for
TCC configuration.

The capability probing process detects the platform features, supported
configuration ranges, and access permissions required for TCC tuning and
validation. The collected information is used to validate user-requested
settings before applying configuration changes.

Capability discovery is performed using the following mechanisms:
- ``nproc --all``
    to determine the total number of logical CPU cores
- ``cpupower idle-info``
    to determine the available C-states for each core
- sysfs files under ``/sys/devices/system/cpu/cpu<index>/cpufreq/``
    to determine the available CPU frequency governors and min/max frequencies
- sysfs files under ``/sys/devices/system/cpu/intel_uncore_frequency/``
    to determine the supported uncore/ring ratio range
- ``rdmsr`` and ``wrmsr``
    to determine the readability and writability of specific MSRs
    related to uncore frequency and RDT/CAT

References:
https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html
"""

import logging
import os
import re
import shutil
import subprocess  # nosec B404 (no shell=True, args are controlled constants)
from typing import Optional

from .capability_types import CStateInfo, MsrAccess, PlatformCapability

# Constants for MSR addresses used in capability probing
_MSR_UNCORE_RATIO   = 0x620  # Uncore Ratio Limit MSR
_MSR_QM_EVTSEL      = 0xC8D  # Monitoring Event Select Register
_MSR_QM_CTR         = 0xC8E  # Monitoring Counter Register
_MSR_PQR_ASSOC      = 0xC8F  # RDT/CAT Resource Association Register
_MSR_L3_COS0        = 0xC90  # L3 CAT COS0 Register
_MSR_L3_COS1        = 0xC91  # L3 CAT COS1 Register

# intel_uncore_frequency sysfs root and the base clock used to convert a
# reported uncore frequency (kHz) into a ring ratio (base-clock multiple).
_UNCORE_SYSFS = "/sys/devices/system/cpu/intel_uncore_frequency"
_UNCORE_BCLK_KHZ = 100_000

logger = logging.getLogger(__name__)


class TccPlatformCapability:

    @staticmethod
    def probe() -> "PlatformCapability":
        num_cores = TccPlatformCapability._probe_num_cores()

        # MSR access classification (probed once each).
        uncore_ratio_limit = TccPlatformCapability._msr_access(_MSR_UNCORE_RATIO)
        rdt_pqr_assoc = TccPlatformCapability._msr_access(_MSR_PQR_ASSOC)
        rdt_qm_evtsel = TccPlatformCapability._msr_access(_MSR_QM_EVTSEL)
        # QM_CTR is read-only by nature; skip the write-back probe.
        rdt_qm_ctr = TccPlatformCapability._msr_access(_MSR_QM_CTR, probe_write=False)
        l3_cat_cos0 = TccPlatformCapability._msr_access(_MSR_L3_COS0)
        l3_cat_cos1 = TccPlatformCapability._msr_access(_MSR_L3_COS1)

        capability = PlatformCapability(
            # Prerequisite
            is_root=os.geteuid() == 0,
            msr_module_loaded=os.path.exists("/dev/cpu/0/msr"),
            cpupower_available=TccPlatformCapability._tool_available("cpupower"),
            tuna_available=TccPlatformCapability._tool_available("tuna"),
            # Capability discovery
            available_governors=TccPlatformCapability._probe_governors(),
            num_cores=num_cores,
            c_states=TccPlatformCapability._probe_c_states(num_cores),
            supported_freq_range=TccPlatformCapability._probe_freq_range(),
            uncore_driver_present=os.path.exists(_UNCORE_SYSFS),
            supported_uncore_ratio_range=TccPlatformCapability._probe_uncore_ratio_range(),
            msr_uncore_ratio_limit=uncore_ratio_limit,
            msr_rdt_pqr_assoc=rdt_pqr_assoc,
            msr_rdt_qm_evtsel=rdt_qm_evtsel,
            msr_rdt_qm_ctr=rdt_qm_ctr,
            msr_l3_cat_cos0=l3_cat_cos0,
            msr_l3_cat_cos1=l3_cat_cos1,
            # Default: Limited to 2 CLOS (COS0/COS1) support only.
            # [ETHTSN-280: Custom CLOS support is not implemented yet]
            supported_clos_count=int(l3_cat_cos0.readable) + int(l3_cat_cos1.readable),
            resource_monitoring_available=(
                rdt_qm_evtsel.readable and rdt_qm_ctr.readable
            ),
        )

        logger.info(TccPlatformCapability._format_probe_summary(capability))
        return capability

    @staticmethod
    def _format_probe_summary(cap: "PlatformCapability") -> str:
        """Render a human-readable summary of a probed capability for logging.

        Each field is formatted next to its own label so the layout cannot
        drift out of sync (unlike a positional ``%s`` argument list).

        :param PlatformCapability cap: The probed platform capability.
        :return: A multi-line, box-drawn summary string.
        :rtype: str
        """
        c_states = TccPlatformCapability._summarize_c_states(cap.c_states)
        return (
            "\nProbed TCC capability:\n"
            "  ┌─ Tools ────────────────────────────────\n"
            f"  │  MSR loaded:          {cap.msr_module_loaded}\n"
            f"  │  cpupower available:  {cap.cpupower_available}\n"
            f"  │  tuna available:      {cap.tuna_available}\n"
            "  ├─ CPU ─────────────────────────────────\n"
            f"  │  total cores:         {cap.num_cores}\n"
            f"  │  governors:           {cap.available_governors}\n"
            f"  │  C-states:            {c_states}\n"
            f"  │  freq range (MHz):    {cap.supported_freq_range}\n"
            "  ├─ Uncore ──────────────────────────────\n"
            f"  │  driver present:      {cap.uncore_driver_present}\n"
            f"  │  ratio range:         {cap.supported_uncore_ratio_range}\n"
            "  ├─ MSR access (RO / RW / NOT_ACCESSIBLE) ─\n"
            f"  │  0x620 UNCORE_RATIO:  {cap.msr_uncore_ratio_limit.value}\n"
            f"  │  0xC8F PQR_ASSOC:     {cap.msr_rdt_pqr_assoc.value}\n"
            f"  │  0xC8D QM_EVTSEL:     {cap.msr_rdt_qm_evtsel.value}\n"
            f"  │  0xC8E QM_CTR:        {cap.msr_rdt_qm_ctr.value}\n"
            f"  │  0xC90 L3_COS0:       {cap.msr_l3_cat_cos0.value}\n"
            f"  │  0xC91 L3_COS1:       {cap.msr_l3_cat_cos1.value}\n"
            "  ├─ RDT / CAT ───────────────────────────\n"
            f"  │  CLOS count:          {cap.supported_clos_count}\n"
            f"  │  monitoring avail:    {cap.resource_monitoring_available}\n"
            "  └────────────────────────────────────────"
        )

    @staticmethod
    def _tool_available(tool: str) -> bool:
        """Return ``True`` if the specified tool is found on ``PATH``."""
        available = shutil.which(tool) is not None
        if not available:
            logger.warning("Tool '%s' not found on PATH", tool)
        return available

    @staticmethod
    def _probe_governors() -> tuple:
        """Read available CPU frequency governors from sysfs."""
        path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"
        try:
            with open(path) as f:
                return tuple(f.read().strip().split())
        except OSError as exc:
            logger.warning("Could not read CPU governors from %s: %s", path, exc)
            return ()

    @staticmethod
    def _probe_num_cores() -> int:
        """Return total logical CPU count via ``nproc --all``."""
        try:
            result = subprocess.run(
                ["nproc", "--all"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return int(result.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning(
                "nproc --all failed (%s), falling back to os.cpu_count()", exc
            )
            return os.cpu_count() or 0

    @staticmethod
    def _probe_c_states(num_cores: int) -> dict:
        """Probe per-Core idle states (C-states) via ``cpupower idle-info``.

        Each Core is probed individually so hybrid platforms (mixed
        P-core/E-core) with differing idle-state counts or exit latencies are
        captured accurately. Cores that cannot be probed are omitted.
        """
        cores = range(num_cores) if num_cores and num_cores > 0 else range(1)
        result: dict = {}
        for core_id in cores:
            states = TccPlatformCapability._probe_c_states_for_core(core_id)
            if states:
                result[core_id] = states
        if not result:
            logger.warning("Could not probe C-states via cpupower idle-info")
        return result

    @staticmethod
    def _probe_c_states_for_core(core_id: int) -> tuple:
        """Return the ordered C-states for a single logical Core (empty on failure)."""
        try:
            result = subprocess.run(
                ["cpupower", "-c", str(core_id), "idle-info"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("cpupower idle-info failed for core %d: %s", core_id, exc)
            return ()
        return TccPlatformCapability._parse_idle_info(result.stdout)

    @staticmethod
    def _parse_idle_info(output: str) -> tuple:
        """Parse ``cpupower -c <core-id> idle-info`` output into ordered C-states.
        Output: tuple of CStateInfo objects, ordered by cpupower idle-set index.
        Example:
        (
            CStateInfo(index=0, name='POLL', latency_us=None),
            CStateInfo(index=1, name='C1_ACPI', latency_us=1),
            CStateInfo(index=2, name='C2_ACPI', latency_us=253),
            CStateInfo(index=3, name='C3_ACPI', latency_us=1048),
        )
        """
        # Search header pattern for idle states. (e.g. "POLL:", "C1_ACP1:")
        state_header_re = re.compile(r"^([A-Za-z][\w\-]*):$")

        # Each entry is a [name, latency_us] pair for one state block.
        parsed_states: list = []
        for line in output.splitlines():
            stripped = line.strip()

            header = state_header_re.match(stripped)
            if header:
                # Save the state name and initialize latency to None.
                # The latency will be filled in when we encounter a "Latency:" line.
                parsed_states.append([header.group(1), None])
                continue

            # A "Latency:" line describes the most recent state block.
            if parsed_states and stripped.startswith("Latency:"):
                try:
                    # Extract the latency value after the colon and convert to int.
                    parsed_states[-1][1] = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass

        return tuple(
            CStateInfo(index=index, name=name, latency_us=latency_us)
            for index, (name, latency_us) in enumerate(parsed_states)
        )

    @staticmethod
    def _summarize_c_states(c_states: dict) -> str:
        """Render a compact per-Core C-state summary for logging.

        Cores that share an identical set of C-states are grouped together so
        hybrid topologies remain easy to read.

        Example:
        {
            ((0,'POLL',None),(1,'C1',1),...): [0, 1, 2, 3],   # P-cores
            ((0,'POLL',None),(1,'C1',2),...): [4, 5, 6, 7],   # E-cores
        }
        """

        if not c_states:
            return "none"

        # Group the cores by their C-state signature (tuple of (index, name, latency_us)).
        consolidated_cores_groups: dict = {}
        for core_id, states in c_states.items():
            signature = tuple((s.index, s.name, s.latency_us) for s in states)
            logger.debug(f"Core {core_id} C-states: {signature}")
            logger.debug(f"consolidated_cores_groups: {consolidated_cores_groups}")
            if signature not in consolidated_cores_groups:
                consolidated_cores_groups[signature] = []

            # Append the core_id to the list of cores that share this signature.
            consolidated_cores_groups[signature].append(core_id)

        # Render the grouped summary as a string.
        parts = []
        for sig, cores in consolidated_cores_groups.items():
            names = " ".join(f"{id}:{name}({latency}us)" for id, name, latency in sig)
            parts.append(f"cores {sorted(cores)}: {names}")
        return " | ".join(parts)

    @staticmethod
    def _probe_freq_range() -> tuple:
        """Read CPU frequency range (MHz) from sysfs cpuinfo_{min,max}_freq."""
        min_path = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq"
        max_path = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
        try:
            with open(min_path) as f:
                min_mhz = int(f.read().strip()) // 1000
            with open(max_path) as f:
                max_mhz = int(f.read().strip()) // 1000
            return (min_mhz, max_mhz)
        except OSError as exc:
            logger.warning("Could not read CPU frequency range from sysfs: %s", exc)
            return (0, 0)

    @staticmethod
    def _probe_uncore_ratio_range() -> tuple:
        """Read the supported uncore/ring ratio range from sysfs.

        The ring ratio is the uncore bus frequency as a multiple of the
        platform base clock (typically 100 MHz), exposed per uncore domain by
        the ``intel_uncore_frequency`` driver. The widest range across all
        domains is returned, or ``(0, 0)`` when the driver is absent.
        """
        if not os.path.isdir(_UNCORE_SYSFS):
            logger.warning(
                "intel_uncore_frequency sysfs not present at %s", _UNCORE_SYSFS
            )
            return (0, 0)

        try:
            domains = os.listdir(_UNCORE_SYSFS)
        except OSError as exc:
            logger.warning(
                "Could not list uncore domains under %s: %s", _UNCORE_SYSFS, exc
            )
            return (0, 0)

        min_ratios = []
        max_ratios = []
        for domain in domains:
            dpath = os.path.join(_UNCORE_SYSFS, domain)
            try:
                with open(os.path.join(dpath, "initial_min_freq_khz")) as f:
                    min_khz = int(f.read().strip())
                with open(os.path.join(dpath, "initial_max_freq_khz")) as f:
                    max_khz = int(f.read().strip())
            except (OSError, ValueError):
                continue
            min_ratios.append(min_khz // _UNCORE_BCLK_KHZ)
            max_ratios.append(max_khz // _UNCORE_BCLK_KHZ)

        if not min_ratios or not max_ratios:
            logger.warning("Could not read uncore ratio limits from %s", _UNCORE_SYSFS)
            return (0, 0)
        return (min(min_ratios), max(max_ratios))

    @staticmethod
    def _rdmsr(msr: int) -> Optional[str]:
        """Read *msr* on CPU 0 via ``rdmsr``.

        :param int msr: The MSR address to read.
        :return: The raw value as printed by ``rdmsr`` (a hex string WITHOUT a
            ``0x`` prefix), or ``None`` if the MSR could not be read.
        :rtype: Optional[str]
        """
        try:
            result = subprocess.run(
                ["rdmsr", "-p", "0", f"{msr:#x}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("MSR %#x is not readable on CPU 0: %s", msr, exc)
            return None

    @staticmethod
    def _msr_access(msr: int, probe_write: bool = True) -> "MsrAccess":
        """Classify access to *msr* on CPU 0 as NOT_ACCESSIBLE / RO / RW.

        A single ``rdmsr`` determines readability; when *probe_write* is set, a
        write-back of the current value determines writability. Registers that
        are read-only by nature should pass ``probe_write=False`` to skip the
        write-back entirely.

        :param int msr: The MSR address to classify.
        :param bool probe_write: Whether to attempt a write-back probe.
        :return: The probed access level.
        :rtype: MsrAccess
        """
        current = TccPlatformCapability._rdmsr(msr)
        if current is None:
            # _rdmsr already logged why the read failed.
            return MsrAccess.NOT_ACCESSIBLE
        if probe_write and TccPlatformCapability._write_back(msr, current):
            return MsrAccess.RW
        return MsrAccess.RO

    @staticmethod
    def _write_back(msr: int, current: str) -> bool:
        """
        Return True if ``wrmsr`` can write *current* back to *msr* on CPU 0.

        WARNING: This method performs a write-back of the current MSR value, which may have side effects.
        Use with caution and only on MSRs that are known to be safe to write back.

        :param int msr: The MSR address to write.
        :param str current: The raw value from ``rdmsr`` (hex WITHOUT a ``0x`` prefix).
        :return: True if the write-back succeeded.
        :rtype: bool
        """
        # rdmsr prints the value as hex WITHOUT a "0x" prefix. wrmsr parses
        # its value argument with base auto-detection, so a leading "0" would
        # be treated as octal. Prefix with "0x" to force hex interpretation.
        current_val = f"0x{current}"
        logger.debug("Current value of MSR %#x on CPU 0: %s", msr, current_val)
        try:
            subprocess.run(
                ["wrmsr", "-p", "0", f"{msr:#x}", current_val],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("MSR %#x is not writable on CPU 0: %s", msr, exc)
            return False
