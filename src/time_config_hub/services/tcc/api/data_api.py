# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
TCC Data Access API for querying and accessing configuration data.

Provides high-level methods to query TCC subsystem containers, access individual
configuration elements, and validate TCC profiles.
"""

from typing import Any, Dict, List, Optional, Set

from time_config_hub.services.tcc.schemas.tcc_data_mapping import (
    TCCRawToDataModelMapping,
)
from time_config_hub.services.tcc.schemas.tcc_data_types import (
    CoreFrequency,
    CoreIsolationPlan,
    FrequencyProfile,
)


class TCCConfigDataAPI:
    """
    High-level data access API for TCC configuration.

    Provides methods to:
    - Query available containers
    - Access Core isolation scheduling configuration
    - Access Core frequency profiles and assignments
    - Validate configuration consistency
    """

    def __init__(self, documents: List[Dict[str, Any]]):
        """
        Initialize TCC Data API with parsed documents.

        :param List[Dict[str, Any]] documents: Parsed configuration documents from UniversalParser
        :raises InvalidInputDataError: If profile-id is missing from documents
        """
        self.mapped_data = TCCRawToDataModelMapping.documents_to_tcc_data_model(
            documents
        )

    @property
    def profile_id(self) -> str:
        """Get the TCC profile ID."""
        return self.mapped_data.profile_id

    @property
    def profile_description(self) -> Optional[str]:
        """Get the TCC profile description."""
        return self.mapped_data.profile_description

    def list_of_subsystem_configured(self) -> Set[str]:
        """
        Get set of available TCC subsystem configured in the profile.

        :return: Set of subsystem names
        :rtype: Set[str]
        """
        configured_subsystems = set()

        if self.mapped_data.core_isolation:
            configured_subsystems.add("core-isolation")

        if self.mapped_data.core_frequency:
            configured_subsystems.add("core-frequency")

        if self.mapped_data.uncore_frequency:
            configured_subsystems.add("uncore-frequency")

        if self.mapped_data.platform_qos_resource_config:
            configured_subsystems.add("platform-qos-resource-config")

        return configured_subsystems

    def core_isolation(self) -> Optional[CoreIsolationPlan]:
        """
        Get Core isolation configuration.

        :return: CoreIsolationPlan instance or None if not configured
        :rtype: Optional[CoreIsolationPlan]
        """
        return self.mapped_data.core_isolation

    def isolated_cores(self) -> List[int]:
        """
        Get list of Core IDs marked as isolated.

        :return: List of isolated Core IDs
        :rtype: List[int]
        """
        if not self.mapped_data.core_isolation:
            return []

        isolated = []
        for assignment in self.mapped_data.core_isolation.assignments:
            if assignment.isolate:
                isolated.append(assignment.core_id)
        return isolated

    def non_isolated_cores(self) -> List[int]:
        """
        Get list of Core IDs NOT marked as isolated (housekeeping).

        :return: List of non-isolated Core IDs
        :rtype: List[int]
        """
        if not self.mapped_data.core_isolation:
            return []

        non_isolated = []
        for assignment in self.mapped_data.core_isolation.assignments:
            if not assignment.isolate:
                non_isolated.append(assignment.core_id)
        return non_isolated

    def core_frequency_profiles(self) -> Optional[CoreFrequency]:
        """
        Get Core frequency profile configuration.

        :return: CoreFrequency instance or None if not configured
        :rtype: Optional[CoreFrequency]
        """
        return self.mapped_data.core_frequency

    def frequency_profile(self, profile_id: str) -> Optional[FrequencyProfile]:
        """
        Get a specific frequency profile by ID.

        :param str profile_id: Profile ID to retrieve
        :return: FrequencyProfile instance or None if not found
        :rtype: Optional[FrequencyProfile]
        """
        if not self.mapped_data.core_frequency:
            return None
        return self.mapped_data.core_frequency.frequency_profiles.get(profile_id)

    def all_frequency_profiles(self) -> Dict[str, FrequencyProfile]:
        """
        Get all defined frequency profiles.

        :return: Dictionary mapping profile ID to FrequencyProfile instance
        :rtype: Dict[str, FrequencyProfile]
        """
        if not self.mapped_data.core_frequency:
            return {}
        return self.mapped_data.core_frequency.frequency_profiles

    def core_frequency_assignment(self, core_id: int) -> Optional[str]:
        """
        Get frequency profile assigned to a specific core.

        :param int core_id: Core ID to query
        :return: Profile ID assigned to the core, or None if not assigned
        :rtype: Optional[str]
        """
        if not self.mapped_data.core_frequency:
            return None
        for (
            assignment
        ) in self.mapped_data.core_frequency.profile_assignments.core_assignments:
            if assignment.core_id == core_id:
                return assignment.profile_ref
        return None

    def frequency_profile_for_core(self, core_id: int) -> Optional[FrequencyProfile]:
        """
        Get the frequency profile configuration for a specific core.

        :param int core_id: Core ID to query
        :return: FrequencyProfile instance or None if core not assigned a profile
        :rtype: Optional[FrequencyProfile]
        """
        if not self.mapped_data.core_frequency:
            return None
        profile_id = self.core_frequency_assignment(core_id)
        if not profile_id:
            return None
        return self.mapped_data.core_frequency.frequency_profiles.get(profile_id)

    def cores_for_frequency_profile(self, profile_id: str) -> List[int]:
        """
        Get all cores assigned to a specific frequency profile.

        :param str profile_id: Profile ID to query
        :return: List of core IDs assigned to the profile
        :rtype: List[int]
        """
        if not self.mapped_data.core_frequency:
            return []

        core_ids = []
        for (
            assignment
        ) in self.mapped_data.core_frequency.profile_assignments.core_assignments:
            if assignment.profile_ref == profile_id:
                core_ids.append(assignment.core_id)
        return core_ids

    def validate_consistency(self) -> List[str]:
        """
        Validate configuration consistency and return any warnings/errors.

        Checks for:
        - Cores assigned in core isolation but not in frequency profiles
        - Undefined profile references
        - Missing or inconsistent configurations

        :return: List of validation messages (empty if valid)
        :rtype: List[str]
        """
        issues: List[str] = []

        # Check Core isolation vs frequency assignments
        if self.mapped_data.core_isolation and self.mapped_data.core_frequency:
            scheduled_cores = {
                a.core_id for a in self.mapped_data.core_isolation.assignments
            }
            assigned_cores = {
                assignment.core_id
                for assignment in self.mapped_data.core_frequency.profile_assignments.core_assignments
            }

            missing_assignments = scheduled_cores - assigned_cores
            if missing_assignments:
                issues.append(
                    f"Cores scheduled but not assigned to frequency profile: {missing_assignments}"
                )

            extra_assignments = assigned_cores - scheduled_cores
            if extra_assignments:
                issues.append(
                    f"Cores assigned to frequency profile but not scheduled: {extra_assignments}"
                )

        # Check profile references are valid
        if self.mapped_data.core_frequency:
            valid_profiles = set(
                self.mapped_data.core_frequency.frequency_profiles.keys()
            )
            referenced_profiles = {
                assignment.profile_ref
                for assignment in self.mapped_data.core_frequency.profile_assignments.core_assignments
            }
            undefined_refs = referenced_profiles - valid_profiles

            if undefined_refs:
                issues.append(f"Undefined profile references: {undefined_refs}")

        return issues

    def summary(self) -> str:
        """
        Generate a human-readable summary of the TCC configuration.

        :return: Formatted configuration summary
        :rtype: str
        """
        lines = [
            f"TCC Profile: {self.mapped_data.profile_id}",
            f"Description: {self.mapped_data.profile_description or '(none)'}",
            f"Selected TCC Subsystems: {sorted(self.list_of_subsystem_configured())}",
        ]

        isolated = self.isolated_cores()
        if isolated:
            lines.append(f"Isolated Cores: {isolated}")

        freq_profiles = self.all_frequency_profiles()
        if freq_profiles:
            lines.append(f"Frequency Profiles: {list(freq_profiles.keys())}")
            for profile_id, profile in freq_profiles.items():
                cores = self.cores_for_frequency_profile(profile_id)
                lines.append(
                    f"  {profile_id}: {profile.frequency_config.governor} "
                    f"({profile.frequency_config.min_freq_mhz}-{profile.frequency_config.max_freq_mhz} MHz), Cores: {cores}"
                )

        return "\n".join(lines)
