"""
PipeMix — Controller (macOS).

The state machine: owns the SharingSession, reacts to device hotplug, runs
crash recovery, drives the backend, and pushes updates to the UI as signals.
All business logic lives here; the UI only triggers and listens.

Forked from `pipemix.windows.controller`, minus what macOS has no use for:

- No leader mode. The hub is always an aggregate device we own, so there is
  never a real device to re-elect when one drops.
- No per-app routing (yet): no app poll, no overrides, no pins. `streams()`
  is always empty and the page hides the Apps tab.
- Crash recovery also has an orphan to clean: an aggregate device outlives
  the process that made it.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from typing import TYPE_CHECKING

from pipemix.models import AudioDevice, SessionState, SharingSession, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows.signal import SignalEmitter

if TYPE_CHECKING:
    from pipemix.macos.backend import CoreAudioBackend

log = logging.getLogger(__name__)


def locked(fn):
    """Serialize routing: the page calls in on one thread, the notify worker on another."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Controller(SignalEmitter):

    def __init__(self, backend: CoreAudioBackend, config: ConfigManager | None = None,
                 monitor=None) -> None:
        super().__init__()
        self.backend = backend
        self.config = config or ConfigManager()

        if monitor is None:
            from pipemix.macos.notify import DeviceMonitor
            monitor = DeviceMonitor()
        self.monitor = monitor
        self.monitor.on_connect = self._on_connect
        self.monitor.on_disconnect = self._on_disconnect

        self.session = SharingSession()
        self.devices: dict[str, AudioDevice] = {}
        self.prev_default: str | None = None

        # Device UIDs we want back if they reconnect mid-session.
        self.targets: set[str] = set()

        self.master_volume = 50

        # The page calls in on pywebview's thread while the notify worker
        # fires on its own, and both change which outputs the session feeds.
        self._lock = threading.RLock()

    # ---------- Startup / shutdown ----------

    def start(self) -> None:
        status = self.backend.health()
        self.emit("health-changed", status)
        if status.health != BackendHealth.OK:
            log.warning("Backend unhealthy on startup: %s", status.message)

        self.clean_orphans()

        try:
            self.monitor.start()
        except Exception as e:
            log.error("Failed to start device monitor: %s", e)

        self.refresh()

    def stop(self) -> None:
        # The hub, not the state: a session whose every output dropped sits in
        # REPAIRING, and its hub still has to go and the default come back.
        if self.session.sink:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        self.monitor.stop()

    def clean_orphans(self) -> None:
        """Undo what a crashed run left behind: a hub nobody owns, and a
        default output still pointing at it."""
        try:
            stranded = self.config.data.get("prev_default")
            if stranded:
                live = {d.id for d in self.backend.list_outputs()}
                if stranded in live:
                    log.warning("Previous run was interrupted — restoring default output to %s.",
                                stranded)
                    self.backend.set_default(stranded)
                else:
                    log.warning("Previous run was interrupted, but its default output "
                                "(%s) is no longer connected.", stranded)
                self.config.data["prev_default"] = None
                self.config.save()

            if self.session.is_active:
                return

            orphans = self.backend.find_orphans()
            if orphans:
                current = self.backend.get_default()
                if current in {o.name for o in orphans}:
                    # Move off the hub before it disappears, or macOS picks for us.
                    target = self.backend.restore_target([])
                    if target:
                        self.backend.set_default(target)
                for o in orphans:
                    log.warning("Removing leftover PipeMix output %s", o.name)
                    self.backend.destroy_sink(o)
        except Exception as e:
            log.error("Error during crash recovery: %s", e)

    # ---------- Devices ----------

    def refresh(self) -> None:
        """Re-enumerate outputs. `backend.list_outputs()` is already the whole truth."""
        self.emit("health-changed", self.backend.health())
        try:
            found: dict[str, AudioDevice] = {}
            for dev in self.backend.list_outputs():
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.sink)
                found[dev.id] = dev

            # Keep the ones the session is waiting on, so the page can show
            # them as dropped out rather than forgetting them.
            for dev_id in self.targets - found.keys():
                old = self.devices.get(dev_id)
                if old:
                    old.connected = False
                    old.sink = None
                    found[dev_id] = old

            self.devices = found
            self.emit("devices-changed", list(found.values()))
            self.emit("streams-changed", self.streams())
        except Exception as e:
            log.error("Failed to refresh devices: %s", e)

    def _volume_of(self, dev_id: str, sink: str | None) -> int:
        """Keep the volume we already know; otherwise ask the device, else 50%."""
        if dev_id in self.devices:
            return self.devices[dev_id].volume
        if not sink:
            return 50
        try:
            return self.backend.get_volume(sink)
        except Exception:
            return 50

    def set_device_volume(self, dev_id: str, volume: int, unmute: bool = False) -> None:
        dev = self.devices.get(dev_id)
        if not dev:
            return
        dev.volume = volume
        if self._solo() is dev:
            self.master_volume = volume
        if dev.connected and dev.sink:
            try:
                self.backend.set_mute(dev.sink, False)
                self.backend.set_volume(dev.sink, volume)
            except Exception as e:
                log.error("Failed to set volume for %s: %s", dev_id, e)

    def set_master_volume(self, volume: int, unmute: bool = False) -> None:
        self.master_volume = volume

        solo = self._solo()
        if solo:
            # The session is transparent for a lone output, so the level
            # belongs on the device — and its row has to move with the master.
            self.set_device_volume(solo.id, volume, unmute)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            try:
                if unmute:
                    self.backend.set_mute(target, False)
                self.backend.set_volume(target, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target, e)

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the session's own level, and keep master honest for a lone output.

        With one output the master fader and that device's fader are two
        handles on the same thing, so the hub steps aside and the device
        carries the level. If both carried one they would multiply, and the
        fader would feel dead until it was most of the way up.
        """
        solo = devices[0] if len(devices) == 1 else None
        if solo:
            self.master_volume = solo.volume
        try:
            self.backend.set_volume(sink, self._hub_level(devices))
        except Exception as e:
            log.warning("Failed to set the level on %s: %s", sink, e)

    def _hub_level(self, devices: list[AudioDevice]) -> int:
        """100 when a lone output carries the level itself, else the master."""
        return 100 if len(devices) == 1 else self.master_volume

    def active_sink(self) -> str | None:
        """Whatever the session is currently playing through."""
        return self.session.sink.name if self.session.sink else None

    # ---------- Presets ----------

    @property
    def presets(self) -> dict:
        return self.config.data["presets"]

    @property
    def last_preset(self) -> str | None:
        return self.config.data["last_preset"]

    @last_preset.setter
    def last_preset(self, preset_id: str | None) -> None:
        self.config.data["last_preset"] = preset_id
        self.config.save()

    def save_preset(self, name: str, devices: list[str]) -> str:
        preset_id = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))
        if not preset_id:
            preset_id = f"preset_{int(time.time())}"
        self.config.save_preset(preset_id, name, devices)
        self.config.save()
        log.info("Saved preset '%s' (%s): %s", name, preset_id, devices)
        return preset_id

    def delete_preset(self, preset_id: str) -> None:
        self.config.delete_preset(preset_id)
        self.config.save()

    # ---------- Sharing ----------

    @locked
    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.sink:
            # The hub is already up, so only its legs move. Nothing is torn
            # down, and the outputs that are staying keep playing.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)
        try:
            self.prev_default = self.backend.restore_target(devices)
            self.config.data["prev_default"] = self.prev_default
            self.config.save()
            self.session.sink = self._route(devices)
            self._adopt(devices)
            log.info("Session active: %s", self.active_sink())
        except Exception as e:
            log.error("Failed to start session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> VirtualSink:
        """Stand up the hub and make it the default output, so every app follows."""
        self._prepare(devices)
        sink = self.backend.create_sink(devices)

        # Set the level before switching output, or the first moment of audio
        # lands at whatever level the devices happened to be at.
        self._level_hub(sink.name, devices)

        self.backend.set_default(sink.name)
        return sink

    def _retarget(self, devices: list[AudioDevice]) -> None:
        """Change which outputs the live hub feeds. The hub itself stays put."""
        self._prepare(devices)

        # Going from one output to two, the hub is still at 100 because the
        # lone device was carrying the level. Duck it before the new leg
        # attaches, or that output gets one blast at full volume.
        if self._hub_level(devices) < self._hub_level(self.session.devices):
            self._level_hub(self.session.sink.name, devices)

        self.backend.set_legs(self.session.sink, devices)
        self._level_hub(self.session.sink.name, devices)
        self._adopt(devices)

    def _prepare(self, devices: list[AudioDevice]) -> None:
        """Unmute each output and put it back at its own level."""
        for d in devices:
            if d.sink:
                try:
                    self.backend.set_mute(d.sink, False)
                    self.backend.set_volume(d.sink, d.volume)
                except Exception as e:
                    log.warning("Failed to configure %s: %s", d.name, e)

    def _adopt(self, devices: list[AudioDevice]) -> None:
        """Record who the session is for, now that the routing matches."""
        self.session.devices = devices
        self.targets = {d.id for d in devices}
        # The page reads who is in the session off each device.
        self.emit("devices-changed", list(self.devices.values()))
        self._set_state(SessionState.ACTIVE)

    def streams(self) -> list[dict]:
        """Per-app routing is not on macOS yet."""
        return self.backend.list_streams()

    def route_stream(self, stream_id: int, ids: list[str] | None) -> None:
        raise BackendError("Per-app routing is not supported on macOS yet.")

    @locked
    def stop_sharing(self) -> None:
        self._set_state(SessionState.STOPPING)
        try:
            if self.prev_default:
                try:
                    self.backend.set_default(self.prev_default)
                    self.config.data["prev_default"] = None
                    self.config.save()
                except Exception as e:
                    log.warning("Could not restore original default output: %s", e)

            if self.session.sink:
                self.backend.destroy_sink(self.session.sink)

            self.session.sink = None
            self.session.devices = []
            self.targets.clear()
            self.emit("devices-changed", list(self.devices.values()))
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Error while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    # ---------- Hotplug ----------

    def _on_connect(self, device_id: str) -> None:
        log.info("Device connected: %s", device_id)
        self.refresh()
        if device_id in self.targets:
            self._rebuild()

    @locked
    def _on_disconnect(self, device_id: str) -> None:
        log.info("Device disconnected: %s", device_id)

        dev = self.devices.get(device_id)
        if dev:
            dev.connected = False
            dev.sink = None
        self.emit("devices-changed", list(self.devices.values()))

        if not (self.session.is_active and device_id in self.targets):
            return

        log.warning("An active sharing device (%s) disconnected.", device_id)

        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != device_id)
            if d and d.connected and d.sink
        ]

        # Drop that one leg. The hub stays the default output either way, so
        # whatever is playing into it keeps playing on the rest.
        self._set_state(SessionState.REPAIRING)
        try:
            self.backend.set_legs(self.session.sink, remaining)
        except Exception as e:
            log.error("Failed to drop %s from the hub: %s", device_id, e)
        self.session.devices = remaining
        if remaining:
            self._level_hub(self.session.sink.name, remaining)

        if not remaining:
            log.warning("No sharing devices left connected.")
            return

        log.info("Continuing on: %s", [d.name for d in remaining])
        self._set_state(SessionState.ACTIVE)

    @locked
    def _rebuild(self) -> None:
        """Feed the hub to each target that has come back, as it comes back."""
        ready = [
            d for d in (self.devices.get(i) for i in self.targets)
            if d and d.connected and d.sink
        ]
        if not (self.session.sink and ready):
            return

        log.info("Reconnected — feeding %s again.", [d.name for d in ready])
        self._retarget(ready)
        # If every output had dropped, macOS will have moved the default off
        # the hub; take it back now that there is something to play on.
        if self.backend.get_default() != self.active_sink():
            try:
                self.backend.set_default(self.active_sink())
            except Exception as e:
                log.warning("Could not make the hub the default again: %s", e)

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("State: %s → %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("state-changed", state)
