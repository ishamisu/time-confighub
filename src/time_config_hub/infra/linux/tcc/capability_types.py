# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
TCC platform capability data types.

Describe *what the platform supports* so that a requested TCC configuration
can be validated against reality before any change is applied.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple


class MsrAccess(Enum):
    """Probed access level for a single MSR.

    The write probe is a read-then-write-back, so writability always implies
    readability.
    """

    NOT_ACCESSIBLE = "not_accessible"  # rdmsr failed
    RO = "ro"                          # rdmsr ok, wrmsr write-back failed
    RW = "rw"                          # rdmsr + wrmsr write-back ok

    @property
    def readable(self) -> bool:
        return self in (MsrAccess.RO, MsrAccess.RW)

    @property
    def writable(self) -> bool:
        return self is MsrAccess.RW


@dataclass(frozen=True)
class CStateInfo:
    """Core idle state (C-state) information."""

    index: int  # idle state index (0..N)
    name: str  # idle state name (e.g. "POLL")
    latency_us: Optional[int] = None  # Wake-up time from this C-state.


@dataclass(frozen=True)
class PlatformCapability:
    """Immutable snapshot of platform tuning capabilities."""

    #--- Prerequisites -------------------------------------------------------
    is_root: bool = False
    msr_module_loaded: bool = False
    cpupower_available: bool = False
    tuna_available: bool = False

    #--- CPU Core Capabilities -----------------------------------------------
    num_cores: int = None
    available_governors: Tuple[str, ...] = None
    c_states: Dict[int, Tuple[CStateInfo, ...]] = None
    supported_freq_range: Tuple[int, int] = None

    # Uncore Frequency
    # Checks /sys/devices/system/cpu/intel_uncore_frequency sysfs driver present
    uncore_driver_present: bool = False

    # Supported ring/uncore ratio range as base-clock multiples, derived from
    # intel_uncore_frequency initial_{min,max}_freq_khz. (0, 0) if unknown.
    supported_uncore_ratio_range: Tuple[int, int] = None

    #--- MSR Access Probes ---------------------------------------------------
    # Each MSR is probed once and classified as NOT_ACCESSIBLE / RO / RW.
    # Use the .readable / .writable properties for gating checks.

    # MSR 0x620 – Uncore Ratio Limit
    msr_uncore_ratio_limit: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # MSR 0xC8F – PQR_ASSOC (RDT/CAT Resource Association)
    msr_rdt_pqr_assoc: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # MSR 0xC8D – IA32_QM_EVTSEL (RDT monitoring event/RMID select)
    msr_rdt_qm_evtsel: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # MSR 0xC8E – IA32_QM_CTR (RDT monitoring counter, read-only by definition)
    msr_rdt_qm_ctr: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # MSR 0xC90 – L3 CAT COS0
    msr_l3_cat_cos0: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # MSR 0xC91 – L3 CAT COS1
    msr_l3_cat_cos1: MsrAccess = MsrAccess.NOT_ACCESSIBLE

    # Number of usable L3 CAT Classes of Service (MSR-only probe).
    supported_clos_count: int = 0

    # RDT cache/memory-bandwidth monitoring availability (MSR-only probe).
    resource_monitoring_available: bool = False
