# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
Time Config TCC Service API Implementation

This service class provides methods to apply, validate, reset and
check the status of Intel TCC configurations.

It uses the ConfigParserService for parsing configuration files and
the TCCStateStore for tracking applied configurations and status.

The TCCService implements the TCCServiceInterface protocol, allowing
it to be used interchangeably with other implementations if needed.

The TccPlatformCapability class is used to probe the platform's capabilities
and validate the requested TCC configuration against what the platform supports.

"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from time_config_hub.infra.linux.tcc.platform_capability import TccPlatformCapability
from time_config_hub.services.common.config_parser import ConfigParserService
from time_config_hub.exceptions import ConfigParseError, TCCConfigError
from time_config_hub.services.common.service_interfaces import TCCServiceInterface
from time_config_hub.services.tcc.state_store import TCCStateStore
from time_config_hub.services.tcc.api import TCCConfigDataAPI
from time_config_hub.services.tcc.capability_validator import (
    validate_against_capability,
)


logger = logging.getLogger(__name__)


class TCCService(TCCServiceInterface):
    """Default TCC service implementation."""

    def __init__(self, tch_config: Dict[str, Any]):
        self.config_dir = Path(tch_config.get("General", {}).get("ConfigDirectory"))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._parser = ConfigParserService(tch_config)
        self._state_store = TCCStateStore(self.config_dir)

    def apply(self, config_file: str, dry_run: bool = False) -> None:
        """
        Apply TCC configuration from the specified file.

        :param config_file: Path to the TCC configuration file
        :param dry_run: If True, validate the configuration without applying it
        :raises TCCConfigError: If there is an error in applying the configuration
        """
        try:
            logger.info(f"Applying TCC configuration from {config_file}")

            # Parse configuration file
            uparser = self._parser.parse_config(config_file)
            raw_docs = uparser.documents

            # Log raw parsed documents for debugging
            logger.debug(f"Parsed configuration documents: {raw_docs}")

            # Translate raw documents to TCC specific data model
            tcc_api = TCCConfigDataAPI(uparser.documents)

            # Log profile information
            logger.info("================= TCC Configuration Summary =================")
            logger.info(f"{tcc_api.summary()}")
            logger.info("=============================================================")

            # Validate configuration consistency
            validation_issues = tcc_api.validate_consistency()
            if validation_issues:
                for issue in validation_issues:
                    logger.warning(f"Configuration validation: {issue}")
                    return

            if dry_run:
                logger.info("Dry-run enabled. TCC configuration was validated only.")
                return

            # Apply configuration (store state)
            self._state_store.save_applied_config(config_file)
            logger.info(f"TCC configuration applied successfully: {config_file}")

        except ConfigParseError as exc:
            logger.error(f"TCC configuration file {config_file} is invalid: {exc}")
            raise TCCConfigError("TCC configuration file is invalid") from exc

        except Exception as exc:
            logger.exception("Failed to apply TCC configuration")
            raise TCCConfigError("Failed to apply TCC configuration") from exc

    def status(self) -> Dict[str, Any]:
        return self._state_store.load_status()

    def reset(self) -> bool:
        return self._state_store.reset()

    def validate(self, config_file: str) -> bool:
        try:
            logger.info(f"Validating TCC configuration file: {config_file}")

            # Parse configuration file
            uparser = self._parser.parse_config(config_file)
            raw_docs = uparser.documents

            # Log raw parsed documents for debugging
            logger.debug(f"Parsed configuration documents: {raw_docs}")

            # Translate raw documents to TCC specific data model
            tcc_api = TCCConfigDataAPI(uparser.documents)

            # Log profile information
            logger.info("================= TCC Configuration Summary =================")
            logger.info(f"{tcc_api.summary()}")
            logger.info("=============================================================")

            if tcc_api.list_of_subsystem_configured() == set():
                logger.warning(
                    f"TCC configuration file {config_file} does not configure any subsystems."
                )
                return True

            # Validate configuration consistency
            validation_issues = tcc_api.validate_consistency()
            if validation_issues:
                for issue in validation_issues:
                    logger.warning(f"Configuration validation: {issue}")
                return False

            # Validate the parsed configuration against probed platform capabilities
            capability = TccPlatformCapability.probe()
            cap_errors, cap_warnings = validate_against_capability(tcc_api, capability)
            for warning in cap_warnings:
                logger.warning(f"TCC Platform capability: {warning}")

            if cap_errors:
                for error in cap_errors:
                    logger.error(f"TCC Platform capability: {error}")
                logger.error(
                    f"TCC configuration file {config_file} is not supported by this platform."
                )
                return False

            logger.info(f"TCC configuration file {config_file} is valid.")
            return True

        except ConfigParseError as exc:
            logger.error(f"TCC configuration file {config_file} is invalid: {exc}")
            return False

        except Exception:
            logger.exception(
                f"Unexpected error validating TCC configuration: {config_file}"
            )
            return False
