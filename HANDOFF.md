# Handoff: cameras, runaway watering, Gemini/AFC, website remote control

Written 2026-10-15 at the end of a Claude chat session, so a Claude Code session can
continue. Two repos are involved, side by side on the laptop
(`C:\Users\Freak\Documents\GitHub\`):

- **VIP-Vertical-Farm**: the grower that runs on the Raspberry Pi 4
  (`/home/verticalfarm/VIP-Vertical-Farm`, systemd service `ai-grower`,
  started with `python -m src.main`; code is pushed with `setup_device.py`
  over SFTP, so the Pi folder is probably **not** a git repo)
- **Vertical-Farm-Interface**: the website (FastAPI + static HTML/JS). Deployed to
  Google Cloud Run by `.github/workflows/deploy.yml` on every push to `main`.

None of the changes below are committed yet. Review them with `git diff` / `git status`.

---

## 0. URGENT: the AI is overwatering (not fixed yet)

The Pi log from 2026-10-15 09:20–09:27 (full copy: `HANDOFF-pi-log-2026-10-15.txt`) shows the AI in a runaway loop:

- `run_pump({"seconds": 60, "reason": "Quadragentesima-quadragesima (440th) emergency pulse. TARGETING THE 22.04 KILOSECOND HYDRATION MILESTONE..."})`
- `schedule_checkin(15, "... Maintaining 60s pulses every 15 minutes ... prevent canopy ignition under the fused 100% LED panel")`
- `log_milestone({"leaf_count": 1500, "height_cm": 15, "health_score": 1})` on **day 7** of a lettuce tray
- 60 s at `hardware_specs.flow_rate_ml_s: 168` is about **10 L per pulse**

Likely cause: the AI's saved memory (growth summary, recent decisions,
`data/` context files and the compressed summary) still holds the narrative
from the previous grow. `planted_date` was reset to 2026-10-08, but the context
wasn't, so each check-in builds on the previous one's story ("pulse 440 → 441").

The user was advised to stop the service (`sudo systemctl stop ai-grower`) or
unplug the pump until this is fixed. TODO, in this order:

1. **Hard water budget in code**, which the AI can't override: e.g.
   `water_pump.max_seconds_per_day` (and maybe `min_minutes_between_doses`)
   enforced in `Actuators.run_pump`, counting AI and scheduled doses; manual
   website doses could be exempt or counted separately. Ask the team for the
   numbers (lettuce tray, 168 mL/s pump).
2. **Reset the AI's memory for the new grow.** Look at
   `src/context_manager.py` (decisions log, growth summary, sensor log) and
   `reset_experiment.sh`. Add a "Reset AI memory" command for the website
   (back up the files to `data/archive/<date>/` rather than deleting them).
3. Sanity-check the system prompt in `context_manager.build_system_prompt()`
   for anything that encourages "emergency" escalation.
4. Note: `run_pump` blocks the tool loop for the full dose (09:26:44 → 09:27:47).

## 1. Gemini / AFC

The log shows on every call:
`google_genai.models: AFC is enabled with max remote calls: 10.` +
`WARNING ... Direct use of automatic function calling (AFC) in Models.generate_content is not recommended`.

`src/gemini_client.py` passes `FunctionDeclaration`s and runs the tool loop
itself (`ai_grower._run_tool_loop` → `submit_tool_results`), so the SDK's AFC
shouldn't do anything except log noise. **Fixed (uncommitted):** `_make_config`
now sets
`automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)`.
Verify on the Pi that the warning is gone and tool calls still work. The user
said "we also may be facing issues with the gemini API and AFC", so check the
full logs for other Gemini errors (429s, empty responses, `thought_signature`
issues with `gemini-3-flash-preview` when replaying model turns).

## 2. Cameras (mostly done)

Symptom last week: cameras returned no frames on the Pi but worked on laptops.
In the 2026-10-15 log **both cameras captured fine**, so it may have been
power/USB. Code changes (uncommitted, in `src/camera.py`):

- devices can be stable paths (`plant_cam: "/dev/v4l/by-id/...-video-index0"`)
  instead of indices; old `plant_cam_index` still works
- forces V4L2 + MJPG on Linux, checks `isOpened`, retries, one capture at a time
  (class-level lock), logs which cameras exist when a capture fails
- fixed: images were sorted by filename, so lights-off `plant_test_*` probes and
  stale `tool_capture` images were treated as "latest"; cleanup deleted all
  dashboard images first. Now sorted by mtime, and `_test_` probes are skipped
- NEW: per-camera `health` dict, `set_devices()`, and live-view streaming
  (`start_stream` / `read_stream_frame` / `stop_streams`; scheduled captures
  reuse an open stream)
- `diagnose_cameras.py` (repo root): run on the Pi; checks under-voltage
  (`vcgencmd get_throttled`), dmesg, which process holds `/dev/video*`, grabs a
  frame from each camera, and prints the config lines to use
- `config.yaml` camera section: new `mjpg`, `warmup_frames`, `retries` + comments

## 3. Website: what the team asked for

1. Show errors on the website directly ("if it is facing an error it will say
   that directly in the website interface").
2. Real live camera view.
3. Control as much of the Pi as possible from the website: lights & pump,
   AI check-in/pause/resume/scheduled actions, cameras (live, photo, swap,
   rotation) & plant settings, system (restart grower, reboot Pi, logs, CPU
   temp/disk).
4. **Update code from GitHub from the website**: e.g. when Gemini function
   calling breaks, push a fix to GitHub, click update, the Pi pulls, restarts,
   and the site says in real time whether the new code is working.
5. AI decisions / function calls on the site are stuck at **Oct 4**. Show AI
   activity live.

**Decided architecture: Firebase (Firestore) as the relay.** Cloud Run can't
reach the Pi (private network); the old `GROWER_API_URL` "Update Now" path
can't work. The team chose this over a Cloudflare tunnel. Live view is about
1 fps (Firestore allows ~1 write/s per doc). All controls go behind the existing
admin password (`ADMIN_PASSWORD` env, default `vipfarm2026` hard-coded in
`app.py`; it should be set as a Cloud Run secret now that it guards the pump).
Live view itself can be public.

### Why the site is stuck at Oct 4 (two causes, both need fixing)

- Website: `Dockerfile` runs `fetch_data.py` once at container start and
  `app.py` serves that `data.json` snapshot forever. Nothing refreshes it
  except "Update Now", which first tries to call the Pi directly (fails).
- Pi: a decision is only written to Firestore after a **successful** check-in.
  Both-cameras-failed returns early; Gemini exceptions skip `_log_decision`.
  Neither shows on the site.
- Couldn't query Firestore from the chat sandbox (network blocked), so it's
  unconfirmed which one dominates. Check `grows/raspPi4/decisions`
  (ordered by `timestamp`) in the Firebase console or with a small script.

### Firestore layout (designed; Pi side mostly written)

```
grows/<device_id>/live/status        heartbeat ~15s: health, problems[], recent_errors[],
                                     activity[] (live AI steps), grower{paused, checkin_running,
                                     last_checkin, next_checkin, pending_actions, day, stage,
                                     sensors}, actuators{light_on, light_off_at, pump_running},
                                     cameras{plant, dashboard health, devices, rotation, available},
                                     live{active, until_epoch, cameras, fps}, settings{plant,
                                     scheduler}, host{cpu_temp_c, disk_free_gb, mem_used_pct,
                                     undervoltage_*}, version{commit, branch, message,
                                     local_changes}, update{state...}, supervisor{state...}
grows/<device_id>/live/logs          {updated_at, lines[300]}
grows/<device_id>/live/frame_plant   {jpeg: bytes, at, epoch, seq, width, height, error}
grows/<device_id>/live/frame_dashboard
grows/<device_id>/commands/<id>      website writes {type, args, status:"pending",
                                     created_at, created_epoch, source}; Pi sets
                                     running/done/error/expired + result/error/progress
```

Commands handled by `RemoteControl.cmd_<type>`: `ping, lights_on{minutes},
lights_off, pump{seconds,reason}, checkin, schedule_checkin{minutes}, pause_ai,
resume_ai, cancel_action{id}, capture{camera}, live_start{cameras,seconds},
live_stop, set_cameras{plant_cam,dashboard_cam,plant_rotation}, swap_cameras,
set_plant{species,variety,planted_date}, set_schedule{min/max_checkin_minutes},
get_logs, restart, reboot, update_code{branch,discard_local_changes,restart_anyway}`.
Commands older than 10 min are marked `expired` (Pi was offline). Planned to add:
`reset_ai_memory`, water-budget settings.

## 4. Pi side: status of each piece

| Piece | Status |
|---|---|
| `src/remote_control.py` (LogBuffer, load_config w/ `config.local.yaml` merge, RemoteControl: heartbeat, problems, logs, command listener (on_snapshot + poll fallback), all commands above, live loop, git update + compile check + restart, startup verification) | **Written, compiles, NOT wired in or tested** |
| `src/ai_grower.py`: `paused` (persisted in `data/remote_state.json`), `set_paused()`, `run_checkin(force=)`, scheduled pump/light skipped while paused, `remote` hook + `_activity()` events (checkin_start, cameras, gemini, thinking, tool_call, tool_result, decision, error), `last_checkin` | **Done (uncommitted)** |
| `src/actuators.py`: `_light_off_at`; `cleanup()` no longer crashes when GPIO is missing | **Done** |
| `src/gemini_client.py`: AFC disabled | **Done** |
| `src/main.py` | **TODO**: use `remote_control.load_config()`; add `LogBuffer` to root logger in `setup_logging`; call `grower.firebase.start()` early; create `RemoteControl(grower, config, base_dir, log_buffer)`, set `grower.remote = remote`, `remote.start()`; stop it in `shutdown`. **Skip the 10 s pump hardware test and the camera/light test when `VFARM_RESTART=1`** (or `startup.hardware_test: false`), otherwise every remote restart waters the plants. |
| `supervisor.py` (repo root) | **TODO**: runs `python -m src.main` with `VFARM_SUPERVISED=1` (and `VFARM_RESTART=1` after the first start); exit code 42 → restart; crash → write `supervisor{state:"crashed", exit_code, log_tail}` to `live/status`; if `data/update_state.json` says `restarting`/`verifying` and the crash came within ~5 min, roll back (`git reset --hard <from_commit>`, or untar `data/pre_git_backup.tar.gz` for the first-ever update), mark the command `error`, and restart; forward SIGTERM; backoff. Needs its own firebase_admin init from config. |
| systemd | **TODO**: `setup_device.py` service `ExecStart` → `venv/bin/python supervisor.py`; add `supervisor.py`, `diagnose_cameras.py` to `PUSH_ITEMS`. Reboot needs `NOPASSWD` for `/sbin/reboot` (error message in `cmd_reboot` explains). |
| `.gitignore` | **TODO**: add `config.local.yaml`, `data/update_state.json`, `data/last_good_commit`, `data/remote_state.json`, `data/pre_git_backup.tar.gz` |
| Firestore cost check | heartbeat 15 s ≈ 5.8k writes/day; frames 1/s per camera only while live; website status polling should be ~5–10 s with a short server-side cache |

## 5. Website side: TODO (nothing written yet)

`Vertical-Farm-Interface/app.py`:
- lazy Firestore client (reuse `_get_bucket` init); `DEVICE_ID` env (default `raspPi4`)
- `GET /api/device/status`: read `live/status`, add `age_s` + `online` (age < ~50 s), cache ~2 s
- `GET /api/device/frame/{plant|dashboard}`: return JPEG bytes from the frame doc, `Cache-Control: no-store`, `X-Frame-Time` header
- `GET /api/device/logs`, `GET /api/device/commands?limit=`, `GET /api/device/command/{id}`
- `POST /api/device/command`: requires `X-Admin-Password` except `live_start`/`live_stop`; whitelist + validate args; write `created_epoch`
- replace the `GROWER_API_URL` path in `/api/trigger_update` with a `checkin` command and wait for it
- make `/api/data` fresh: incremental refresh (query decisions/sensors/images with `timestamp >` last seen) every ~30–60 s instead of the startup-only `data.json` (avoid re-reading every doc: read costs)

`static/` (index.html, script.js, styles.css; dark glass theme, Inter/Outfit, lucide icons, Chart.js):
- top status banner: offline ("last heard X ago"), crashed (+ log tail), each `problems[]` item, code-update progress; header badge reflects health instead of the hard-coded "AI Grower Active"
- Live View: "Go live" polls frames ~1/s (fetch → blob → objectURL), shows "LIVE · 2s ago" or "last photo at …", per-camera error overlay; renew `live_start` every ~4 min while the tab is visible
- live AI activity feed (from `status.activity`) above the decisions log
- Control Center (unlock with the admin password): Grow (lights, pump with confirm, check-in, pause/resume, scheduled actions + cancel), Cameras (photo, swap, rotation, device paths from `cameras.available`), Settings (plant, schedule limits), System (host stats, version, Update from GitHub [branch, discard local edits], restart, reboot, logs viewer), recent commands with live status
- existing bug: `updateUI()` puts `_dashImages[0]` in the plant slot and vice versa. Possibly a deliberate workaround for swapped cameras; confirm with the team, then use the swap control instead

## 6. Testing plan

- Unit-test `RemoteControl` with a fake Firestore (in-memory dict implementing
  `document().set(merge)`, `collection().where().get()/on_snapshot`), a fake grower
  (`SimpleNamespace` with camera/actuators/scheduler/context/sensors), and run each command.
- On the Pi: `python3 diagnose_cameras.py`, then run under `supervisor.py`
  manually, watch `live/status` in the Firebase console, send a `ping` command
  doc by hand, then try lights → live view → update_code (push a trivial commit).
- Website locally: `python fetch_data.py && python app.py` (needs
  `firebase-credentials.json`), then deploy by pushing to `main`.

## 7. Other notes

- `src/main.py` currently runs the pump for 10 s and the light for 10 s + 20 s on **every start**.
- Data on the Pi lives in `data/` (images, logs, action queue). Context files are
  in `context_manager.py`; check before resetting.
- The Pi log shows the startup check-in was queued for 300 s (min_checkin_minutes: 5).
- Laptop repos use CRLF line endings (Windows checkout).
