# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
Check a parsed TCC configuration against the actual platform capabilities.

A TCC configuration can pass schema validation and still be incompatible with
a particular platform. For example, it may reference a core-id greater than
the number of cores available on the platform, or specify a frequency governor
that is not supported.

This module provides functions that identify mismatches between the parsed
configuration and the platform's actual capabilities, returning errors or
warnings for unsupported or potentially problematic settings.

"""

import logging
from typing import List, Tuple

from time_config_hub.infra.linux.tcc.capability_types import PlatformCapability
from time_config_hub.services.tcc.api import TCCConfigDataAPI
from time_config_hub.services.tcc.schemas.tcc_data_types import (
    TccConfigProfile,
    FrequencyProfile,
)

logger = logging.getLogger(__name__)


def validate_against_capability(
    tcc_api: TCCConfigDataAPI, capability: PlatformCapability
) -> Tuple[List[str], List[str]]:
    """Validate a parsed TCC configuration against probed platform capabilities."""
    errors: List[str] = []
    warnings: List[str] = []
    profile: TccConfigProfile = tcc_api.mapped_data

    _check_prerequisites(profile, capability, errors, warnings)

    if profile.core_isolation:
        _check_core_isolation(profile, capability, errors)
    if profile.core_frequency:
        _check_core_frequency(tcc_api, profile, capability, errors, warnings)
    if profile.uncore_frequency:
        _check_uncore_frequency(profile, capability, errors)
    if profile.platform_qos_resource_config:
        _check_platform_qos(profile, capability, errors)

    return errors, warnings


def _core_in_range(core_id: int, capability: PlatformCapability) -> bool:
    """Check if a core id is valid for the platform."""
    if not capability.num_cores or capability.num_cores <= 0:
        logger.warning(
            "Platform capability does not report a valid number of cores; cannot validate core-id."
        )
        # Unknown core count information; assume core-id is valid to avoid false errors.
        return True
    return 0 <= core_id < capability.num_cores


def _check_prerequisites(
    profile: TccConfigProfile,
    capability: PlatformCapability,
    errors: List[str],
    warnings: List[str],
) -> None:
    """General TCC requirements needed to apply the configuration."""
    if not capability.is_root:
        errors.append(
            "Not running as root; applying a TCC configuration requires root privileges."
        )

    # --- Tools Dependencies --------------------------------------------------
    needs_msr = profile.uncore_frequency or profile.platform_qos_resource_config
    if needs_msr and not capability.msr_module_loaded:
        errors.append(
            "msr kernel module not loaded; "
            "uncore-frequency and platform-qos-resource-config cannot be applied."
        )

    if not capability.cpupower_available:
        warnings.append(
            "'cpupower' tool not installed; "
            "core-frequency and C-state tuning may not be applied."
        )

    if not capability.tuna_available:
        warnings.append(
            "'tuna' tool not installed; " "core-isolation tuning may not be applied."
        )


def _check_core_isolation(
    profile: TccConfigProfile, capability: PlatformCapability, errors: List[str]
) -> None:
    """Validate core-isolation core ids against the available core count."""

    if not profile.core_isolation:
        return

    for assignment in profile.core_isolation.assignments:
        if not _core_in_range(assignment.core_id, capability):
            errors.append(
                f"core-id {assignment.core_id} exceeds available cores "
                f"({capability.num_cores})."
            )


def _check_core_frequency(
    tcc_api: TCCConfigDataAPI,
    profile: TccConfigProfile,
    capability: PlatformCapability,
    errors: List[str],
    warnings: List[str],
) -> None:
    """Validate governors, frequency ranges and idle state overrides."""
    core_frequency = profile.core_frequency
    if not core_frequency:
        return

    governors = capability.available_governors or ()
    freq_range = capability.supported_freq_range

    for profile_id, freq_profile in core_frequency.frequency_profiles.items():
        freq_config = freq_profile.frequency_config

        if governors and freq_config.governor not in governors:
            errors.append(
                f"core-frequency profile '{profile_id}': governor '{freq_config.governor}' "
                f"not available on this platform {list(governors)}."
            )

        if freq_range and freq_range != (0, 0):
            low, high = freq_range
            if freq_config.min_freq_mhz < low or freq_config.max_freq_mhz > high:
                errors.append(
                    f"core-frequency profile '{profile_id}': frequency range "
                    f"{freq_config.min_freq_mhz}-{freq_config.max_freq_mhz} MHz is outside the "
                    f"supported range {low}-{high} MHz."
                )

        _check_idle_config(
            tcc_api, profile_id, freq_profile, capability, errors, warnings
        )


def _check_idle_config(
    tcc_api: TCCConfigDataAPI,
    profile_id: str,
    freq_profile: FrequencyProfile,
    capability: PlatformCapability,
    errors: List[str],
    warnings: List[str],
) -> None:
    """Validate state-override ids and preview disable-by-latency effects."""
    idle = freq_profile.idle_config
    if not idle:
        return

    assigned_cores = tcc_api.cores_for_frequency_profile(profile_id)
    c_states = capability.c_states or {}

    for core_id in assigned_cores:
        states = c_states.get(core_id)
        if not states:
            continue
        state_count = len(states)

        for override in idle.state_overrides:
            if not 0 <= override.state_id < state_count:
                errors.append(
                    f"core-frequency profile '{profile_id}': state-id {override.state_id} is "
                    f"invalid for core {core_id} (has {state_count} C-states, valid 0..{state_count - 1})."
                )

        if idle.disable_by_latency_us is not None:
            has_match = any(
                s.latency_us is not None and s.latency_us > idle.disable_by_latency_us
                for s in states
            )
            if not has_match:
                warnings.append(
                    f"core-frequency profile '{profile_id}': disable-by-latency-us="
                    f"{idle.disable_by_latency_us} matches no C-state on core {core_id} (no-op)."
                )


def _check_uncore_frequency(
    profile: TccConfigProfile, capability: PlatformCapability, errors: List[str]
) -> None:
    """Validate uncore ring ratios against MSR access and supported range."""
    uncore = profile.uncore_frequency
    if not uncore:
        return

    if not capability.msr_uncore_ratio_limit.writable:
        errors.append(
            "uncore-frequency: MSR 0x620 is not writable; ring ratio cannot be set."
        )

    ratio_range = capability.supported_uncore_ratio_range
    for ring in uncore.ring_freqs:
        if not _core_in_range(ring.core_id, capability):
            errors.append(
                f"uncore-frequency: core-id {ring.core_id} exceeds available cores "
                f"({capability.num_cores})."
            )
        if ratio_range and ratio_range != (0, 0):
            low, high = ratio_range
            if ring.min_ring_ratio < low or ring.max_ring_ratio > high:
                errors.append(
                    f"uncore-frequency: core {ring.core_id} ring ratio "
                    f"{ring.min_ring_ratio}-{ring.max_ring_ratio} is outside the supported "
                    f"range {low}-{high}."
                )


def _check_platform_qos(
    profile: TccConfigProfile, capability: PlatformCapability, errors: List[str]
) -> None:
    """Validate CLOS ids and resource-monitoring against MSR-probed limits."""
    pqr = profile.platform_qos_resource_config
    if not pqr:
        return

    if not capability.msr_rdt_pqr_assoc.writable:
        errors.append(
            "platform-qos-resource-config: MSR 0xC8F (PQR_ASSOC) is not writable; "
            "CLOS/RMID cannot be assigned."
        )

    clos_count = capability.supported_clos_count
    for assoc in pqr.core_qos_associations:
        if not _core_in_range(assoc.core_id, capability):
            errors.append(
                f"platform-qos-resource-config: core-id {assoc.core_id} exceeds available cores "
                f"({capability.num_cores})."
            )

        if clos_count and assoc.class_of_service_id >= clos_count:
            errors.append(
                f"platform-qos-resource-config: core {assoc.core_id} class-of-service-id "
                f"{assoc.class_of_service_id} exceeds supported CLOS count "
                f"({clos_count}, valid 0..{clos_count - 1})."
            )

        monitoring = assoc.resource_monitoring
        if (
            monitoring
            and monitoring.enabled
            and not capability.resource_monitoring_available
        ):
            errors.append(
                f"platform-qos-resource-config: core {assoc.core_id} resource-monitoring is "
                f"enabled but RDT monitoring is unavailable on this platform."
            )
