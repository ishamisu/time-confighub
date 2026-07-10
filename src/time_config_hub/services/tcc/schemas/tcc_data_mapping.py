# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
TCC data mapping module for converting XML configuration to Python dataclasses.

This module defines the TCCRawToDataModelMapping class, which provides static methods
to transform raw parsed documents from the UniversalParser into strongly-typed TCCProfile dataclass instances.

This module provides logic to transform raw libyang-parsed documents
(List[Dict]) into strongly-typed TCC dataclass instances.

It supports:
- Extracting profile ID and description
- Mapping Core isolation assignments
- Mapping Core frequency profiles and assignments
- Mapping uncore frequency and platform QoS resource settings

"""
from typing import Any, Optional, cast

import time_config_hub.services.tcc.schemas.tcc_data_types as tcc_types
from time_config_hub.utils.yang_parser.exceptions import InvalidInputDataError


class TCCRawToDataModelMapping:
    """
    Mapper for converting raw parsed documents to TCC domain models.

    Provides static methods to extract and transform TCC configuration
    data from libyang parsed documents into strongly-typed dataclasses.
    """

    @staticmethod
    def documents_to_tcc_data_model(documents: list[dict[str, Any]]) -> tcc_types.TccConfigProfile:
        """
        Convert raw parsed documents to a TCC profile dataclass instance.

        :param List[Dict[str, Any]] documents: Parsed configuration documents from UniversalParser
        :return: TccConfigProfile instance
        :rtype: TccConfigProfile
        :raises InvalidInputDataError: If required profile-id is missing
        :raises InvalidInputDataError: If more than one tcc-config root element is present
        """
        if not documents:
            raise InvalidInputDataError("No documents provided for TCC profile mapping")

        if len(documents) > 1:
            raise InvalidInputDataError(
                f"Only a single tcc-config instance is permitted per XML file, "
                f"but {len(documents)} were found. "
                "Remove the duplicate tcc-config root element(s) and resubmit."
            )

        roots = [TCCRawToDataModelMapping._resolve_profile_root(doc) for doc in documents]
        if not roots:
            raise InvalidInputDataError("No valid document dictionaries provided for TCC profile mapping")

        doc = roots[0]
        profile_id = TCCRawToDataModelMapping._extract_string(doc, "profile-id")
        if not profile_id:
            raise InvalidInputDataError("TCC profile must contain 'profile-id'")

        profile_description = TCCRawToDataModelMapping._extract_string(doc, "profile-description")
        core_isolation = TCCRawToDataModelMapping._map_core_isolation(roots)
        core_frequency = TCCRawToDataModelMapping._map_core_frequency(roots)
        uncore_frequency = TCCRawToDataModelMapping._map_uncore_frequency(roots)
        platform_qos_resource_config = TCCRawToDataModelMapping._map_platform_qos_resource_config(roots)

        return tcc_types.TccConfigProfile(
            profile_id=profile_id,
            profile_description=profile_description,
            core_isolation=core_isolation,
            core_frequency=core_frequency,
            uncore_frequency=uncore_frequency,
            platform_qos_resource_config=platform_qos_resource_config,
        )

    @staticmethod
    def _resolve_profile_root(doc: dict[str, Any]) -> dict[str, Any]:
        """
        Return the TCC profile root from a parsed document. ('tcc-config')

        :param Dict[str, Any] doc: Parsed document
        :return: profile root dictionary, or original document if root key not found
        :rtype: Dict[str, Any]
        """
        tcc_root = doc.get("tcc-config")

        # If 'tcc-config' key exists and is a dict, use it as the root.
        # Otherwise, assume the entire document is the root.
        if isinstance(tcc_root, dict):
            return cast(dict[str, Any], tcc_root)
        return doc

    @staticmethod
    def _extract_string(node: Any, key: str) -> Optional[str]:
        """
        Extract string value from node, handling both dict and string formats.

        libyang can represent leaf nodes as either:
        - dict with attribute. E.g. "#text" key: {"#text": "value"}
        - plain string: "value"

        :param Any node: Dictionary node to extract from
        :param str key: Key to look up
        :return: Extracted string value, or None if not found
        :rtype: Optional[str]
        """

        # Check if node is a dict and contains the key
        if not isinstance(node, dict) or key not in node:
            return None

        # Retrieve the value for the specified key
        value = node[key]

        # If the value is a dict with a "#text" key, extract the text.
        # Otherwise, convert to string
        if isinstance(value, dict) and "#text" in value:
            return str(value.get("#text"))
        return str(value) if value is not None else None

    @staticmethod
    def _extract_int(node: Any, key: str) -> Optional[int]:
        """
        Extract integer value from node.

        :param Any node: Dictionary node to extract from
        :param str key: Key to look up
        :return: Extracted integer value, or None if not found
        :rtype: Optional[int]
        """
        value = TCCRawToDataModelMapping._extract_string(node, key)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_bool(node: Any, key: str) -> bool:
        """
        Extract boolean value from node, defaulting to False if not found.

        :param Any node: Dictionary node to extract from
        :param str key: Key to look up
        :return: Extracted boolean value (False if not found or invalid)
        :rtype: bool
        """
        value = TCCRawToDataModelMapping._extract_string(node, key)
        if value is not None:
            return value.lower() in ("true", "1", "yes")
        return False

    @staticmethod
    def _convert_to_list(value: Any) -> list[dict[str, Any]]:
        """
        Convert a value to a list if it isn't already one.

        This is to unify the handling of libyang parsed data, which can
        represent single items as dicts and multiple items as lists of dicts.

        :param Any value: Value to convert
        :return: List representation of value
        :rtype: List[Dict[str, Any]]
        """

        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    raise InvalidInputDataError(
                        f"Expected a dictionary, but got type {type(item).__name__}: {item!r}"
                    )
            return list(value)

        if isinstance(value, dict):
            return [value]

        return []


    @staticmethod
    def _map_core_isolation(docs: list[dict[str, Any]]) -> Optional[tcc_types.CoreIsolationPlan]:
        """
        Map core isolation scheduling section from documents to CoreIsolationPlan dataclass.

        :param List[Dict[str, Any]] docs: Parsed profile roots
        :return: CoreIsolationPlan instance or None if section not found
        :rtype: Optional[tcc_types.CoreIsolationPlan]
        """
        section_found = False
        core_assignments: list[tcc_types.CoreIsolateAssignment] = []

        for doc in docs:
            core_sched_section = doc.get("core-isolation")
            if not isinstance(core_sched_section, dict):
                continue

            section_found = True
            assignments = TCCRawToDataModelMapping._convert_to_list(core_sched_section.get("core-assignment", []))

            for assignment in assignments:

                core_id = TCCRawToDataModelMapping._extract_int(assignment, "core-id")
                isolate = TCCRawToDataModelMapping._extract_bool(assignment, "isolate")

                if core_id is not None:
                    core_assignments.append(
                        tcc_types.CoreIsolateAssignment(core_id=core_id, isolate=isolate)
                    )

        if not section_found:
            return None
        return tcc_types.CoreIsolationPlan(assignments=core_assignments)

    @staticmethod
    def _map_core_frequency(docs: list[dict[str, Any]]) -> Optional[tcc_types.CoreFrequency]:
        """
        Map Core frequency section from documents to CoreFrequency dataclass.

        :param List[Dict[str, Any]] docs: Parsed profile roots
        :return: CoreFrequency instance or None if section not found
        :rtype: Optional[tcc_types.CoreFrequency]
        """
        section_found = False
        freq_profiles: dict[str, tcc_types.FrequencyProfile] = {}
        core_assignments: list[tcc_types.CoreAssignment] = []

        for doc in docs:
            core_freq_section = doc.get("core-frequency")
            if not isinstance(core_freq_section, dict):
                continue

            section_found = True

            freq_profile_list = TCCRawToDataModelMapping._convert_to_list(
                core_freq_section.get("frequency-profile", [])
            )

            for freq_profile in freq_profile_list:

                # Get Profile ID (required)
                profile_id = TCCRawToDataModelMapping._extract_string(freq_profile, "profile-id")
                if not profile_id:
                    raise InvalidInputDataError("Each frequency-profile must contain a 'profile-id'")

                # Get Core Frequency Control settings
                frequency_info = freq_profile.get("frequency", {})
                if not isinstance(frequency_info, dict):
                    frequency_info = {}

                governor = TCCRawToDataModelMapping._extract_string(frequency_info, "governor")
                min_freq = TCCRawToDataModelMapping._extract_int(frequency_info, "min-freq-mhz")
                max_freq = TCCRawToDataModelMapping._extract_int(frequency_info, "max-freq-mhz")

                # Get C-State Idle Control settings
                idle_info = freq_profile.get("idle", {})
                if not isinstance(idle_info, dict):
                    idle_info = {}

                state_override_list = TCCRawToDataModelMapping._convert_to_list(
                    idle_info.get("state-override", [])
                )
                state_overrides: list[tcc_types.StateOverride] = []

                for state_override in state_override_list:

                    state_id = TCCRawToDataModelMapping._extract_int(state_override, "state-id")
                    action = TCCRawToDataModelMapping._extract_string(state_override, "action")
                    if state_id is not None and action in ("enable", "disable"):
                        state_overrides.append(
                            tcc_types.StateOverride(
                                state_id=state_id,
                                action=action,
                            )
                        )

                idle_config = tcc_types.IdleConfig(
                    enable_all=TCCRawToDataModelMapping._extract_bool(idle_info, "enable-all"),
                    disable_by_latency_us=TCCRawToDataModelMapping._extract_int(
                        idle_info, "disable-by-latency-us"
                    ),
                    state_overrides=state_overrides,
                )

                freq_profiles[profile_id] = tcc_types.FrequencyProfile(
                    profile_id=profile_id,
                    frequency_config=tcc_types.FrequencyConfig(
                        governor=cast(tcc_types.PowerGovernor, governor),
                        min_freq_mhz=min_freq,
                        max_freq_mhz=max_freq,
                    ),
                    idle_config=idle_config,
                )

            profile_assign_section = core_freq_section.get("profile-assignment", {})
            if not isinstance(profile_assign_section, dict):
                profile_assign_section = {}

            core_assign_list = TCCRawToDataModelMapping._convert_to_list(
                profile_assign_section.get("core-assignment", [])
            )

            for core_assignment in core_assign_list:

                core_id = TCCRawToDataModelMapping._extract_int(core_assignment, "core-id")
                profile_ref = TCCRawToDataModelMapping._extract_string(core_assignment, "profile-ref")

                if core_id is not None and profile_ref:
                    core_assignments.append(
                        tcc_types.CoreAssignment(core_id=core_id, profile_ref=profile_ref)
                    )

        if not section_found:
            return None

        return tcc_types.CoreFrequency(
            frequency_profiles=freq_profiles,
            profile_assignments=tcc_types.ProfileAssignment(core_assignments=core_assignments),
        )

    @staticmethod
    def _map_uncore_frequency(docs: list[dict[str, Any]]) -> Optional[tcc_types.UncoreFrequency]:
        """
        Map uncore frequency section from documents.

        :param List[Dict[str, Any]] docs: Parsed profile roots
        :return: UncoreFrequency instance or None if section not found
        :rtype: Optional[tcc_types.UncoreFrequency]
        """
        section_found = False
        ring_freqs: list[tcc_types.CoreRingRatio] = []

        for doc in docs:
            uncore_section = doc.get("uncore-frequency")
            if not isinstance(uncore_section, dict):
                continue

            section_found = True
            core_ring_freq_list = TCCRawToDataModelMapping._convert_to_list(
                uncore_section.get("core-ring-freq", [])
            )

            for core_ring_freq in core_ring_freq_list:

                core_id = TCCRawToDataModelMapping._extract_int(core_ring_freq, "core-id")
                min_ring_ratio = TCCRawToDataModelMapping._extract_int(core_ring_freq, "min-ring-ratio")
                max_ring_ratio = TCCRawToDataModelMapping._extract_int(core_ring_freq, "max-ring-ratio")

                if core_id is None or min_ring_ratio is None or max_ring_ratio is None:
                    continue

                ring_freqs.append(
                    tcc_types.CoreRingRatio(
                        core_id=core_id,
                        min_ring_ratio=min_ring_ratio,
                        max_ring_ratio=max_ring_ratio,
                    )
                )

        if not section_found:
            return None
        return tcc_types.UncoreFrequency(ring_freqs=ring_freqs)

    @staticmethod
    def _map_platform_qos_resource_config(
        docs: list[dict[str, Any]],
    ) -> Optional[tcc_types.PlatformQosResourceConfig]:
        """
        Map platform QoS resource configuration from documents.

        :param List[Dict[str, Any]] docs: Parsed profile roots
        :return: PlatformQosResourceConfig instance or None if section not found
        :rtype: Optional[PlatformQosResourceConfig]
        """
        section_found = False
        associations: list[tcc_types.CoreQosAssociation] = []

        for doc in docs:
            qos_section = doc.get("platform-qos-resource-config")
            if not isinstance(qos_section, dict):
                continue

            section_found = True
            core_pqr_assoc_list = TCCRawToDataModelMapping._convert_to_list(
                qos_section.get("core-pqr-assoc", [])
            )

            for core_pqr_assoc in core_pqr_assoc_list:

                core_id = TCCRawToDataModelMapping._extract_int(core_pqr_assoc, "core-id")
                class_of_service_id = TCCRawToDataModelMapping._extract_int(
                    core_pqr_assoc,
                    "class-of-service-id",
                )
                if core_id is None or class_of_service_id is None:
                    continue

                resource_monitoring_node = core_pqr_assoc.get("resource-monitoring", {})
                if not isinstance(resource_monitoring_node, dict):
                    resource_monitoring_node = {}

                resource_monitoring = tcc_types.ResourceMonitoringConfig(
                    enabled=TCCRawToDataModelMapping._extract_bool(
                        resource_monitoring_node,
                        "enabled",
                    ),
                    rmid_id=TCCRawToDataModelMapping._extract_int(
                        resource_monitoring_node,
                        "rmid-id",
                    ),
                    rmid_label=TCCRawToDataModelMapping._extract_string(
                        resource_monitoring_node,
                        "rmid-label",
                    ),
                )

                associations.append(
                    tcc_types.CoreQosAssociation(
                        core_id=core_id,
                        class_of_service_id=class_of_service_id,
                        resource_monitoring=resource_monitoring,
                    )
                )

        if not section_found:
            return None
        return tcc_types.PlatformQosResourceConfig(core_qos_associations=associations)
