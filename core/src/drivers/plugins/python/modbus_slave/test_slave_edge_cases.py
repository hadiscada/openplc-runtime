import pytest
from unittest.mock import MagicMock
import simple_modbus


# =====================================================================
# Fake SafeBufferAccess (valid)
# =====================================================================
@pytest.fixture
def fake_sba():
    sba = MagicMock()
    sba.is_valid = True
    sba.error_msg = "Success"
    sba.acquire_mutex = MagicMock()
    sba.release_mutex = MagicMock()

    ram = {}

    def read_bool_output(addr, thread_safe=False):
        return ram.get(addr, 0), "Success"

    def write_bool_output(addr, val, thread_safe=False):
        ram[addr] = val
        return "Success"

    def read_int_input(addr, thread_safe=False):
        return ram.get(addr, 0), "Success"

    def write_int_output(addr, val, thread_safe=False):
        ram[addr] = val
        return "Success"

    sba.read_bool_output.side_effect = read_bool_output
    sba.write_bool_output.side_effect = write_bool_output
    sba.read_int_input.side_effect = read_int_input
    sba.write_int_output.side_effect = write_int_output

    return sba


# =====================================================================
# Helper: create all block types
# =====================================================================
def get_blocks(sba):
    return {
        "coils": simple_modbus.OpenPLCCoilsDataBlock(8, sba),
        "discretes": simple_modbus.OpenPLCDiscreteInputsDataBlock(8, sba),
        "hold": simple_modbus.OpenPLCHoldingRegistersDataBlock(8, sba),
        "input": simple_modbus.OpenPLCInputRegistersDataBlock(8, sba),
    }


# =====================================================================
# 1. ADDRESS VALIDATION
# =====================================================================
def test_negative_address(fake_sba):
    blocks = get_blocks(fake_sba)

    for name, blk in blocks.items():
        # negative Modbus addresses → should not throw
        result = blk.getValues(-1, 3)
        assert result == [0, 0, 0], f"Block {name} must return zeroes"


def test_zero_address(fake_sba):
    blocks = get_blocks(fake_sba)

    for name, blk in blocks.items():
        result = blk.getValues(0, 2)
        assert result == [0, 0]


def test_address_beyond_range(fake_sba):
    blocks = get_blocks(fake_sba)

    for name, blk in blocks.items():
        result = blk.getValues(100, 5)
        assert result == [0] * 5


def test_partial_out_of_range(fake_sba):
    """Half the requested region inside buffer, half outside."""
    block = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    fake_sba.write_bool_output(0, 1)
    fake_sba.write_bool_output(1, 0)

    # Request addresses 3 → 7 (indices 2–6)
    result = block.getValues(3, 5)

    assert result == [0, 0, 0, 0, 0]


# =====================================================================
# 2. SETVALUES EDGE CASES
# =====================================================================
def test_setvalues_larger_than_range(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    # values exceed internal RAM but should not error
    blk.setValues(1, [1, 0, 1, 1, 0, 0, 1])

    # Only first 4 should be written
    assert blk.getValues(1, 4) == [1, 0, 1, 1]


def test_setvalues_empty_list(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    blk.setValues(1, [])
    result = blk.getValues(1, 3)
    assert result == [0, 0, 0]


def test_setvalues_none(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    blk.setValues(1, None)
    assert blk.getValues(1, 3) == [0, 0, 0]


# =====================================================================
# 3. NUMERIC EDGE CASES (Registers)
# =====================================================================
def test_register_overflow(fake_sba):
    blk = simple_modbus.OpenPLCHoldingRegistersDataBlock(4, fake_sba)

    overflow_value = 70000  # > uint16
    blk.setValues(1, [overflow_value])

    stored = blk.getValues(1, 1)[0]

    # Depending on implementation — assume modulo 65536
    assert stored == overflow_value % 65536


def test_register_negative(fake_sba):
    blk = simple_modbus.OpenPLCHoldingRegistersDataBlock(4, fake_sba)

    blk.setValues(1, [-5])

    stored = blk.getValues(1, 1)[0]

    assert stored == (-5) % 65536


# =====================================================================
# 4. BOOL PACKING EDGE CASES (MAX_BITS logic)
# =====================================================================
def test_bool_block_does_not_accept_big_values(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    blk.setValues(1, [15])  # illegal > 1
    result = blk.getValues(1, 1)

    assert result == [0] or result == [1]  # depends on your rules


def test_large_bool_sequence(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(8, fake_sba)

    blk.setValues(1, [1] * 100)  # far more than 8

    assert blk.getValues(1, 8) == [1] * 8


# =====================================================================
# 5. INVALID SafeBufferAccess BEHAVIOR
# =====================================================================
def test_sba_becomes_invalid():
    sba = MagicMock()
    sba.is_valid = True
    sba.error_msg = "OK"
    sba.acquire_mutex = MagicMock()
    sba.release_mutex = MagicMock()
    sba.read_bool_output.return_value = (1, "Success")

    blk = simple_modbus.OpenPLCCoilsDataBlock(4, sba)

    # Valid first
    assert blk.getValues(1, 1) == [1]

    # Now simulate runtime failure
    sba.is_valid = False
    sba.error_msg = "Simulated Fault"

    assert blk.getValues(1, 1) == [0]


# =====================================================================
# 6. MUTEX VALIDATION
# =====================================================================
def test_mutex_lock_unlock(fake_sba):
    blk = simple_modbus.OpenPLCCoilsDataBlock(4, fake_sba)

    blk.getValues(1, 1)

    fake_sba.acquire_mutex.assert_called_once()
    fake_sba.release_mutex.assert_called_once()
