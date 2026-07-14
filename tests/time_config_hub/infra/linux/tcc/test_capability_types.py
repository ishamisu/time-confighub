# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""Unit tests for the TCC platform capability data types."""

from time_config_hub.infra.linux.tcc.capability_types import (
    CStateInfo,
    MsrAccess,
    PlatformCapability,
)


class TestMsrAccess:
    """Tests for the :class:`MsrAccess` enum and its derived properties."""

    def test_enum_values(self):
        """Each member carries its documented string value."""
        assert MsrAccess.NOT_ACCESSIBLE.value == "not_accessible"
        assert MsrAccess.RO.value == "ro"
        assert MsrAccess.RW.value == "rw"

    def test_not_accessible_is_neither_readable_nor_writable(self):
        assert MsrAccess.NOT_ACCESSIBLE.readable is False
        assert MsrAccess.NOT_ACCESSIBLE.writable is False

    def test_ro_is_readable_but_not_writable(self):
        assert MsrAccess.RO.readable is True
        assert MsrAccess.RO.writable is False

    def test_rw_is_both_readable_and_writable(self):
        assert MsrAccess.RW.readable is True
        assert MsrAccess.RW.writable is True

    def test_writable_implies_readable(self):
        """Writability must always imply readability (write is read-then-write)."""
        for access in MsrAccess:
            if access.writable:
                assert access.readable


class TestCStateInfo:
    """Tests for the :class:`CStateInfo` dataclass."""

    def test_construction_with_all_fields(self):
        state = CStateInfo(index=1, name="C1_ACPI", latency_us=1)
        assert state.index == 1
        assert state.name == "C1_ACPI"
        assert state.latency_us == 1

    def test_latency_defaults_to_none(self):
        state = CStateInfo(index=0, name="POLL")
        assert state.latency_us is None

    def test_is_frozen(self):
        """The dataclass is immutable (frozen)."""
        state = CStateInfo(index=0, name="POLL")
        try:
            state.index = 5  # type: ignore[misc]
        except Exception as exc:  # dataclasses raises FrozenInstanceError
            assert exc.__class__.__name__ == "FrozenInstanceError"
        else:
            raise AssertionError("CStateInfo should be immutable")

    def test_equality(self):
        a = CStateInfo(index=2, name="C2_ACPI", latency_us=253)
        b = CStateInfo(index=2, name="C2_ACPI", latency_us=253)
        assert a == b

    def test_is_hashable(self):
        """Frozen dataclasses are hashable and usable in sets/dict keys."""
        state = CStateInfo(index=0, name="POLL")
        assert state in {state}


class TestPlatformCapability:
    """Tests for the :class:`PlatformCapability` dataclass."""

    def test_defaults(self):
        """Verify the conservative default values for a bare instance."""
        cap = PlatformCapability()
        assert cap.is_root is False
        assert cap.msr_module_loaded is False
        assert cap.cpupower_available is False
        assert cap.tuna_available is False
        assert cap.uncore_driver_present is False
        assert cap.supported_clos_count == 0
        assert cap.resource_monitoring_available is False
        assert cap.msr_uncore_ratio_limit is MsrAccess.NOT_ACCESSIBLE
        assert cap.msr_rdt_pqr_assoc is MsrAccess.NOT_ACCESSIBLE
        assert cap.msr_rdt_qm_evtsel is MsrAccess.NOT_ACCESSIBLE
        assert cap.msr_rdt_qm_ctr is MsrAccess.NOT_ACCESSIBLE
        assert cap.msr_l3_cat_cos0 is MsrAccess.NOT_ACCESSIBLE
        assert cap.msr_l3_cat_cos1 is MsrAccess.NOT_ACCESSIBLE

    def test_custom_construction(self):
        c_states = {0: (CStateInfo(index=0, name="POLL"),)}
        cap = PlatformCapability(
            is_root=True,
            num_cores=8,
            available_governors=("performance", "powersave"),
            c_states=c_states,
            supported_freq_range=(800, 3200),
            supported_uncore_ratio_range=(8, 24),
            msr_uncore_ratio_limit=MsrAccess.RW,
        )
        assert cap.is_root is True
        assert cap.num_cores == 8
        assert cap.available_governors == ("performance", "powersave")
        assert cap.c_states == c_states
        assert cap.supported_freq_range == (800, 3200)
        assert cap.supported_uncore_ratio_range == (8, 24)
        assert cap.msr_uncore_ratio_limit.writable is True

    def test_is_frozen(self):
        cap = PlatformCapability()
        try:
            cap.is_root = True  # type: ignore[misc]
        except Exception as exc:
            assert exc.__class__.__name__ == "FrozenInstanceError"
        else:
            raise AssertionError("PlatformCapability should be immutable")
