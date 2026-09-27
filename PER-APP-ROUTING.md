# Per-app routing on Windows by per-app capture

Status: planned. Phase 0 (prototype) not started.

## Why

Per-app routing on Windows currently pins an app's output with
`SetPersistedDefaultAudioEndpoint`. That is a *persisted preference*, not a
live move, and some apps ignore it until they reopen their audio. Verified on
Win11 26200 with Apple Music (its audio comes from `AMPLibraryAgent.exe`):
while playing it ignored a per-app pin to either output, a cleared pin, and a
machine-default change. While sharing, such an app sits on CABLE Input and the
hub fans it out to every output, so pinning it to one output does nothing
until it restarts. Commit `527e9f0` only *reports* this ("didn't switch —
restart to apply").

## Idea

In hub mode every app already plays into CABLE Input, which is silent on its
own. Instead of capturing "CABLE Output" as one mix, capture **each app
separately** with process loopback and run one `Engine` per app, whose legs
are that app's outputs. Windows mixes every stream rendered to an endpoint, so
PipeMix never mixes anything itself — this reuses `Engine` and `_Leg` (drift
handling included) unchanged apart from the source.

What falls out of it:

- No per-app pins in hub mode, so apps that ignore reroutes just work.
- One app can go to several outputs (the current one-output limit goes away).
- App mute and volume keep working natively (see below).
- The controller polls streams, so the Apps tab finally updates live.

## Verified so far

- Process loopback (`ActivateAudioInterfaceAsync("VAD\\Process_Loopback")`)
  activates (`S_OK`) and captures Apple Music cleanly, polled without an event
  handle.
- The capture is taken **after** the app's session mute/volume: six alternating
  0.5 s windows gave unmuted RMS 0.015–0.020, muted RMS 0.0000 every time. So
  the app's own mute can't be used to silence its copy in the hub — hence the
  "CABLE is the silent sink" design rather than "mute and re-render".

## Phase 0 — prototype (Claude)

Extend the throwaway prototype (activation code below) to:

1. Capture an app while it plays into CABLE Input and render it to one
   output only, via the existing `_Leg`; listen for dropouts at 5 ms polling.
2. Find out whether system sounds (session pid 0) can be captured at all. If
   not, notification sounds go silent in hub mode — decide before Phase 1
   (options: accept it, or keep one exclude-mode capture of PipeMix's own pid
   for "everything else", if exclude mode turns out to be CABLE-only).
3. Check that capturing the audio process's pid with
   `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE` covers Chromium browsers
   (Brave), whose audio can come from a child process.
4. Check that process loopback only picks up what the app plays into CABLE,
   not also what it plays on real devices (an app stuck on a real output must
   not be duplicated).

Activation, as prototyped (MTA; `sys.coinit_flags = 0` before importing
comtypes):

```python
class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (COMMETHOD([], HRESULT, "GetActivateResult",
                           (["out"], POINTER(HRESULT), "hr"),
                           (["out"], POINTER(POINTER(IUnknown)), "itf")),)

class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (COMMETHOD([], HRESULT, "ActivateCompleted",
                           (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),)

class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = ()

class Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]
    def __init__(self):
        super().__init__(); self.done = threading.Event()
    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, op):
        self.done.set(); return 0

# AUDIOCLIENT_ACTIVATION_PARAMS { ActivationType=1 (PROCESS_LOOPBACK),
#   { TargetProcessId=pid, ProcessLoopbackMode=0 (INCLUDE_TARGET_PROCESS_TREE) } }
# wrapped in a PROPVARIANT of vt=VT_BLOB (65): cbSize at offset 8, pBlobData at 16.
mmdevapi.ActivateAudioInterfaceAsync(r"VAD\Process_Loopback", IAudioClient._iid_,
                                     byref(propvariant), handler, byref(op))
handler.done.wait(5)
hr, unk = op.GetActivateResult()
client = unk.QueryInterface(IAudioClient)
# GetMixFormat is not supported here: pass a fixed format.
# WAVE_FORMAT_IEEE_FLOAT (3), 2 ch, 48000 Hz, 32 bit, nBlockAlign 8.
client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                  200 * 10_000, 0, byref(fmt), None)
```

## Phase 1 — two agents in parallel, disjoint files

The only contract between them is the `Engine` constructor:

```python
Engine(source_id: str | None = None, *, pid: int | None = None)
# exactly one of the two; start/stop/set_legs/legs/error unchanged
```

| Agent | Files | Delivers |
|---|---|---|
| 1 | `wasapi/com.py`, `wasapi/engine.py` | The interfaces above go in `com.py`. `Engine(pid=...)` makes `_open_source` activate process loopback with the fixed format; `_fmt`/`_bpf`/`_rate` come from that format, so legs (`AUTOCONVERTPCM`) work unchanged. The `__main__` self-check gains `--pid`. |
| 2 | `windows/backend.py`, `windows/controller.py`, their tests | See below. Tests use a mocked `Engine`. |

Agent 2, backend:

- Hub mode `create_sink` no longer starts a CABLE Output engine; it still sets
  up the session (default → CABLE Input is still the controller's job).
- New `set_app_routes(routes: dict[int, list[str]])`: the pid → device ids
  wanted right now. Starts an `Engine(pid=...)` per new pid, stops engines for
  pids no longer present, calls `set_legs` where the devices changed.
  `destroy_sink` stops them all. Never raises for one bad pid (log it and move
  on, like `_reconcile` does for a dead leg).
- Leader mode unchanged.

Agent 2, controller:

- A poll thread (1 s) while a hub-mode session is active: take the streams
  whose active session is on the hub (`endpoint == active_sink()`), build
  `routes = {pid: overrides.get(pid) or session device ids}`, call
  `backend.set_app_routes(routes)`, and emit `streams-changed` when the list
  changed.
  `# ponytail: 1 s poll; IAudioSessionNotification if the delay before a new app is heard matters.`
- Hub-mode `route_stream` only updates `overrides` (a list of device ids now)
  and reconciles at once: no pins, no one-output error. Leader mode keeps
  today's pin path.
- Session legs changing (`_retarget`, disconnect, rebuild) reconciles too.
- `stuck` then means only "active, but not playing into the hub" in hub mode
  (an app that started on a real output before sharing).

## Phase 2 — verify on the real machine (Claude)

Brave, Apple Music and a system sound across both headphones: pin, un-pin, pin
to two outputs, restart sharing mid-song, unplug a headset, close PipeMix.
Listen for dropouts and duplicated audio; watch CPU with several apps playing.

## Skipped

- Leader mode (no VB-CABLE) keeps pins. It has no silent sink, so per-app
  capture would still leave the original playing on the leader. Revisit if
  leader mode matters.
- Session notifications instead of polling (see the `ponytail:` line above).
