"""Tests for the process-loopback pieces of the Windows engine/com layer
(PER-APP-ROUTING.md Phase 1).

Headless: no COM activation, no hardware. Covers contract A (the new
`com.py` declarations and helpers) and the constructor half of contract B
(`Engine(source_id=None, *, pid=None)` — exactly one of the two). The
actual activation path (`_open_source` doing `ActivateAudioInterfaceAsync`
on the pump thread) needs real mmdevapi and is exercised on hardware via
`python -m pipemix.windows.wasapi.engine --pid <pid>`, not here.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from pipemix.windows.wasapi.engine import Engine


# -- Engine constructor: source_id xor pid ------------------------------

def test_engine_requires_one_source():
    with pytest.raises(ValueError):
        Engine()


def test_engine_rejects_both_source_and_pid():
    with pytest.raises(ValueError):
        Engine("x", pid=1)


def test_engine_pid_source():
    e = Engine(pid=42)
    assert e.pid == 42
    assert e.source_id is None


def test_engine_source_id_source():
    e = Engine("id")
    assert e.source_id == "id"
    assert e.pid is None


# -- com.py: process-loopback declarations ------------------------------

from pipemix.windows.wasapi import com


def test_process_loopback_constants():
    assert com.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK == 1
    assert com.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE == 0
    assert com.VT_BLOB == 65
    assert com.VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK == r"VAD\Process_Loopback"


def test_activation_params_struct():
    assert ctypes.sizeof(com.AUDIOCLIENT_ACTIVATION_PARAMS) == 12


def test_process_loopback_params():
    params, blob = com.process_loopback_params(1234)
    assert params.ActivationType == 1
    assert params.ProcessLoopbackParams.TargetProcessId == 1234
    assert params.ProcessLoopbackParams.ProcessLoopbackMode == 0
    assert ctypes.sizeof(params) == 12

    assert blob.vt == com.VT_BLOB
    assert blob.cbSize == ctypes.sizeof(params)
    assert blob.pBlobData == ctypes.addressof(params)


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="offsets are for 64-bit Python")
def test_propvariant_blob_offsets():
    blob = com.PROPVARIANT_BLOB
    assert blob.vt.offset == 0
    assert blob.cbSize.offset == 8
    assert blob.pBlobData.offset == 16


def test_process_loopback_format():
    fmt = com.process_loopback_format()
    assert fmt.wFormatTag == 3
    assert fmt.nChannels == 2
    assert fmt.nSamplesPerSec == 48000
    assert fmt.wBitsPerSample == 32
    assert fmt.nBlockAlign == 8
    assert fmt.nAvgBytesPerSec == 384000
    assert fmt.cbSize == 0


def test_com_interfaces_declared():
    # Presence only — these are comtypes interface classes, not callable here.
    assert com.IActivateAudioInterfaceAsyncOperation is not None
    assert com.IActivateAudioInterfaceCompletionHandler is not None
    assert com.IAgileObject is not None
